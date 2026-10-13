"""Explicit cold private backup; stopped owner proofs precede any data capture."""

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
    availability,
    backup_manifest,
    configuration,
    install,
    lifecycle,
    preflight,
    service_install,
)
from .policy import DeliveryError, canonical, digest, read_json, sha256
from .services import python_environment

ROOT = Path("/var/lib/kf-onlinejudge/backups")
CAPACITY = 16 * 1024**3
SOURCES = {
    "postgres": Path("/var/lib/kf-onlinejudge/postgres/data"),
    "artifacts": Path("/var/lib/kf-onlinejudge/artifacts"),
    "imports": Path("/var/lib/kf-onlinejudge/imports"),
    "app-config": Path("/etc/kf-onlinejudge"),
    "judge-config": Path("/etc/kf-judge"),
    "factory": Path("/var/lib/kf-judge/factory"),
    "operations": lifecycle.STATE,
    "releases": install.RELEASES,
    "images": install.IMAGES,
    "dependencies": Path("/etc/kf-dependencies"),
    "proxy": Path("/etc/kf-proxy"),
}


def identifier(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"bk-[0-9a-f]{32}", value) is None:
        raise DeliveryError("explicit private backup ID required")
    return value


def manager(*args: str) -> dict:
    raw = service_install.systemctl(*args)
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def stopped(selected: dict, cut: dict) -> None:
    lifecycle.validate_record(cut)
    if (
        cut["source_sha"][:12] != selected["release_id"][3:]
        or cut["configuration_id"] != selected["configuration_id"]
        or cut["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        or cut["action"] not in {"drain", "stop"}
        or cut["phase"] != "complete"
        or cut["success"] is not True
        or cut["root_cleanup_confirmed"] is not True
        or "drain_projection" not in cut
        or not lifecycle.projection_empty(cut["drain_projection"])
    ):
        raise DeliveryError("cold backup requires this boot owner-confirmed drained cut")
    record = availability.read()
    if (
        record is None
        or record.release_id != selected["release_id"]
        or record.configuration_id != selected["configuration_id"]
        or record.state != "quiescent"
    ):
        raise DeliveryError("cold backup admission is not proven quiescent")
    for name in [*lifecycle.APPS, *lifecycle.DEPENDENCIES]:
        props = manager(
            "show",
            "kf-" + name + ".service",
            "--property=MainPID,ActiveState,ControlGroup",
        )
        if props.get("MainPID") != "0" or props.get("ActiveState") != "inactive":
            raise DeliveryError("cold backup has an active required service")
        if props.get("ControlGroup"):
            group = Path("/sys/fs/cgroup") / props["ControlGroup"].lstrip("/")
            if group.exists():
                events = dict(
                    line.split() for line in (group / "cgroup.events").read_text().splitlines()
                )
                if events.get("populated") != "0":
                    raise DeliveryError("cold backup service descendant processes remain")
    for name in ("tasks", "identities", "captures"):
        path = Path("/var/lib/kf-judge") / name
        if path.exists() and next(path.iterdir(), None) is not None:
            raise DeliveryError("cold backup task resources remain")
    if Path("/var/lib/kf-judge/factory/control.sock").exists():
        raise DeliveryError("cold backup factory socket remains")
    processes = [p for p in Path("/proc").iterdir() if p.name.isdecimal()]
    if len(processes) > 1024:
        raise DeliveryError("cold backup process observation exceeds bound")
    for p in processes:
        try:
            status = (p / "status").read_text()
        except FileNotFoundError:
            continue
        rows = [line.split()[1:] for line in status.splitlines() if line.startswith("Uid:")]
        if len(rows) != 1 or len(rows[0]) != 4:
            raise DeliveryError("cold backup process identity unavailable")
        if any(int(uid) in {1200, 1201, 1202, 1203, 1204, 1205, 100000} for uid in rows[0]):
            raise DeliveryError("cold backup unmanaged data writer or task remains")
    if (SOURCES["postgres"] / "postmaster.pid").exists():
        raise DeliveryError("PostgreSQL did not confirm clean cold shutdown")


def volume(*, permit_full: bool = False, read_only: bool = False) -> None:
    preflight.protected(ROOT, directory=True)
    if stat.S_IMODE(ROOT.stat().st_mode) != 0o700:
        raise DeliveryError("private preprovisioned backup volume required")
    rows = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    matches = [r for r in rows if r[4] == str(ROOT)]
    if len(matches) != 1:
        raise DeliveryError("separate hard-sized backup filesystem required")
    row = matches[0]
    sep = row.index("-")
    opts = set(row[5].split(",")) | set(row[sep + 3].split(","))
    fs = os.statvfs(ROOT)
    if (
        row[3] != "/"
        or row[sep + 1] != "ext4"
        or any(r.startswith(("shared:", "master:")) for r in row[6:sep])
        or not {"ro" if read_only else "rw", "nosuid", "nodev", "noexec"} <= opts
        or (not read_only and "ro" in opts)
        or any(other[2] == row[2] and other != row for other in rows)
        or not CAPACITY * 9 // 10 <= fs.f_frsize * fs.f_blocks <= CAPACITY
    ):
        raise DeliveryError("backup volume limits or mount isolation differ")
    members = []
    for member in ROOT.iterdir():
        members.append(member)
        if len(members) > 3:
            raise DeliveryError("private backup directory count exceeds bound")
    snapshots = []
    for p in members:
        preflight.protected(p, directory=p.is_dir())
        if p.name == "lost+found" and p.is_dir() and not list(p.iterdir()):
            continue
        identifier(p.name)
        if not p.is_dir() or stat.S_IMODE(p.stat().st_mode) != 0o700:
            raise DeliveryError("unknown private backup volume member")
        snapshots.append(p)
    if len(snapshots) > 2 or (len(snapshots) == 2 and not permit_full):
        raise DeliveryError(
            "two-backup retention capacity reached; explicit verified pruning required"
        )


def broker_cookie_fingerprint() -> str:
    path = Path("/var/lib/kf-onlinejudge/rabbitmq/.erlang.cookie")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1204, 1204, 0o400)
        or info.st_size != 65
    ):
        raise DeliveryError("private stopped broker cookie identity differs")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        contents = stream.read(66)
        after = os.fstat(stream.fileno())
    keys = (
        "st_dev",
        "st_ino",
        "st_uid",
        "st_gid",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        re.fullmatch(rb"[0-9a-f]{64}\n", contents) is None
        or any(getattr(info, key) != getattr(before, key) for key in keys)
        or any(getattr(before, key) != getattr(after, key) for key in keys)
    ):
        raise DeliveryError("private stopped broker cookie changed")
    return hashlib.sha256(contents).hexdigest()


