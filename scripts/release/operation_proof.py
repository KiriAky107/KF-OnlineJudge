"""Complete bundle byte proofs limited to one live Root maintenance invocation."""

import hashlib
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from . import preflight
from .policy import DeliveryError, canonical, digest, fields, integer, read_json, sha256

DIRECTORY = Path("/run/kf-node-verification")
RECORD = DIRECTORY / "byte-proof.json"
PARENT = "kf-node-maintenance.service"
GROUP = "/sys/fs/cgroup/kfonlinejudge.slice/"
CONSUMERS = frozenset(
    "kf-" + name + ".service"
    for name in (
        "postgres",
        "rabbitmq",
        "sandbox-factory",
        "coordinator",
        "api",
        "outbox",
        "problem-import",
        "worker",
        "proxy",
        "node-health",
    )
)
MAX_AGE_NS = 900 * 10**9
MAX_RECORD = 4096
_REPORTED: tuple[int, str] | None = None


def _root() -> bool:
    return (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (
        0,
        0,
        0,
        0,
    ) and set(os.getgroups()) <= {0}


def _now() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def _boot() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _signature(value: dict) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _manager(release: str, configuration: str) -> dict:
    from . import service_install

    expected = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "Transient": "yes",
        "ControlGroup": "/kfonlinejudge.slice/" + PARENT,
        "Description": "KF fixed node-maintenance " + release + "." + configuration,
        "User": "root",
        "Group": "root",
        "MemoryMax": "268435456",
        "MemorySwapMax": "0",
        "TasksMax": "32",
        "CPUQuotaPerSecUSec": "500ms",
        "RuntimeMaxUSec": "15min",
        "NoNewPrivileges": "yes",
        "CapabilityBoundingSet": "",
        "AmbientCapabilities": "",
        "Restart": "no",
        "KillMode": "control-group",
        "RuntimeDirectory": DIRECTORY.name,
        "RuntimeDirectoryMode": "0700",
    }
    names = [*expected, "MainPID", "InvocationID"]
    observed = dict(
        line.split("=", 1)
        for line in service_install.systemctl(
            "show", PARENT, "--property=" + ",".join(names)
        ).splitlines()
        if "=" in line
    )
    if (
        set(observed) != set(names)
        or any(observed[key] != value for key, value in expected.items())
        or not re.fullmatch(r"[1-9][0-9]{0,9}", observed.get("MainPID", ""))
        or not re.fullmatch(r"[0-9a-f]{32}", observed.get("InvocationID", ""))
        or observed["InvocationID"] == "0" * 32
    ):
        raise DeliveryError("bundle proof maintenance invocation unavailable")
    return {"pid": int(observed["MainPID"]), "invocation": observed["InvocationID"]}


def _read() -> tuple[dict, tuple[int, ...]]:
    preflight.protected(DIRECTORY, directory=True)
    directory = DIRECTORY.lstat()
    if directory.st_gid != 0 or stat.S_IMODE(directory.st_mode) != 0o700:
        raise DeliveryError("private maintenance proof directory required")
    preflight.protected(RECORD)
    before = RECORD.lstat()
    if before.st_gid != 0 or stat.S_IMODE(before.st_mode) != 0o400:
        raise DeliveryError("private maintenance proof file required")
    value = read_json(RECORD, MAX_RECORD)
    after = RECORD.lstat()
    if (before.st_dev, before.st_ino, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_ctime_ns,
    ):
        raise DeliveryError("maintenance proof changed while reading")
    validate(value)
    return value, (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_uid,
        before.st_gid,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )


