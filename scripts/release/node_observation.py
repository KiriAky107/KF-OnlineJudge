"""Finite read-only owner/kernel observation; no execution or admission mutation."""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import install, journal, node_health, operator_units, preflight, services
from .policy import DeliveryError, canonical, digest, integer, read_json

NODE = Path("/sys/fs/cgroup/kfonlinejudge.slice")
UNITS = (*services.PROFILES, "postgres", "rabbitmq", "sandbox-factory", "proxy")
CPU_FIELDS = {
    "usage_usec",
    "user_usec",
    "system_usec",
    "nr_periods",
    "nr_throttled",
    "throttled_usec",
}
MEMORY_FIELDS = {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"}
IO_FIELDS = {"rbytes", "wbytes", "rios", "wios"}


def text(path: Path, maximum: int = 4096) -> str:
    with path.open() as source:
        value = source.read(maximum + 1)
    if len(value) > maximum:
        raise DeliveryError("kernel observation byte limit")
    return value.strip()


def number(value: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,19}", value) is None:
        raise DeliveryError("invalid kernel counter")
    return integer(int(value), 0, 2**63 - 1, "kernel counter")


def counters(value: str, selected: set[str]) -> dict:
    result = {}
    lines = value.splitlines()
    if not 1 <= len(lines) <= 32:
        raise DeliveryError("kernel counter inventory limit")
    for line in lines:
        parts = line.split()
        if len(parts) != 2 or parts[0] in result:
            raise DeliveryError("invalid kernel counter row")
        result[parts[0]] = number(parts[1])
    if not selected <= result.keys():
        raise DeliveryError("required kernel counter absent")
    return {key: result[key] for key in sorted(selected)}


def io_totals(value: str) -> dict:
    """Bound the device inventory and omit device identities from the output."""
    lines = value.splitlines()
    if len(lines) > 16:
        raise DeliveryError("kernel IO device inventory limit")
    totals = dict.fromkeys(sorted(IO_FIELDS), 0)
    devices = set()
    for line in lines:
        parts = line.split()
        if not parts or re.fullmatch(r"[0-9]{1,10}:[0-9]{1,10}", parts[0]) is None:
            raise DeliveryError("invalid kernel IO device row")
        if parts[0] in devices or len(parts) > 16:
            raise DeliveryError("duplicate or excessive kernel IO row")
        devices.add(parts[0])
        if len(parts) == 1:
            # Linux omits all read/write counters for an idle device.
            continue
        values = {}
        for member in parts[1:]:
            key, separator, raw = member.partition("=")
            if not separator or key in values:
                raise DeliveryError("invalid kernel IO counter")
            values[key] = number(raw)
        if not IO_FIELDS <= values.keys():
            raise DeliveryError("required kernel IO counter absent")
        for key in totals:
            totals[key] = integer(totals[key] + values[key], 0, 2**63 - 1, "total kernel IO")
    return {"devices_observed": len(devices), **totals}


def kernel(path: Path) -> dict:
    preflight.protected(path, directory=True)
    return {
        "memory_current_bytes": number(text(path / "memory.current")),
        "memory_peak_bytes": number(text(path / "memory.peak")),
        "pids_current": number(text(path / "pids.current")),
        "pids_peak": number(text(path / "pids.peak")),
        "memory_events": counters(text(path / "memory.events"), MEMORY_FIELDS),
        "cpu": counters(text(path / "cpu.stat"), CPU_FIELDS),
        "io": io_totals(text(path / "io.stat", 8192)),
        "limits": {
            key: text(path / key, 64)
            for key in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
        },
    }


def filesystem(path: Path) -> dict:
    # Aggregate filesystem figures never enumerate source/hidden/credential files.
    if path.is_symlink() or not path.is_dir():
        raise DeliveryError("ordinary observation storage directory required")
    value = os.statvfs(path)
    return {
        "capacity_bytes": value.f_frsize * value.f_blocks,
        "available_bytes": value.f_frsize * value.f_bavail,
        "used_bytes": value.f_frsize * (value.f_blocks - value.f_bfree),
        "inodes_total": value.f_files,
        "inodes_free": value.f_favail,
        "read_only": bool(value.f_flag & os.ST_RDONLY),
    }


def observe() -> dict:
    operator_units.guarded("node-observation")
    selected, _, payload = node_health.binding()
    for name in ("node_observation.py", "operator_units.py"):
        shipped = payload / "delivery/scripts/release" / name
        preflight.protected(shipped)
        if digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("observation program differs from selected delivery")
    started = time.monotonic()
    result = {
        "schema_version": 1,
        "kind": "kf.node.read-only-observation",
        "source_sha": read_json(payload.parent / "manifest.json")["source_sha"],
        "configuration_id": selected["configuration_id"],
        "observed_unix_ms": time.time_ns() // 1_000_000,
        "read_only": True,
        "isolation_verified": False,
        "complete": False,
        "kernel": {},
        "filesystems": {},
        "owner_health": None,
        "errors": [],
    }
    for label, path in [
        ("node", NODE),
        *[(name, NODE / ("kf-" + name + ".service")) for name in UNITS],
        ("tasks", NODE / "kf-judge-tasks"),
    ]:
        try:
            result["kernel"][label] = kernel(path)
        except (OSError, ValueError):
            result["errors"].append("kernel_" + label + "_unavailable")
    paths = {
        "root": Path("/var/lib/kf-onlinejudge"),
        "artifacts": Path("/var/lib/kf-onlinejudge/artifacts"),
        "imports": Path("/var/lib/kf-onlinejudge/imports"),
        "postgres": Path("/var/lib/kf-onlinejudge/postgres"),
        "rabbitmq": Path("/var/lib/kf-onlinejudge/rabbitmq"),
        "proxy": Path("/var/lib/kf-onlinejudge/proxy"),
        "backups": Path("/var/lib/kf-onlinejudge/backups"),
    }
    try:
        paths["journal"] = Path("/var/log/journal") / (
            journal.machine_id() + "." + journal.NAMESPACE
        )
    except (OSError, ValueError):
        result["errors"].append("filesystem_journal_unavailable")
    for label, path in paths.items():
        try:
            result["filesystems"][label] = filesystem(path)
        except (OSError, ValueError):
            result["errors"].append("filesystem_" + label + "_unavailable")
    try:
        # Owner status/ACL/MQ/factory observers run under their existing fixed
        # identities. Notification probing is off: no publish/ack/claim/cleanup.
        health = operator_units.run("node-health", payload)
        if (
            health.get("source_sha") != result["source_sha"]
            or health.get("configuration_id") != selected["configuration_id"]
        ):
            raise DeliveryError("owner health observation binding differs")
        result["owner_health"] = health
        if health.get("errors"):
            result["errors"].append("owner_observation_incomplete")
    except (OSError, ValueError, subprocess.SubprocessError):
        result["errors"].append("owner_observation_unavailable")
    if install.selection(check_configuration=False) != selected:
        result["errors"].append("selected_binding_changed")
    result["sample_duration_ms"] = int((time.monotonic() - started) * 1000)
    result["complete"] = not result["errors"]
    if len(canonical(result)) > 32768:
        raise DeliveryError("read-only observation result exceeds output bound")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Finite read-only owner and kernel observation")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.inside:
            value = observe()
        else:
            selected = install.selection()
            value = operator_units.run(
                "node-observation", install.RELEASES / selected["release_id"] / "payload"
            )
        print(json.dumps(value, sort_keys=True))
        if not args.inside and not value["complete"]:
            raise SystemExit(2)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, "Read-only node observation unavailable: " + type(error).__name__ + "\n")


if __name__ == "__main__":
    main()