def guarded() -> None:
    if (os.getuid(), os.geteuid(), os.getgid()) != (0, 0, 0) or not set(os.getgroups()) <= {0}:
        raise DeliveryError("root backup operator required")
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    if status.get("NoNewPrivs", "").strip() != "1" or any(
        int(status.get(k, "-1").strip(), 16) != 4
        for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    ):
        raise DeliveryError("fixed read-search-only backup capability required")
    group = preflight.current_cgroup()
    if group != Path("/sys/fs/cgroup/kfonlinejudge.slice/kf-node-backup.service"):
        raise DeliveryError("fixed backup operator unit required")
    if {
        k: (group / k).read_text().strip()
        for k in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
    } != {
        "memory.max": "268435456",
        "memory.swap.max": "0",
        "pids.max": "32",
        "cpu.max": "50000 100000",
    }:
        raise DeliveryError("actual backup operator limits differ")
    node = Path("/sys/fs/cgroup/kfonlinejudge.slice")
    if {
        k: (node / k).read_text().strip()
        for k in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
    } != {
        "memory.max": "4026531840",
        "memory.swap.max": "0",
        "pids.max": "384",
        "cpu.max": "200000 100000",
    }:
        raise DeliveryError("actual backup node limits differ")
    for key, bound in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, backup_manifest.MAX_FILE_BYTES),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_STACK, 16 * 1024**2),
    ):
        if resource.getrlimit(key) != (bound, bound):
            raise DeliveryError("backup process limit differs")


