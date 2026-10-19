"""A separate bounded log namespace; never change the host's default journal."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from . import configuration, install, manifest, preflight
from .policy import DeliveryError, canonical, digest, fields, read_json

NAMESPACE = "kf-onlinejudge"
UNIT = "systemd-journald@kf-onlinejudge.service"
CONFIG = Path("/etc/systemd/journald@kf-onlinejudge.conf")
DROPIN = Path("/etc/systemd/system") / (UNIT + ".d") / "50-kf-bounds.conf"
RECEIPT = Path("/var/lib/kf-onlinejudge/journal-profile.json")
CAPACITY = 64 * 1024**2
PROFILE = b"""[Journal]
Storage=persistent
SplitMode=none
SystemMaxUse=32M
RuntimeMaxUse=8M
SystemMaxFileSize=4M
RuntimeMaxFileSize=2M
SystemMaxFiles=8
RuntimeMaxFiles=4
MaxFileSec=1h
MaxRetentionSec=7day
RateLimitIntervalSec=30s
RateLimitBurst=100
LineMax=8192
ForwardToSyslog=no
ForwardToKMsg=no
ForwardToConsole=no
ForwardToWall=no
"""
UNIT_BOUNDS = b"""[Unit]
RequiresMountsFor=/var/log/journal/%m.kf-onlinejudge
StartLimitIntervalSec=600
StartLimitBurst=3
[Service]
Slice=kfonlinejudge.slice
MemoryMax=64M
MemorySwapMax=0
TasksMax=16
CPUQuota=25%
LimitCORE=0
LimitFSIZE=8M
LimitNOFILE=256
Restart=on-failure
RestartSec=30
TimeoutStopSec=15
"""


def manager(*args: str) -> str:
    # Import after service profiles are initialized so their future log-binding
    # checks can call this independent installer without an import cycle.
    from .service_install import systemctl

    return systemctl(*args)


def machine_id() -> str:
    path = Path("/etc/machine-id")
    preflight.protected(path)
    value = bounded_text(path, 64).strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value) or value == "0" * 32:
        raise DeliveryError("fixed machine journal identity required")
    return value


def bounded_text(path: Path, maximum: int) -> str:
    with path.open() as source:
        value = source.read(maximum + 1)
    if len(value) > maximum:
        raise DeliveryError("journal observation exceeds bound")
    return value


def validate_storage(value: dict, path: Path, *, allow_read_only: bool = False) -> None:
    fields(
        value,
        {"path", "root", "filesystem", "options", "shared", "capacity_bytes"},
        "journal storage",
    )
    if (
        value["path"] != str(path)
        or value["root"] != "/"
        or value["filesystem"] != "ext4"
        or value["shared"] is not False
        or not isinstance(value["options"], list)
        or not all(isinstance(option, str) for option in value["options"])
        or not {"nosuid", "nodev", "noexec"} <= set(value["options"])
        or set(value["options"]) & {"ro", "rw"}
        not in (({"rw"}, {"ro"}) if allow_read_only else ({"rw"},))
        or type(value["capacity_bytes"]) is not int
        or not CAPACITY * 9 // 10 <= value["capacity_bytes"] <= CAPACITY
    ):
        raise DeliveryError("dedicated journal hard storage budget differs")


def storage(*, allow_read_only: bool = False, observed_pid: int | None = None) -> dict:
    path = Path("/var/log/journal") / (machine_id() + "." + NAMESPACE)
    preflight.protected(path, directory=True)
    if observed_pid is not None and (type(observed_pid) is not int or not 1 < observed_pid < 2**31):
        raise DeliveryError("known journal process required")
    mountinfo = (
        Path("/proc/self/mountinfo")
        if observed_pid is None
        else Path(f"/proc/{observed_pid}/mountinfo")
    )
    rows = [line.split() for line in bounded_text(mountinfo, 1024**2).splitlines()]
    selected = [row for row in rows if len(row) > 6 and row[4] == str(path)]
    if len(selected) != 1:
        raise DeliveryError("dedicated journal mount required; never created by installer")
    row = selected[0]
    separator = row.index("-")
    if not allow_read_only and (
        "rw" not in row[separator + 3].split(",") or "ro" in row[separator + 3].split(",")
    ):
        raise DeliveryError("journal filesystem superblock is not writable")
    device = path.stat().st_dev
    if row[2] != f"{os.major(device)}:{os.minor(device)}":
        raise DeliveryError("journal daemon sees another storage device")
    info = os.statvfs(path)
    value = {
        "path": str(path),
        "root": row[3],
        "filesystem": row[separator + 1],
        "options": row[5].split(","),
        "shared": any(other[2] == row[2] and other != row for other in rows),
        "capacity_bytes": info.f_frsize * info.f_blocks,
    }
    validate_storage(value, path, allow_read_only=allow_read_only)
    return value


def validate_receipt(value: dict) -> None:
    fields(
        value,
        {
            "schema_version",
            "namespace",
            "machine_id",
            "files",
            "services_started",
            "execution_verified",
        },
        "journal receipt",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["namespace"] != NAMESPACE
        or not isinstance(value["machine_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["machine_id"])
        or value["machine_id"] == "0" * 32
        or value["files"]
        != {
            str(CONFIG): hashlib.sha256(PROFILE).hexdigest(),
            str(DROPIN): hashlib.sha256(UNIT_BOUNDS).hexdigest(),
        }
        or value["services_started"] is not False
        or value["execution_verified"] is not False
    ):
        raise DeliveryError("journal receipt differs from fixed profile")


def check_profile(*, allow_read_only: bool = False) -> dict:
    preflight.protected(RECEIPT)
    configuration.private(RECEIPT)
    receipt = read_json(RECEIPT, 4096)
    validate_receipt(receipt)
    if receipt["machine_id"] != machine_id():
        raise DeliveryError("journal profile belongs to another machine")
    for path, expected in receipt["files"].items():
        member = Path(path)
        preflight.protected(member)
        if (
            member.stat().st_gid != 0
            or stat.S_IMODE(member.stat().st_mode) != 0o644
            or digest(member) != expected
        ):
            raise DeliveryError("protected journal profile changed")
    # Refuse unrecorded instance-specific overrides; systemd's actual limits are
    # checked separately, including overrides inherited from the vendor template.
    if {p.name for p in DROPIN.parent.iterdir()} != {DROPIN.name}:
        raise DeliveryError("unknown journal instance override")
    check_configuration_overrides()
    return storage(allow_read_only=allow_read_only)


def check_configuration_overrides() -> None:
    for root in ("/etc/systemd", "/run/systemd", "/usr/local/lib/systemd", "/usr/lib/systemd"):
        directory = Path(root) / ("journald@" + NAMESPACE + ".conf.d")
        if directory.exists() or directory.is_symlink():
            preflight.protected(directory, directory=True)
            if next(directory.iterdir(), None) is not None:
                raise DeliveryError("unrecorded namespace journal configuration override")


def check_ready() -> dict:
    # A cap-zero observer's ProtectSystem view is intentionally read-only.
    # That view establishes physical identity/capacity, not daemon writability.
    check_profile(allow_read_only=True)
    props = dict(
        line.split("=", 1)
        for line in manager(
            "show",
            UNIT,
            "--property=ActiveState,SubState,MainPID,ControlGroup,FragmentPath,DropInPaths,InvocationID,RootDirectory,RootImage",
        ).splitlines()
        if "=" in line
    )
    group = "/kfonlinejudge.slice/" + UNIT
    if (
        props.get("ActiveState") != "active"
        or props.get("SubState") != "running"
        or props.get("ControlGroup") != group
    ):
        raise DeliveryError("bounded journal instance unavailable")
    if props.get(
        "FragmentPath"
    ) != "/usr/lib/systemd/system/systemd-journald@.service" or props.get(
        "DropInPaths", ""
    ).split() != [str(DROPIN)]:
        raise DeliveryError("unrecorded journal manager executable or override")
    if (
        props.get("RootDirectory")
        or props.get("RootImage")
        or not re.fullmatch(r"[0-9a-f]{32}", props.get("InvocationID", ""))
    ):
        raise DeliveryError("unknown journal process/root binding")
    pid = int(props.get("MainPID", "0"))
    if not 1 < pid < 2**31:
        raise DeliveryError("journal process identity unavailable")
    before = bounded_text(Path(f"/proc/{pid}/stat"), 16384).rsplit(")", 1)[1].split()[19]
    status = dict(
        line.split(":", 1)
        for line in bounded_text(Path(f"/proc/{pid}/status"), 16384).splitlines()
        if ":" in line
    )
    if status.get("Uid", "").split() != ["0"] * 4:
        raise DeliveryError("journal process authority differs")
    directory = Path("/sys/fs/cgroup" + group)
    limits = {
        name: (directory / name).read_text().strip()
        for name in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
    }
    if limits != {
        "memory.max": str(64 * 1024**2),
        "memory.swap.max": "0",
        "pids.max": "16",
        "cpu.max": "25000 100000",
    }:
        raise DeliveryError("effective journal budgets differ")
    # Kernel mountinfo is read-only and does not enter the daemon namespace or
    # require ptrace/setns. Match its mount device to this protected fixed path,
    # and require the daemon's actual view to be writable. No extra capability
    # or writable-path exception is given to the observer or Python services.
    value = storage(observed_pid=pid)
    current = dict(
        line.split("=", 1)
        for line in manager(
            "show", UNIT, "--property=ActiveState,MainPID,InvocationID"
        ).splitlines()
        if "=" in line
    )
    after = bounded_text(Path(f"/proc/{pid}/stat"), 16384).rsplit(")", 1)[1].split()[19]
    if (
        current.get("ActiveState") != "active"
        or current.get("MainPID") != str(pid)
        or current.get("InvocationID") != props["InvocationID"]
        or before != after
    ):
        raise DeliveryError("journal process changed during observation")
    value["effective_limits"] = limits
    value["memory_current"] = int(bounded_text(directory / "memory.current", 64))
    value["pids_current"] = int(bounded_text(directory / "pids.current", 64))
    fs = os.statvfs(Path(value["path"]))
    value["available_bytes"] = fs.f_frsize * fs.f_bavail
    return value


@manifest.verified_operation
def install_profile() -> dict:
    if os.getuid() != 0 or os.geteuid() != 0 or os.getgid() != 0:
        raise DeliveryError("root journal operator required")
    selected = install.selection()
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in ("journal.py", "manifest.py"):
        shipped = payload / "delivery/scripts/release" / name
        preflight.protected(shipped)
        if digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("journal operator differs from selected delivery")
    install.require_stopped()
    observed = storage()
    if RECEIPT.exists() or RECEIPT.is_symlink():
        check_profile()
        return {
            "profile_installed": True,
            "storage": observed,
            "services_started": False,
            "execution_verified": False,
        }
    props = dict(
        line.split("=", 1)
        for line in manager("show", UNIT, "--property=ActiveState,MainPID").splitlines()
        if "=" in line
    )
    if props.get("ActiveState") != "inactive" or props.get("MainPID") != "0":
        raise DeliveryError("unrecorded journal instance; refused adoption")
    path = Path(observed["path"])
    if any(
        p.name != "lost+found"
        or p.is_symlink()
        or not p.is_dir()
        or next(p.iterdir(), None) is not None
        for p in path.iterdir()
    ):
        raise DeliveryError("unrecorded journal data; refused adoption")
    for member in (CONFIG, DROPIN):
        if member.exists() or member.is_symlink():
            raise DeliveryError("unrecorded journal configuration; refused overwrite")
    check_configuration_overrides()
    install.directory(DROPIN.parent, 0o755)
    if next(DROPIN.parent.iterdir(), None) is not None:
        raise DeliveryError("unknown journal override directory")
    install.exclusive_file(CONFIG, PROFILE, 0o644)
    install.exclusive_file(DROPIN, UNIT_BOUNDS, 0o644)
    receipt = {
        "schema_version": 1,
        "namespace": NAMESPACE,
        "machine_id": machine_id(),
        "files": {
            str(CONFIG): hashlib.sha256(PROFILE).hexdigest(),
            str(DROPIN): hashlib.sha256(UNIT_BOUNDS).hexdigest(),
        },
        "services_started": False,
        "execution_verified": False,
    }
    validate_receipt(receipt)
    install.exclusive_file(RECEIPT, canonical(receipt), 0o600)
    manager("daemon-reload")
    check_profile()
    return {
        "profile_installed": True,
        "storage": observed,
        "services_started": False,
        "execution_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Install only the fixed KF log namespace profile")
    parser.parse_args()
    try:
        print(json.dumps(install_profile(), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Journal profile refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
