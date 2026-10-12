"""Versioned delivery bounds, separate from the judging wire protocols."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_FILES = 50000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_BUNDLE_BYTES = 4 * 1024**3
FORMAT = "kf.single-node.directory.v1"
PLATFORM = "linux_x86_64_v3_glibc244_systemd"
TOOLCHAINS = {"python": "3.13.16", "rust": "1.99.0", "node": "26.10.0", "pnpm": "11.28.4"}
ROOTS = (
    "backend/",
    "python/",
    "site/",
    "lib/",
    "bin/",
    "frontend/",
    "images/",
    "deploy/",
    "delivery/",
)
FORBIDDEN = {".git", ".localplan", "docs", "__pycache__", ".env", ".ssh"}


class DeliveryError(ValueError):
    """An intentionally bounded, non-secret error for the operator."""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sha256(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise DeliveryError("invalid SHA-256")
    if value == "a" * 64 or value == "0" * 64:
        raise DeliveryError("placeholder SHA-256 refused")
    return value


def fields(value: Any, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise DeliveryError(f"invalid {label} fields")
    return value


def integer(value: Any, lower: int, upper: int, label: str) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise DeliveryError(f"invalid {label}")
    return value


def relative_name(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise DeliveryError("invalid payload path")
    if not all(re.fullmatch(r"[A-Za-z0-9_.+@-]{1,255}", p) for p in value.split("/")):
        raise DeliveryError("noncanonical payload path")
    if any(p in {".", "..", *FORBIDDEN} for p in value.split("/")):
        raise DeliveryError("private or unsafe payload path")
    if not value.startswith(ROOTS) or value.endswith((".pyc", ".pyo")):
        raise DeliveryError("payload path outside delivery allowlist")
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeliveryError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path: Path, maximum: int = MAX_METADATA_BYTES) -> dict:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise DeliveryError("metadata size limit")
    try:
        value = json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 32 or count > 400000:
                raise DeliveryError("metadata structure limit")
            if isinstance(item, dict):
                pending.extend((member, depth + 1) for member in item.values())
            elif isinstance(item, list):
                pending.extend((member, depth + 1) for member in item)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise DeliveryError("invalid JSON metadata") from error


def invalid_constant(_value: str):
    raise DeliveryError("nonfinite JSON value refused")


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def contract(path: Path) -> dict:
    data = read_json(path, 16384)
    fields(
        data,
        {
            "schema_version",
            "format",
            "platform",
            "toolchains",
            "capacity",
            "layout",
            "services",
            "recovery",
        },
        "delivery contract",
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise DeliveryError("unsupported delivery contract")
    if data["format"] != FORMAT or data["platform"] != PLATFORM or data["toolchains"] != TOOLCHAINS:
        raise DeliveryError("unsupported delivery target or toolchain")
    capacity = fields(
        data["capacity"],
        {"memory_bytes", "disk_bytes", "execution_slots", "import_slots"},
        "capacity",
    )
    for name, value in capacity.items():
        integer(value, 1, 16 * 1024**3, name)
    if capacity != {
        "memory_bytes": 4 * 1024**3,
        "disk_bytes": 12 * 1024**3,
        "execution_slots": 1,
        "import_slots": 1,
    }:
        raise DeliveryError("delivery capacity mismatch")
    layout = fields(
        data["layout"],
        {
            "releases",
            "current",
            "configuration",
            "data",
            "judge_configuration",
            "judge_state",
            "task_cgroup",
        },
        "layout",
    )
    if layout != {
        "releases": "/opt/kf-onlinejudge/releases",
        "current": "/opt/kf-onlinejudge/current",
        "configuration": "/etc/kf-onlinejudge",
        "data": "/var/lib/kf-onlinejudge",
        "judge_configuration": "/etc/kf-judge",
        "judge_state": "/var/lib/kf-judge",
        "task_cgroup": "/sys/fs/cgroup/kf-judge-tasks",
    }:
        raise DeliveryError("delivery layout mismatch")
    expected_services = {
        "api": "platform",
        "coordinator": "coordinator",
        "outbox": "platform",
        "problem-import": "problem_import",
        "worker": "none",
        "sandbox-factory": "none",
        "identity-operator": "identity_operator",
        "migration": "migration_owner",
    }
    if data["services"] != expected_services:
        raise DeliveryError("service authority mismatch")
    fields(
        data["recovery"], {"consistent_cut_rpo_seconds", "prepared_restore_rto_seconds"}, "recovery"
    )
    for name, value in data["recovery"].items():
        integer(value, 0, 3600, name)
    if data["recovery"] != {"consistent_cut_rpo_seconds": 0, "prepared_restore_rto_seconds": 1800}:
        raise DeliveryError("recovery contract mismatch")
    return data