def capture(backup_id: str, references_file: Path, references_sha: str) -> dict:
    guarded()
    selected = install.selection()
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in ("backup.py", "backup_manifest.py"):
        if digest(payload / "delivery/scripts/release" / name) != digest(
            Path(__file__).parent / name
        ):
            raise DeliveryError("backup operator differs from selected release")
    configuration.private(references_file)
    if digest(references_file) != sha256(references_sha):
        raise DeliveryError("separate private artifact receipt differs")
    refs = backup_manifest.validate_references(backup_manifest.read_metadata(references_file))
    cut = read_json(lifecycle.LAST, 32768)
    stopped(selected, cut)
    cookie_sha = broker_cookie_fingerprint()
    artifact_root = SOURCES["artifacts"].stat()
    if refs["boot_id"] != cut["boot_id"] or refs["storage_identity"] != {
        "device": artifact_root.st_dev,
        "inode": artifact_root.st_ino,
        "uid": artifact_root.st_uid,
    }:
        raise DeliveryError("artifact inventory was not observed on this source storage/boot")
    volume()
    lock = lifecycle.STATE / "maintenance.lock"
    configuration.private(lock)
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    deadline = time.monotonic() + 1700
    target = ROOT / identifier(backup_id)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stopped(selected, cut)
        if target.exists() or target.is_symlink():
            raise DeliveryError("backup target already exists; incomplete backups retained")
        target.mkdir(mode=0o700)
        tree = target / "payload"
        tree.mkdir(mode=0o700)
        dirs, files, total = [], [], 0
        for domain, source_root in SOURCES.items():
            if source_root.is_symlink() or not source_root.is_dir():
                raise DeliveryError("a required backup source domain is absent or linked")
            for directory, names, members in os.walk(source_root, followlinks=False):
                if time.monotonic() >= deadline:
                    raise DeliveryError("private backup capture deadline")
                source_dir = Path(directory)
                relative = backup_manifest.name(
                    domain
                    + (
                        "/" + source_dir.relative_to(source_root).as_posix()
                        if source_dir != source_root
                        else ""
                    )
                )
                info = source_dir.lstat()
                entry = {
                    "path": relative,
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "mode": stat.S_IMODE(info.st_mode),
                }
                backup_manifest._member(entry, set(), directory=True)
                if not stat.S_ISDIR(info.st_mode):
                    raise DeliveryError("backup source directory changed type")
                destination = tree / relative
                destination.mkdir(mode=0o700)
                dirs.append(entry)
                if len(dirs) + len(files) > backup_manifest.MAX_ENTRIES:
                    raise DeliveryError("backup directory inventory exceeds bound")
                for member in names:
                    if (source_dir / member).is_symlink():
                        raise DeliveryError("linked backup directory refused")
                for member in sorted(members):
                    p = source_dir / member
                    before = p.lstat()
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise DeliveryError("special or linked backup source refused")
                    entry = {
                        "path": backup_manifest.name(relative + "/" + member),
                        "uid": before.st_uid,
                        "gid": before.st_gid,
                        "mode": stat.S_IMODE(before.st_mode),
                        "bytes": before.st_size,
                    }
                    backup_manifest._member(entry, set(), directory=False)
                    if before.st_size > backup_manifest.MAX_FILE_BYTES:
                        raise DeliveryError("backup individual file exceeds bound")
                    total += before.st_size
                    if (
                        total > backup_manifest.MAX_BYTES
                        or len(dirs) + len(files) >= backup_manifest.MAX_ENTRIES
                        or os.statvfs(ROOT).f_frsize * os.statvfs(ROOT).f_bavail
                        < before.st_size + 64 * 1024**2
                    ):
                        raise DeliveryError("private backup capacity exhausted")
                    h = hashlib.sha256()
                    with os.fdopen(
                        os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC),
                        "rb",
                    ) as incoming:
                        with os.fdopen(
                            os.open(
                                destination / member,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                0o600,
                            ),
                            "wb",
                        ) as outgoing:
                            copy_file(incoming, outgoing, before, deadline, h)
                    entry["sha256"] = h.hexdigest()
                    files.append(entry)
                install.sync_directory(destination)
        stopped(selected, cut)
        if install.selection() != selected or read_json(lifecycle.LAST, 32768) != cut:
            raise DeliveryError("selected cut changed during backup")
        if broker_cookie_fingerprint() != cookie_sha:
            raise DeliveryError("stopped broker cookie changed during backup")
        manifest = read_json(payload.parent / "manifest.json")
        now = int(time.time())
        value = backup_manifest.validate(
            {
                "schema_version": 2,
                "format": backup_manifest.FORMAT,
                "backup_id": backup_id,
                "source_sha": manifest["source_sha"],
                "manifest_sha256": selected["manifest_sha256"],
                "configuration_id": selected["configuration_id"],
                "created_unix_seconds": now,
                "expires_unix_seconds": now + 7 * 86400,
                "consistent_cut": cut,
                "selected": selected,
                "artifact_references": refs,
                "broker_cookie_sha256": cookie_sha,
                "images": manifest["images"],
                "directories": sorted(dirs, key=lambda x: x["path"]),
                "files": sorted(files, key=lambda x: x["path"]),
                "payload_bytes": total,
                "restored": False,
                "execution_verified": False,
            }
        )
        raw = canonical(value)
        if len(raw) > backup_manifest.MAX_METADATA_BYTES:
            raise DeliveryError("private backup manifest exceeds bound")
        install.exclusive_file(target / "manifest.json", raw, 0o600)
        install.sync_directory(tree)
        install.sync_directory(target)
        install.sync_directory(ROOT)
        fingerprint = digest(target / "manifest.json")
        backup_manifest.verify(target, fingerprint)
        return {
            "backup_id": backup_id,
            "private_manifest_sha256": fingerprint,
            "payload_bytes": total,
            "source_sha": manifest["source_sha"],
            "captured": True,
            "restored": False,
            "execution_verified": False,
        }
    finally:
        os.close(descriptor)


