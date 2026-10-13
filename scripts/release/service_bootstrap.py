"""Fixed ordinary service launch after effective identity and resource checks."""

import argparse
import hashlib
import os
import re
import resource
import stat
from pathlib import Path

from . import credentials, preflight
from .policy import DeliveryError, sha256
from .services import PROFILES

ROLES = {
    "coordinator": (
        1201,
        "kf-coordinator.service",
        "coordinator.json",
        "kf-coordinator",
    ),
    "worker": (1202, "kf-worker.service", "worker.json", "kf-worker"),
}
PYTHON_MODULES = {
    "api": [
        "uvicorn",
        "kf_oj.api.app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--no-proxy-headers",
        "--no-access-log",
        "--limit-concurrency",
        "32",
        "--backlog",
        "64",
    ],
    "outbox": ["kf_oj.infra.messaging.publisher"],
    "problem-import": ["kf_oj.workers.problem_import"],
}
COMMON_ENVIRONMENT = {
    "KF_OJ_ENVIRONMENT",
    "KF_OJ_LOG_LEVEL",
    "KF_OJ_BROWSER_ORIGINS",
    "KF_OJ_ARTIFACT_ROOT",
    "KF_OJ_IMPORT_STAGING_ROOT",
    "KF_OJ_ARTIFACT_MAX_BYTES",
    "KF_OJ_ARTIFACT_MAX_FILES",
    "KF_OJ_DATABASE_URL",
}
ROLE_ENVIRONMENT = {
    "api": COMMON_ENVIRONMENT
    | {
        "KF_OJ_JUDGE_ADMISSION",
        "KF_OJ_COORDINATOR_URL",
        "KF_OJ_COORDINATOR_TOKEN",
    },
    "outbox": COMMON_ENVIRONMENT | {"KF_OJ_AMQP_URL"},
    "problem-import": COMMON_ENVIRONMENT | {"KF_OJ_IMPORT_WORKER_CGROUP", "KF_OJ_JUDGE_ADMISSION"},
}


def bounded_text(path: Path) -> str:
    with path.open() as stream:
        contents = stream.read(65537)
    if len(contents) > 65536:
        raise DeliveryError("process observation exceeds bound")
    return contents


def observe_runtime(role: str) -> dict:
    group = preflight.current_cgroup()
    status = dict(
        line.split(":", 1)
        for line in bounded_text(Path("/proc/self/status")).splitlines()
        if ":" in line
    )
    limits = {}
    for name, identifier in {
        "core": resource.RLIMIT_CORE,
        "file": resource.RLIMIT_FSIZE,
        "files": resource.RLIMIT_NOFILE,
        "stack": resource.RLIMIT_STACK,
    }.items():
        limits[name] = list(resource.getrlimit(identifier))

    def read_limits(path):
        return {
            name: bounded_text(path / name).strip()
            for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
        }

    interfaces = (
        [
            line.partition(":")[0].strip()
            for line in bounded_text(Path("/proc/net/dev")).splitlines()
            if ":" in line
        ]
        if role == "problem-import"
        else None
    )
    return {
        "cgroup": str(group),
        "service_limits": read_limits(group),
        "node_limits": read_limits(Path("/sys/fs/cgroup/kfonlinejudge.slice")),
        "uids": status.get("Uid", "").split(),
        "gids": status.get("Gid", "").split(),
        "groups": status.get("Groups", "").split(),
        "no_new_privileges": status.get("NoNewPrivs", "").strip(),
        "capabilities": [
            status.get(key, "").strip()
            for key in (
                "CapInh",
                "CapPrm",
                "CapEff",
                "CapBnd",
                "CapAmb",
            )
        ],
        "pid": os.getpid(),
        "process_limits": limits,
        "interfaces": interfaces,
    }