def validate(value: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "bundle",
            "manifest_sha256",
            "release_id",
            "configuration_id",
            "metadata_sha256",
            "boot_id",
            "issued_boottime_ns",
            "pid",
            "invocation",
        },
        "maintenance bundle proof",
    )
    integer(value["schema_version"], 1, 1, "maintenance proof schema")
    integer(value["pid"], 1, 2**31 - 1, "maintenance proof PID")
    integer(value["issued_boottime_ns"], 0, 2**63 - 1, "maintenance proof time")
    for key, pattern in (
        ("release_id", r"sd-[0-9a-f]{12}"),
        ("configuration_id", r"cfg-[0-9a-f]{24}"),
        ("invocation", r"[0-9a-f]{32}"),
        ("boot_id", r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"),
    ):
        if not isinstance(value[key], str) or not re.fullmatch(pattern, value[key]):
            raise DeliveryError("maintenance proof identity differs")
    sha256(value["manifest_sha256"])
    sha256(value["metadata_sha256"])
    return value


def _live(value: dict) -> None:
    if (
        value["boot_id"] != _boot()
        or not 0 <= _now() - value["issued_boottime_ns"] <= MAX_AGE_NS
        or _manager(value["release_id"], value["configuration_id"])
        != {key: value[key] for key in ("pid", "invocation")}
    ):
        raise DeliveryError("maintenance byte proof expired or owner changed")


def _observed(value: dict) -> None:
    """One fixed journal event per process/invocation, never proof authority."""
    global _REPORTED
    owner = (os.getpid(), value["invocation"])
    if _REPORTED == owner:
        return
    message = (
        "KF complete byte proof reused manifest="
        + value["manifest_sha256"]
        + " invocation="
        + value["invocation"]
        + "\n"
    ).encode()
    if os.write(2, message) != len(message):
        raise DeliveryError("maintenance byte proof observation unavailable")
    _REPORTED = owner


def reuse(bundle: Path, expected: str) -> dict | None:
    """Missing/ineligible proofs use full bytes; eligible corrupt proofs refuse."""
    if not _root() or str(preflight.current_cgroup()) not in {GROUP + unit for unit in CONSUMERS}:
        return None
    try:
        RECORD.lstat()
    except FileNotFoundError:
        return None
    value, identity = _read()
    if value["bundle"] != str(bundle) or value["manifest_sha256"] != expected:
        raise DeliveryError("maintenance byte proof bundle differs")
    _live(value)
    preflight.protected(bundle, directory=True)
    from . import manifest

    before = manifest._bundle_signature(bundle)
    if _signature(before) != value["metadata_sha256"]:
        raise DeliveryError("maintenance byte proof metadata changed")
    metadata = bundle / "manifest.json"
    if digest(metadata) != expected:
        raise DeliveryError("bundle manifest fingerprint mismatch")
    data = manifest.validate(read_json(metadata))
    if data["release_id"] != value["release_id"]:
        raise DeliveryError("maintenance byte proof release differs")
    manifest._unchanged_bundle(bundle, before)
    reread, current = _read()
    if current != identity or reread != value:
        raise DeliveryError("maintenance byte proof replaced during use")
    _live(value)
    _observed(value)
    return data


@contextmanager
def publish(selected: dict, bundle: Path):
    """Read all bytes once; systemd removes the private directory at unit exit."""
    from . import install, manifest, operator_units

    operator_units.guarded("node-maintenance")
    if not _root() or str(preflight.current_cgroup()) != GROUP + PARENT:
        raise DeliveryError("maintenance byte proof requires its fixed owner")
    manager = _manager(selected["release_id"], selected["configuration_id"])
    if manager["pid"] != os.getpid():
        raise DeliveryError("only maintenance MainPID may publish byte proof")
    if install.selection(check_configuration=False) != selected:
        raise DeliveryError("maintenance proof selection changed")
    preflight.protected(DIRECTORY, directory=True)
    if RECORD.exists() or RECORD.is_symlink():
        raise DeliveryError("existing maintenance byte proof refused")
    created = None
    try:
        with manifest.verification_scope():
            preflight.protected(bundle, directory=True)
            before = manifest._bundle_signature(bundle)
            if any(stamp[4:6] != (0, 0) or stamp[2] & 0o022 for stamp in before.values()):
                raise DeliveryError("maintenance byte proof requires a protected Root tree")
            data = manifest.verify(bundle, selected["manifest_sha256"])
            manifest._unchanged_bundle(bundle, before)
            if data["release_id"] != selected["release_id"]:
                raise DeliveryError("maintenance byte proof release differs")
            value = {
                "schema_version": 1,
                "bundle": str(bundle),
                "manifest_sha256": selected["manifest_sha256"],
                "release_id": selected["release_id"],
                "configuration_id": selected["configuration_id"],
                "metadata_sha256": _signature(before),
                "boot_id": _boot(),
                "issued_boottime_ns": _now(),
                **manager,
            }
            contents = canonical(value)
            if len(contents) > MAX_RECORD:
                raise DeliveryError("maintenance byte proof exceeds bound")
            install.exclusive_file(RECORD, contents, 0o400)
            actual, created = _read()
            if actual != value:
                raise DeliveryError("maintenance byte proof publication differs")
            _live(value)
            yield
            if install.selection(check_configuration=False) != selected:
                raise DeliveryError("maintenance proof selection changed")
            _live(value)
            manifest._unchanged_bundle(bundle, before)
    finally:
        if created is not None:
            actual, identity = _read()
            if identity != created or actual != value:
                raise DeliveryError("foreign maintenance proof; refused cleanup")
            RECORD.unlink()
            install.sync_directory(DIRECTORY)
