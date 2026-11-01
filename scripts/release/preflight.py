"""Read-only installation eligibility; never an execution isolation permit."""

import grp
import os
import platform
import pwd
import re
import shutil
import socket
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .layout import RELEASES
from .policy import MAX_FILES, DeliveryError, digest, relative_name

GIB = 1024**3
ACCOUNTS = {"kf-oj": 1200, "kf-coordinator": 1201, "kf-worker": 1202}
TASK_UID = 100000
TASK_GID = 100000


def protected(path: Path, *, directory: bool = False, ancestors: bool = True) -> None:
    """Only the deliberate `current` link is handled by the activation module."""
    paths = [path, *path.parents] if ancestors else [path]
    for item in paths:
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise DeliveryError("root-owned non-writable ordinary path required")
        if item != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise DeliveryError("ordinary protected directory required")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DeliveryError("ordinary protected single-link file required")


def protected_tree(
    root: Path, files: Sequence[str], directories: Sequence[str], *, path_prefix: str = ""
) -> None:
    """Check a closed tree afresh, walking its shared ancestors only at the root."""
    if len(files) + len(directories) > MAX_FILES:
        raise DeliveryError("protected tree entry count limit")
    names, folders = list(files), list(directories)
    for name in names:
        relative_name(path_prefix + name)
    for name in folders:
        relative_name(path_prefix + name + "/.__directory__")
    file_set, directory_set = set(names), set(folders)
    if (
        len(file_set) != len(names)
        or len(directory_set) != len(folders)
        or file_set & directory_set
    ):
        raise DeliveryError("protected tree inventory differs")
    for name in file_set | directory_set:
        parent, separator, _ = name.rpartition("/")
        if separator and parent not in directory_set:
            raise DeliveryError("protected tree parent directory missing")
    protected(root, directory=True)
    # Parents precede children: no leaf lookup traverses an unchecked directory.
    # Every directory, including empty ones, and every file is checked on each
    # call. There is no remembered ownership result between calls or operations.
    for name in sorted(folders, key=lambda name: (name.count("/"), name)):
        protected(root / name, directory=True, ancestors=False)
    for name in names:
        protected(root / name, ancestors=False)
    protected(root, directory=True)


def hardware() -> dict:
    """Conservative measurement binding; no performance claim is inferred."""
    cpu = Path("/proc/cpuinfo").read_text()[: 1024 * 1024]
    keys = {
        "vendor_id",
        "cpu family",
        "model",
        "model name",
        "stepping",
        "microcode",
        "flags",
    }
    processor = {}
    for line in cpu.split("\n\n", 1)[0].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip() in keys:
                processor[key.strip()] = value.strip()
    group = current_cgroup()
    limits = {
        name: (group / name).read_text().strip() if (group / name).exists() else "max"
        for name in ("cpu.max", "memory.max")
    }
    cpuset = group / "cpuset.cpus.effective"
    limits["cpuset.cpus.effective"] = cpuset.read_text().strip() if cpuset.exists() else ""
    return {
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "online_cpus": os.cpu_count(),
        "processor": processor,
        "parent_limits": limits,
    }


def current_cgroup() -> Path:
    value = Path("/proc/self/cgroup").read_text().strip()
    if not value.startswith("0::/") or "\n" in value:
        raise DeliveryError("unified cgroup v2 required")
    return Path("/sys/fs/cgroup") / value.removeprefix("0::/")


def identity_conflicts() -> list[str]:
    errors = []
    for name, identity in ACCOUNTS.items():
        for lookup, kind in ((pwd.getpwuid, "uid"), (grp.getgrgid, "gid")):
            try:
                row = lookup(identity)
            except KeyError:
                continue
            if row[0] != name:
                errors.append("reserved " + kind + " conflicts")
        try:
            account = pwd.getpwnam(name)
            if (account.pw_uid, account.pw_gid, account.pw_dir, account.pw_shell) != (
                identity,
                identity,
                "/nonexistent",
                "/usr/bin/nologin",
            ):
                errors.append("existing service identity differs")
        except KeyError:
            pass
        try:
            group = grp.getgrnam(name)
            if group.gr_gid != identity or group.gr_mem:
                errors.append("existing service group differs")
        except KeyError:
            pass
        if any(name in group.gr_mem for group in grp.getgrall() if group.gr_gid != identity):
            errors.append("service identity has supplementary authority")
    for rows, field, reserved in (
        (pwd.getpwall(), "pw_uid", TASK_UID),
        (grp.getgrall(), "gr_gid", TASK_GID),
    ):
        if any(getattr(row, field) == reserved for row in rows):
            errors.append("task identity already assigned")
    for name, reserved in (("subuid", TASK_UID), ("subgid", TASK_GID)):
        source = Path("/etc") / name
        if not source.exists():
            continue
        raw = source.read_text()
        if len(raw) > 1024 * 1024:
            errors.append("subordinate identity file exceeds bound")
            continue
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split(":")
            if len(fields) != 3 or not fields[1].isdigit() or not fields[2].isdigit():
                errors.append("subordinate identity allocation invalid")
            elif int(fields[1]) <= reserved < int(fields[1]) + int(fields[2]):
                errors.append("task identity overlaps subordinate allocation")
    return errors