def validate_runtime(role: str, observed: dict) -> None:
    if role not in PROFILES:
        raise DeliveryError("unknown ordinary service")
    user, memory, pids, _, _, _ = PROFILES[role]
    uid = str(preflight.ACCOUNTS[user])
    if (
        observed["cgroup"] != "/sys/fs/cgroup/kfonlinejudge.slice/kf-" + role + ".service"
        or observed["uids"] != [uid] * 4
        or observed["gids"] != [uid] * 4
        or not set(observed["groups"]) <= {uid}
        or observed["no_new_privileges"] != "1"
        or len(observed["capabilities"]) != 5
        or any(not re.fullmatch(r"0+", value) for value in observed["capabilities"])
    ):
        raise DeliveryError("ordinary process identity or privilege boundary differs")
    for limits, required_memory, required_pids, cpus in (
        (observed["service_limits"], memory, pids, 1),
        (observed["node_limits"], 3840, 384, 2),
    ):
        if (
            limits["memory.max"] != str(required_memory * 1024**2)
            or limits["memory.swap.max"] != "0"
            or limits["pids.max"] != str(required_pids)
        ):
            raise DeliveryError("effective ordinary/node memory or process budget differs")
        parts = limits["cpu.max"].split()
        if len(parts) != 2 or not all(v.isdecimal() for v in parts):
            raise DeliveryError("effective CPU budget unavailable")
        quota, period = map(int, parts)
        if not period or quota != cpus * period:
            raise DeliveryError("effective CPU budget differs")
    if observed["process_limits"] != {
        "core": [0, 0],
        "file": [64 * 1024**2] * 2,
        "files": [64 if role == "problem-import" else 256] * 2,
        "stack": [16 * 1024**2] * 2,
    }:
        raise DeliveryError("effective ordinary file/descriptor/stack limits differ")
    if role in PYTHON_MODULES and observed["pid"] != 1:
        raise DeliveryError("private ordinary PID namespace unavailable")
    if role == "problem-import" and observed["interfaces"] != ["lo"]:
        raise DeliveryError("private import network namespace unavailable")


def python_command(role: str, release: str) -> tuple[list[str], dict]:
    payload = Path("/opt/kf-onlinejudge/releases") / release / "payload"
    python = payload / "python/bin/python3.13"
    preflight.protected(python)
    environment = {key: value for key, value in os.environ.items() if key.startswith("KF_OJ_")}
    if set(environment) != ROLE_ENVIRONMENT[role] or any(
        not value or len(value) > 32768 or any(ch in value for ch in "\r\n\x00")
        for value in environment.values()
    ):
        raise DeliveryError("ordinary service environment fields differ")
    environment.update(
        {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C.UTF-8",
            "PYTHONHOME": str(payload / "python"),
            "PYTHONPATH": f"{payload}/backend/src:{payload}/site:{payload}/delivery",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LD_LIBRARY_PATH": f"{payload}/lib:{payload}/python/lib",
        }
    )
    return [str(python), "-s", "-P", "-m", *PYTHON_MODULES[role]], environment


def read_credential(path: Path, expected: str, uid: int) -> bytes:
    info = credentials.validate_metadata(path, uid, 32768)
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
    return [
        str(executable),
        *(["serve"] if role == "worker" else []),
        "--config",
        str(path),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Ordinary service credential bootstrap")
    parser.add_argument("role", choices=PROFILES)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--configuration-id", required=True)
    parser.add_argument("--credential-directory", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"sd-[0-9a-f]{12}", args.release_id) or not re.fullmatch(
            r"cfg-[0-9a-f]{24}", args.configuration_id
        ):
            raise DeliveryError("invalid ordinary service binding")
        validate_runtime(args.role, observe_runtime(args.role))
        if args.role in PYTHON_MODULES:
            if args.credential_directory is not None or args.sha256 is not None:
                raise DeliveryError("unexpected ordinary credential override")
            command, environment = python_command(args.role, args.release_id)
            if args.role == "api":
                # Added only after fixed argument and environment-file checks;
                # the batch ID cannot be embedded in its own hashed env file.
                environment["KF_OJ_DELIVERY_RELEASE_ID"] = args.release_id
                environment["KF_OJ_DELIVERY_CONFIGURATION_ID"] = args.configuration_id
        else:
            if args.credential_directory is None or args.sha256 is None:
                raise DeliveryError("fixed ordinary credential required")
            command = prepare(
                args.role,
                args.release_id,
                args.configuration_id,
                args.credential_directory,
                args.sha256,
            )
            environment = {
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C.UTF-8",
                "LD_LIBRARY_PATH": str(Path(command[0]).parent.parent / "lib"),
            }
        # Only the verified normal service binary is launched. No student
        # executable or privileged Sandbox operation is accepted here.
        os.execve(
            command[0],
            command,
            environment,
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
