"""Explicit cold data restoration on a clean node; no service/schema/grade writes."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import resource
import stat
import subprocess
import time
from pathlib import Path

from . import (
    backup,
    backup_manifest,
    dependencies,
    dependency_install,
    install,
    lifecycle,
    manifest,
    preflight,
    restore_policy,
)
from .policy import DeliveryError, canonical, digest, sha256
from .services import python_environment

STATE = Path("/var/lib/kf-onlinejudge/recovery")
LIVE = {
    name: backup.SOURCES[name]
    for name in ("postgres", "artifacts", "imports", "factory", "releases", "images")
}
HISTORICAL = backup_manifest.DOMAINS - set(LIVE)


def identifier(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"rr-[0-9a-f]{32}", value) is None:
        raise DeliveryError("explicit cold recovery ID required")
    return value


def guarded() -> None:
    if (os.getuid(), os.geteuid(), os.getgid()) != (0, 0, 0) or not set(os.getgroups()) <= {0}:
        raise DeliveryError("fixed root recovery operator required")
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    if status.get("NoNewPrivs", "").strip() != "1" or any(
        int(status.get(key, "-1").strip(), 16) != 15
        for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    ):
        raise DeliveryError("recovery read/copy/chown capability boundary differs")
    group = preflight.current_cgroup()
    if group != Path("/sys/fs/cgroup/kfonlinejudge.slice/kf-node-restore.service"):
        raise DeliveryError("fixed recovery operator unit required")
    for path, expected in (
        (group, ["536870912", "0", "32", "50000 100000"]),
        (Path("/sys/fs/cgroup/kfonlinejudge.slice"), ["4026531840", "0", "384", "200000 100000"]),
    ):
        observed = [
            (path / key).read_text().strip()
            for key in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
        ]
        if observed != expected:
            raise DeliveryError("actual recovery operator/node budgets differ")
    for key, maximum in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, backup_manifest.MAX_FILE_BYTES),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_STACK, 16 * 1024**2),
    ):
        if resource.getrlimit(key) != (maximum, maximum):
            raise DeliveryError("actual recovery process limits differ")
    backup.volume(permit_full=True, read_only=True)


def stopped() -> None:
    install.require_stopped()
    dependency_install.require_stopped()
    for name in (*lifecycle.APPS, *lifecycle.DEPENDENCIES):
        values = backup.manager(
            "show", "kf-" + name + ".service", "--property=MainPID,ActiveState,ControlGroup"
        )
        if values.get("MainPID") != "0" or values.get("ActiveState") != "inactive":
            raise DeliveryError("cold recovery requires stopped required services")
        lifecycle.Maintenance.empty_cgroup(name)
    processes = [p for p in Path("/proc").iterdir() if p.name.isdecimal()]
    if len(processes) > 1024:
        raise DeliveryError("recovery process observation exceeds bound")
    for process in processes:
        try:
            status = (process / "status").read_text()
        except FileNotFoundError:
            continue
        row = next(
            (line.split()[1:] for line in status.splitlines() if line.startswith("Uid:")), None
        )
        if row is None or len(row) != 4:
            raise DeliveryError("recovery process identity unavailable")
        if any(int(uid) in {1200, 1201, 1202, 1203, 1204, 1205, 100000} for uid in row):
            raise DeliveryError("recovery found an unmanaged data writer/task")
    for name in ("tasks", "identities", "captures"):
        path = Path("/var/lib/kf-judge") / name
        if path.exists() and next(path.iterdir(), None) is not None:
            raise DeliveryError("cold recovery task/lease/capture resources remain")
    if Path("/var/lib/kf-judge/factory/control.sock").exists():
        raise DeliveryError("cold recovery factory socket remains")


def target(record: dict, recovery: Path) -> tuple[Path, int, int, int]:
    name = backup_manifest.name(record["path"])
    domain, _, suffix = name.partition("/")
    if domain in LIVE:
        path = LIVE[domain] / suffix if suffix else LIVE[domain]
        return path, record["uid"], record["gid"], record["mode"]
    path = recovery / "history" / name
    return path, 0, 0, 0o700 if "bytes" not in record else 0o600


def member(path: Path, uid: int, gid: int, mode: int, *, directory: bool) -> None:
    info = path.lstat()
    if (
        (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode)
        or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)
    ):
        raise DeliveryError("existing recovery destination differs; no overwrite")


def file_witness(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def readback(
    path: Path, item: dict, uid: int, gid: int, mode: int, deadline: float
) -> tuple[int, ...]:
    member(path, uid, gid, mode, directory=False)
    before = file_witness(path.lstat())
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        if file_witness(os.fstat(stream.fileno())) != before:
            raise DeliveryError("recovery file identity changed before readback")
        total, checked = 0, hashlib.sha256()
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > item["bytes"] or time.monotonic() >= deadline:
                raise DeliveryError("recovery readback size/deadline changed")
            checked.update(block)
        after = file_witness(os.fstat(stream.fileno()))
    if total != item["bytes"] or checked.hexdigest() != item["sha256"]:
        raise DeliveryError("restored data byte verification failed")
    if before != after or after != file_witness(path.lstat()) or time.monotonic() >= deadline:
        raise DeliveryError("recovery file changed during readback")
    return after


def unchanged_file(path: Path, witness: tuple[int, ...], uid: int, gid: int, mode: int) -> None:
    member(path, uid, gid, mode, directory=False)
    if file_witness(path.lstat()) != witness:
        raise DeliveryError("verified recovery file changed before completion")


def write_file(
    source: Path, path: Path, item: dict, uid: int, gid: int, mode: int, deadline: float
) -> tuple[int, ...]:
    if path.exists() or path.is_symlink():
        # Existing destinations are read and checked, never overwritten.
        return readback(path, item, uid, gid, mode, deadline)
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as incoming:
        with os.fdopen(
            os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            ),
            "wb",
        ) as outgoing:
            total = 0
            while block := incoming.read(1024 * 1024):
                total += len(block)
                if total > item["bytes"] or time.monotonic() >= deadline:
                    raise DeliveryError(
                        "recovery input/deadline changed; private partial target retained"
                    )
                outgoing.write(block)
            if total != item["bytes"]:
                raise DeliveryError("recovery input bytes differ; private partial target retained")
            outgoing.flush()
            os.fchown(outgoing.fileno(), uid, gid)
            os.fchmod(outgoing.fileno(), mode)
            os.fsync(outgoing.fileno())
    # The backup was fully verified before copying. Independently read every
    # flushed destination while its pages are still local, then bind that proof
    # to its unchanged inode/metadata through completion on the stopped node.
    return readback(path, item, uid, gid, mode, deadline)


def restore(recovery_id: str, backup_id: str, expected: str) -> dict:
    guarded()
    stopped()
    selected = install.selection()
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in ("restore.py", "restore_policy.py", "backup.py", "backup_manifest.py"):
        if digest(payload / "delivery/scripts/release" / name) != digest(
            Path(__file__).parent / name
        ):
            raise DeliveryError("recovery operator differs from selected delivery")
    root = backup.ROOT / backup.identifier(backup_id)
    value = backup_manifest.verify(root, expected)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if value["consistent_cut"]["boot_id"] == boot:
        raise DeliveryError("cold recovery requires a different clean boot/node")
    if not value["created_unix_seconds"] <= int(time.time()) < value["expires_unix_seconds"]:
        raise DeliveryError("private recovery snapshot is future-dated or expired")
    data = manifest.verify(payload.parent, selected["manifest_sha256"])
    restore_policy.compatible(value, payload, data, root)
    restore_policy.identity_check(value, selected, root)
    for name in ("postgres", "rabbitmq"):
        # The copy operator writes PostgreSQL only; the fresh broker stays read-only.
        dependencies.storage_observation(name, read_only=name == "rabbitmq")
        member(
            Path(dependencies.STORAGE[name]["path"]),
            dependency_install.IDENTITIES[name][0],
            dependency_install.IDENTITIES[name][0],
            0o700,
            directory=True,
        )
    install.directory(STATE, 0o700)
    lock = STATE / "restore.lock"
    install.exclusive_file(lock, b"", 0o600)
    descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    recovery = STATE / identifier(recovery_id)
    record = {
        "schema_version": 1,
        "recovery_id": recovery_id,
        "backup_id": backup_id,
        "private_backup_sha256": sha256(expected),
        "source_sha": data["source_sha"],
        "configuration_id": selected["configuration_id"],
        "boot_id": boot,
        "data_restored": False,
        "database_credentials_rotated": False,
        "services_started": False,
        "execution_verified": False,
    }
    deadline = time.monotonic() + 1700
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stopped()
        if recovery.exists() or recovery.is_symlink():
            raise DeliveryError(
                "recovery ID exists; incomplete data retained for explicit inspection"
            )
        for directory in (install.RELEASES, install.IMAGES):
            if {path.name for path in directory.iterdir()} != {selected["release_id"]}:
                raise DeliveryError(
                    "cold recovery node has foreign or historical installed versions"
                )
        for domain in ("postgres", "artifacts", "imports", "factory"):
            path = LIVE[domain]
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_dir() or next(path.iterdir(), None) is not None:
                    raise DeliveryError("cold recovery target is not clean")
        recovery.mkdir(mode=0o700)
        (recovery / "history").mkdir(mode=0o700)
        install.exclusive_file(recovery / "prepared.json", canonical(record), 0o600)
        for item in sorted(
            value["directories"], key=lambda row: (row["path"].count("/"), row["path"])
        ):
            path, uid, gid, mode = target(item, recovery)
            if path.exists() or path.is_symlink():
                member(path, uid, gid, mode, directory=True)
            else:
                if path.parent.is_symlink() or not path.parent.is_dir():
                    raise DeliveryError("recovery parent is absent or linked")
                path.mkdir(mode=0o700)
                # Keep new directories Root-owned and writable until verified
                # children are complete. Final ownership/mode is applied below.
        free = os.statvfs(Path("/var/lib/kf-onlinejudge"))
        if free.f_frsize * free.f_bavail < value["payload_bytes"] + 128 * 1024**2:
            raise DeliveryError("cold recovery destination reserve insufficient")
        verified = {}
        for item in value["files"]:
            path, uid, gid, mode = target(item, recovery)
            verified[item["path"]] = write_file(
                root / "payload" / item["path"], path, item, uid, gid, mode, deadline
            )
        for item in sorted(
            value["directories"], key=lambda row: row["path"].count("/"), reverse=True
        ):
            path, uid, gid, mode = target(item, recovery)
            os.chown(path, uid, gid, follow_symlinks=False)
            path.chmod(mode)
            install.sync_directory(path)
            member(path, uid, gid, mode, directory=True)
        # Flush directory entries once after their children, including the two
        # parents whose new domain roots are outside the backup's directory list.
        install.sync_directory(recovery / "history")
        install.sync_directory(LIVE["postgres"].parent)
        stopped()
        if install.selection() != selected:
            raise DeliveryError("selected recovery identity changed")
        for item in value["files"]:
            if time.monotonic() >= deadline:
                raise DeliveryError("recovery verification deadline exceeded")
            path, uid, gid, mode = target(item, recovery)
            unchanged_file(path, verified[item["path"]], uid, gid, mode)
        record.update(
            data_restored=True,
            restored_payload_bytes=value["payload_bytes"],
            restored_files=len(value["files"]),
            restored_directories=len(value["directories"]),
            historical_configuration_active=False,
            factory_replay_history_retained=True,
        )
        install.exclusive_file(recovery / "data-restored.json", canonical(record), 0o600)
        install.sync_directory(recovery)
        install.sync_directory(STATE)
        return record
    finally:
        os.close(descriptor)


def launch(recovery_id: str, backup_id: str, expected: str) -> dict:
    identifier(recovery_id)
    backup.identifier(backup_id)
    sha256(expected)
    selected = install.selection()
    payload = install.RELEASES / selected["release_id"] / "payload"
    stopped()
    install.directory(STATE, 0o700)
    install.directory(LIVE["factory"], 0o711)
    current = backup.manager(
        "show", "kf-node-restore.service", "--property=LoadState,ActiveState,MainPID"
    )
    if current != {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}:
        raise DeliveryError("unknown or active recovery operator")
    command = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--unit=kf-node-restore.service",
        "--slice=kfonlinejudge.slice",
    ]
    for key, setting in {
        "User": "root",
        "Group": "root",
        "MemoryMax": "512M",
        "MemorySwapMax": "0",
        "TasksMax": "32",
        "CPUQuota": "50%",
        "RuntimeMaxSec": "1800",
        "TimeoutStopSec": "15",
        "LimitCORE": "0",
        "LimitFSIZE": "256M",
        "LimitNOFILE": "256",
        "LimitSTACK": "16M",
        "Restart": "no",
        "KillMode": "control-group",
        "UMask": "0077",
        "NoNewPrivileges": "yes",
        "CapabilityBoundingSet": "CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER",
        "AmbientCapabilities": "CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
        "PrivateDevices": "yes",
        "ProtectControlGroups": "yes",
        "MountFlags": "private",
        "RestrictAddressFamilies": "AF_UNIX AF_INET",
        "ReadOnlyPaths": str(backup.ROOT),
        "ReadWritePaths": " ".join(
            str(path)
            for path in [
                Path(dependencies.STORAGE["postgres"]["path"]),
                LIVE["artifacts"],
                LIVE["imports"],
                LIVE["factory"],
                LIVE["releases"],
                LIVE["images"],
                STATE,
            ]
        ),
    }.items():
        command.append("--property=" + key + "=" + setting)
    command += [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "LC_ALL=C.UTF-8",
        *python_environment(payload).split(),
        str(payload / "python/bin/python3.13"),
        "-s",
        "-P",
        "-m",
        "scripts.release.restore",
        "--inside",
        "--recovery-id",
        recovery_id,
        "--backup-id",
        backup_id,
        "--manifest-sha256",
        expected,
    ]
    result = subprocess.run(command, capture_output=True, timeout=1820, check=False)
    if result.returncode or len(result.stdout) > 4096:
        raise DeliveryError(
            "cold recovery failed; target remains stopped and partial data retained"
        )
    return json.loads(result.stdout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Private bounded cold data restoration")
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--backup-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if os.getuid() != 0 or os.geteuid() != 0:
            raise DeliveryError("explicit root cold recovery operator required")
        result = (restore if args.inside else launch)(
            args.recovery_id, args.backup_id, args.manifest_sha256
        )
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Cold recovery refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
