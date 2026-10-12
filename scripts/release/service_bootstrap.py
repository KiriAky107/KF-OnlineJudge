"""Ordinary Rust service launch with verified private systemd credential bytes."""

import argparse
import hashlib
import os
import re
import stat
from pathlib import Path

from . import preflight
from .policy import DeliveryError, sha256

ROLES = {
    "coordinator": (1201, "kf-coordinator.service", "coordinator.json", "kf-coordinator"),
    "worker": (1202, "kf-worker.service", "worker.json", "kf-worker"),
}


def read_credential(path: Path, expected: str, uid: int) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in {0, uid}
        or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
        or not 1 <= info.st_size <= 32768
    ):
        raise DeliveryError("private bounded systemd credential required")
    content = path.read_bytes()
    if len(content) != info.st_size or hashlib.sha256(content).hexdigest() != sha256(expected):
        raise DeliveryError("service credential fingerprint mismatch")
    return content


def prepare(role: str, release: str, batch: str, directory: Path, expected: str) -> list[str]:
    if (
        role not in ROLES
        or not re.fullmatch(r"sd-[0-9a-f]{12}", release)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", batch)
    ):
        raise DeliveryError("invalid ordinary service binding")
    uid, unit, member, binary = ROLES[role]
    if os.getuid() != uid or os.geteuid() != uid or os.getgid() != uid:
        raise DeliveryError("dedicated ordinary service identity required")
    if directory != Path("/run/credentials") / unit or directory.is_symlink():
        raise DeliveryError("fixed systemd credential directory required")
    if preflight.current_cgroup() != Path("/sys/fs/cgroup/kfonlinejudge.slice") / unit:
        raise DeliveryError("service cgroup identity differs")
    content = read_credential(directory / member, expected, uid)
    runtime = Path("/run") / ("kf-" + role)
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise DeliveryError("private ordinary runtime directory required")
    path = runtime / "configuration.json"
    if path.exists() or path.is_symlink():
        old = path.lstat()
        if (
            not stat.S_ISREG(old.st_mode)
            or old.st_nlink != 1
            or old.st_uid != uid
            or (stat.S_IMODE(old.st_mode) != 0o600 or path.read_bytes() != content)
        ):
            raise DeliveryError("unknown runtime configuration refused")
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as target:
                target.write(content)
                target.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
    executable = Path("/opt/kf-onlinejudge/releases") / release / "payload/bin" / binary
    preflight.protected(executable)
    return [str(executable), *(["serve"] if role == "worker" else []), "--config", str(path)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Ordinary service credential bootstrap")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--configuration-id", required=True)
    parser.add_argument("--credential-directory", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        command = prepare(
            args.role,
            args.release_id,
            args.configuration_id,
            args.credential_directory,
            args.sha256,
        )
        # Only the verified normal service binary is launched. No student
        # executable or privileged Sandbox operation is accepted here.
        os.execve(
            command[0],
            command,
            {
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C.UTF-8",
                "LD_LIBRARY_PATH": str(Path(command[0]).parent.parent / "lib"),
            },
        )
    except (OSError, ValueError) as error:
        parser.exit(
            1,
            "Ordinary service refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