def system_bus() -> dict:
    """Transient bounded operator units need the actual system message bus."""
    try:
        endpoint = Path("/run/dbus/system_bus_socket")
        protected(endpoint.parent, directory=True)
        info = endpoint.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
            raise DeliveryError("system message bus socket absent/unprotected")
        protected(Path("/usr/bin/busctl"))
        result = subprocess.run(
            [
                "/usr/bin/busctl",
                "--system",
                "--timeout=3",
                "get-property",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "Version",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/usr/bin", "LC_ALL": "C"},
        )
        if result.returncode or not re.fullmatch(
            r's "262(?:[-+ .][A-Za-z0-9.+() -]{1,120})?"\n?', result.stdout
        ):
            raise DeliveryError("system message bus manager unavailable/incompatible")
        return {"manager_reachable": True, "version": result.stdout.strip()}
    except (OSError, subprocess.SubprocessError) as error:
        raise DeliveryError("system message bus unavailable") from error


def installation_space(payload: Path, *, bundle_data: dict) -> dict:
    """Keep the fresh image/data reserve; discount only a fully verified release."""
    from . import manifest

    release = RELEASES / bundle_data["release_id"]
    existing_payload = 0
    if release.exists() or release.is_symlink():
        protected(release, directory=True)
        protected(release / "manifest.json")
        protected(release / "payload", directory=True)
        if (
            stat.S_IMODE(release.stat().st_mode) != 0o555
            or stat.S_IMODE((release / "manifest.json").stat().st_mode) != 0o444
        ):
            raise DeliveryError("existing release seal differs; operator inspection required")
        if manifest.verify(release, digest(payload.parent / "manifest.json")) != bundle_data:
            raise DeliveryError("existing release inventory differs")
        protected_tree(
            release / "payload",
            [record["path"] for record in bundle_data["files"]],
            bundle_data["directories"],
        )
        existing_payload = bundle_data["payload_bytes"]
    protected_images = sum(
        entry["bytes"] for entry in bundle_data["files"] if entry["path"].startswith("images/")
    )
    legacy_image = sum(
        entry["bytes"] for entry in bundle_data["files"] if entry["path"].startswith("images/gnu/")
    )
    new_payload = bundle_data["payload_bytes"] - existing_payload
    return {
        "required_free_bytes": new_payload + protected_images + legacy_image + 4 * GIB,
        "new_payload_bytes": new_payload,
        "verified_existing_payload_bytes": existing_payload,
        "protected_image_reserve_bytes": protected_images,
        "legacy_image_reserve_bytes": legacy_image,
        "mutable_operation_reserve_bytes": 4 * GIB,
    }


def observe(payload: Path, *, bundle_data: dict, require_root: bool = True) -> dict:
    from . import manifest

    with manifest.verification_scope():
        return _observe(payload, bundle_data=bundle_data, require_root=require_root)


def _observe(payload: Path, *, bundle_data: dict, require_root: bool) -> dict:
    errors = []
    observations = {}
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        errors.append("unsupported architecture")
    version = re.match(r"(\d+)\.(\d+)", platform.release())
    if not version or tuple(map(int, version.groups())) < (6, 6):
        errors.append("kernel below supported policy")
    if require_root and os.geteuid() != 0:
        errors.append("installer requires root")
    os_release = Path("/etc/os-release").read_text()
    if not re.search(r'^ID=(?:"?)(arch|cachyos)(?:"?)$', os_release, re.MULTILINE):
        errors.append("unsupported distribution profile")
    if Path("/proc/1/comm").read_text().strip() != "systemd":
        errors.append("systemd PID1 required")
    try:
        observations["system_bus"] = system_bus()
    except DeliveryError as error:
        errors.append(str(error))
    try:
        observations["hardware"] = hardware()
        flags = set(observations["hardware"]["processor"].get("flags", "").split())
        required = {
            "avx",
            "avx2",
            "bmi1",
            "bmi2",
            "f16c",
            "fma",
            "movbe",
            "xsave",
            "ssse3",
            "sse4_1",
            "sse4_2",
            "cx16",
            "popcnt",
        }
        if not required <= flags or not flags & {"abm", "lzcnt"}:
            errors.append("x86_64-v3 features missing")
        if (os.cpu_count() or 0) < 2:
            errors.append("two online CPUs required")
        controllers = set(Path("/sys/fs/cgroup/cgroup.controllers").read_text().split())
        if not {"cpu", "memory", "pids"} <= controllers:
            errors.append("required cgroup controllers missing")
        group = current_cgroup()
        while True:
            if group == Path("/sys/fs/cgroup"):
                break
            memory_max = (group / "memory.max").read_text().strip()
            if memory_max != "max" and int(memory_max) < 7 * GIB // 2:
                errors.append("effective memory limit below node capacity")
            quota, period = (group / "cpu.max").read_text().split()
            if quota != "max" and int(quota) < 2 * int(period):
                errors.append("effective CPU quota below two CPUs")
            group = group.parent
    except (OSError, ValueError):
        errors.append("cgroup/CPU observations unavailable")
    memory = {
        key: int(value.split()[0]) * 1024
        for key, value in (
            line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
        )
    }
    observations["memory"] = {key: memory[key] for key in ("MemTotal", "MemAvailable")}
    # A 4-GiB physical guest exposes slightly less MemTotal after kernel reserve.
    if memory["MemTotal"] < 15 * GIB // 4 or memory["MemAvailable"] < 5 * GIB // 2:
        errors.append("physical/available memory below installation capacity")
    disk = shutil.disk_usage("/var/lib")
    # A sealed identical release requires no second payload allocation. Images
    # and the four-GiB mutable reserve retain their full fresh-install budgets.
    space = installation_space(payload, bundle_data=bundle_data)
    required = space["required_free_bytes"]
    observations["disk"] = {
        "total_bytes": disk.total,
        "free_bytes": disk.free,
        **space,
    }
    if disk.total < 12 * GIB or disk.free < required:
        errors.append("installation/data capacity or inventory-derived free reserve insufficient")
    for name in ("libc.so.6", "ld-linux-x86-64.so.2"):
        system = Path("/usr/lib") / name
        try:
            protected(system)
            if digest(system) != digest(payload / "lib" / name):
                errors.append("system GNU ABI bytes differ from verified release")
        except (DeliveryError, OSError):
            errors.append("system GNU ABI unavailable/unprotected")
    for executable in (
        "/usr/bin/systemctl",
        "/usr/bin/useradd",
        "/usr/bin/groupadd",
        "/usr/bin/nologin",
        "/usr/bin/postgres",
        "/usr/bin/psql",
    ):
        try:
            protected(Path(executable))
        except (DeliveryError, OSError):
            errors.append("offline system dependency absent/unprotected")
    for path in (
        "/usr/lib/rabbitmq/sbin/rabbitmq-server",
        "/usr/lib/rabbitmq/plugins/rabbit-4.3.6/ebin/rabbit.app",
        "/usr/lib/erlang/erts-15.2.7/bin/beam.smp",
    ):
        try:
            protected(Path(path))
        except (DeliveryError, OSError):
            errors.append("offline broker/OTP dependency absent/unprotected")
    broker_version = Path("/usr/lib/rabbitmq/plugins/rabbit-4.3.6/ebin/rabbit.app")
    if broker_version.is_file():
        with broker_version.open("rb") as stream:
            header = stream.read(65537)
        if len(header) > 65536 or not re.search(rb'\{vsn,\s*"4\.3\.6"\}', header):
            errors.append("offline broker version differs")
    for executable, expected in (
        ("/usr/bin/systemctl", "systemd 262"),
        ("/usr/bin/postgres", "(PostgreSQL) 18.6"),
    ):
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                env={"PATH": "/usr/bin", "LC_ALL": "C"},
            )
            if result.returncode or expected not in result.stdout[:4096]:
                errors.append("offline system dependency version differs")
        except (OSError, subprocess.SubprocessError):
            errors.append("offline system dependency unavailable")
    errors += identity_conflicts()
    for directory in ("/opt", "/etc", "/var/lib"):
        try:
            protected(Path(directory), directory=True)
        except (OSError, DeliveryError):
            errors.append("installation ancestor unprotected")
    # Port occupation checks do not connect to application peers.
    for port in (8000, 8100):
        with socket.socket() as check:
            try:
                check.bind(("127.0.0.1", port))
            except OSError:
                errors.append("application control port occupied")
    if Path("/var/lib/kf-judge/factory/control.sock").exists():
        errors.append("factory socket exists; drain and stop before installation")
    return {
        "eligible": not errors,
        "errors": sorted(set(errors)),
        "observations": observations,
        "installed": False,
        "execution_verified": False,
    }