def copy_file(incoming, outgoing, before, deadline: float, h) -> None:
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    observed = os.fstat(incoming.fileno())
    if any(getattr(before, key) != getattr(observed, key) for key in keys):
        raise DeliveryError("backup source changed before capture")
    count = 0
    while block := incoming.read(65536):
        count += len(block)
        if count > before.st_size or time.monotonic() >= deadline:
            raise DeliveryError("backup source or deadline changed")
        h.update(block)
        outgoing.write(block)
    after = os.fstat(incoming.fileno())
    if count != before.st_size or any(getattr(before, key) != getattr(after, key) for key in keys):
        raise DeliveryError("backup source changed during capture")
    outgoing.flush()
    os.fsync(outgoing.fileno())
    os.fchmod(outgoing.fileno(), 0o600)


def retention_eligible(expired: dict, retained: dict, now: int) -> None:
    """An explicit expired snapshot never removes the last verified recovery copy."""
    if (
        expired["backup_id"] == retained["backup_id"]
        or expired["expires_unix_seconds"] > now
        or retained["created_unix_seconds"] > now
        or retained["created_unix_seconds"] <= expired["created_unix_seconds"]
        or retained["expires_unix_seconds"] <= now
    ):
        raise DeliveryError("pruning requires an expired snapshot and a newer unexpired copy")


def prune(backup_id: str, expected: str, keep_id: str, keep_expected: str) -> dict:
    guarded()
    volume(permit_full=True)
    target, retained = ROOT / identifier(backup_id), ROOT / identifier(keep_id)
    lock = lifecycle.STATE / "maintenance.lock"
    configuration.private(lock)
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    deadline = time.monotonic() + 1700
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        value = backup_manifest.verify(target, expected)
        keep = backup_manifest.verify(retained, keep_expected)
        if value["backup_id"] != backup_id or keep["backup_id"] != keep_id:
            raise DeliveryError("private backup path does not match its receipt")
        retention_eligible(value, keep, int(time.time()))
        # Remove exactly the already verified inventory, never traverse or delete
        # an unknown tree. Any interruption leaves a private partial target.
        for item in value["files"]:
            if time.monotonic() >= deadline:
                raise DeliveryError("private backup pruning deadline")
            path = target / "payload" / item["path"]
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o600)
                or info.st_size != item["bytes"]
            ):
                raise DeliveryError("private backup changed before pruning")
            path.unlink()
            install.sync_directory(path.parent)
        for item in sorted(
            value["directories"], key=lambda item: item["path"].count("/"), reverse=True
        ):
            path = target / "payload" / item["path"]
            preflight.protected(path, directory=True)
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                raise DeliveryError("private backup directory changed before pruning")
            path.rmdir()
            install.sync_directory(path.parent)
        (target / "payload").rmdir()
        configuration.private(target / "manifest.json")
        if digest(target / "manifest.json") != sha256(expected):
            raise DeliveryError("private backup receipt changed before pruning")
        (target / "manifest.json").unlink()
        target.rmdir()
        install.sync_directory(ROOT)
        backup_manifest.verify(retained, keep_expected)
        return {
            "backup_id": backup_id,
            "pruned": True,
            "retained_backup_id": keep_id,
            "retained_manifest_sha256": keep_expected,
            "execution_verified": False,
        }
    finally:
        os.close(descriptor)


def launch(args) -> dict:
    """Only fixed backup actions; no program, identity or budget override."""
    identifier(args.backup_id)
    if args.action == "capture":
        configuration.private(args.references)
        if digest(args.references) != sha256(args.references_sha256):
            raise DeliveryError("private artifact inventory receipt differs")
        options = [
            "--references",
            str(args.references),
            "--references-sha256",
            args.references_sha256,
        ]
    else:
        sha256(args.manifest_sha256)
        options = ["--manifest-sha256", args.manifest_sha256]
        if args.action == "prune":
            identifier(args.keep_backup_id)
            sha256(args.keep_manifest_sha256)
            options += [
                "--keep-backup-id",
                args.keep_backup_id,
                "--keep-manifest-sha256",
                args.keep_manifest_sha256,
            ]
    selected = install.selection()
    payload = install.RELEASES / selected["release_id"] / "payload"
    current = manager("show", "kf-node-backup.service", "--property=LoadState,ActiveState,MainPID")
    if current != {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}:
        raise DeliveryError("unknown or active backup operator")
    command = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--unit=kf-node-backup.service",
        "--slice=kfonlinejudge.slice",
    ]
    for key, value in {
        "User": "root",
        "Group": "root",
        "MemoryMax": "256M",
        "MemorySwapMax": "0",
        "TasksMax": "32",
        "CPUQuota": "50%",
        "RuntimeMaxSec": "1800",
        "TimeoutStopSec": "15",
        "KillMode": "control-group",
        "Restart": "no",
        "LimitCORE": "0",
        "LimitFSIZE": "256M",
        "LimitNOFILE": "256",
        "LimitSTACK": "16M",
        "UMask": "0077",
        "NoNewPrivileges": "yes",
        "CapabilityBoundingSet": "CAP_DAC_READ_SEARCH",
        "AmbientCapabilities": "CAP_DAC_READ_SEARCH",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
        "PrivateDevices": "yes",
        "ProtectControlGroups": "yes",
        "RestrictAddressFamilies": "AF_UNIX",
        "ReadWritePaths": str(ROOT),
    }.items():
        command += ["--property=" + key + "=" + value]
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
        "scripts.release.backup",
        args.action,
        "--inside",
        "--backup-id",
        args.backup_id,
        *options,
    ]
    result = subprocess.run(command, capture_output=True, timeout=1820, check=False)
    if result.returncode or len(result.stdout) > 4096:
        raise DeliveryError(
            "private backup action failed; source unchanged and partial target retained"
        )
    return json.loads(result.stdout)


def selected_operator() -> None:
    selected = install.selection()
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in ("backup.py", "backup_manifest.py"):
        if digest(payload / "delivery/scripts/release" / name) != digest(
            Path(__file__).parent / name
        ):
            raise DeliveryError("backup operator differs from selected release")


def main() -> None:
    parser = argparse.ArgumentParser(description="Private bounded cold backup operator")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("capture")
    create.add_argument("--references", type=Path, required=True)
    create.add_argument("--references-sha256", required=True)
    check = sub.add_parser("verify")
    remove = sub.add_parser("prune")
    for command in (create, check, remove):
        command.add_argument("--backup-id", required=True)
        command.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    for command in (check, remove):
        command.add_argument("--manifest-sha256", required=True)
    remove.add_argument("--keep-backup-id", required=True)
    remove.add_argument("--keep-manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        if os.getuid() != 0 or os.geteuid() != 0:
            raise DeliveryError("explicit root backup operator required")
        if not args.inside:
            result = launch(args)
        else:
            guarded()
            selected_operator()
            if args.action == "capture":
                result = capture(args.backup_id, args.references, args.references_sha256)
            elif args.action == "prune":
                result = prune(
                    args.backup_id,
                    args.manifest_sha256,
                    args.keep_backup_id,
                    args.keep_manifest_sha256,
                )
            else:
                volume(permit_full=True)
                value = backup_manifest.verify(
                    ROOT / identifier(args.backup_id), args.manifest_sha256
                )
                if value["backup_id"] != args.backup_id:
                    raise DeliveryError("private backup path does not match its receipt")
                result = {
                    "verified": True,
                    "backup_id": value["backup_id"],
                    "restored": False,
                    "execution_verified": False,
                }
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Private backup refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
