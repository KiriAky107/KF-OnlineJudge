"""Private cold-backup directory format; never a public release or CI artifact."""

import json
import os
import re
import stat
from pathlib import Path

from .policy import (
    DeliveryError,
    digest,
    fields,
    integer,
    invalid_constant,
    sha256,
    unique_object,
)

FORMAT = "kf.private.cold-backup.v1"
MAX_BYTES = 7 * 1024**3
MAX_FILE_BYTES = 256 * 1024**2
MAX_ENTRIES = 50000
MAX_METADATA_BYTES = 16 * 1024**2
DOMAINS = {
    "postgres",
    "artifacts",
    "imports",
    "app-config",
    "judge-config",
    "factory",
    "operations",
    "releases",
    "images",
    "dependencies",
    "proxy",
}


def read_metadata(path: Path) -> dict:
    """Private inventories can exceed the public bundle's structure count.

    Byte, depth and entry bounds remain explicit before any restore/capture use.
    """
    with path.open("rb") as stream:
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise DeliveryError("private backup metadata byte limit")
    try:
        value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 32 or count > 1_000_000:
                raise DeliveryError("private backup metadata structure limit")
            if isinstance(item, dict):
                pending.extend((member, depth + 1) for member in item.values())
            elif isinstance(item, list):
                pending.extend((member, depth + 1) for member in item)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise DeliveryError("invalid private backup metadata") from error


def name(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise DeliveryError("invalid private backup member")
    parts = value.split("/")
    if parts[0] not in DOMAINS or any(
        p in {"", ".", "..", ".git", ".ssh", ".localplan", "docs"}
        or re.fullmatch(r"[A-Za-z0-9_.+@-]{1,255}", p) is None
        for p in parts
    ):
        raise DeliveryError("private backup member outside fixed domains")
    return value


def validate_references(value: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "kind",
            "read_only",
            "quiescence_verified",
            "boot_id",
            "storage_identity",
            "objects",
        },
        "private artifact references",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != "kf.private.artifact-backup-inventory"
        or value["read_only"] is not True
        or value["quiescence_verified"] is not False
        or not isinstance(value["objects"], list)
        or len(value["objects"]) > 65536
    ):
        raise DeliveryError("artifact backup inventory authority differs")
    from uuid import UUID

    if (
        not isinstance(value["boot_id"], str)
        or str(UUID(value["boot_id"])) != value["boot_id"]
        or UUID(value["boot_id"]).int == 0
    ):
        raise DeliveryError("artifact backup boot identity differs")
    identity = fields(
        value["storage_identity"],
        {"device", "inode", "uid"},
        "artifact storage identity",
    )
    integer(identity["device"], 1, 2**64 - 1, "artifact storage device")
    integer(identity["inode"], 1, 2**64 - 1, "artifact storage inode")
    if type(identity["uid"]) is not int or identity["uid"] != 1200:
        raise DeliveryError("artifact storage identity is not the platform owner")
    seen = set()
    for item in value["objects"]:
        fields(item, {"digest", "bytes"}, "private artifact reference")
        sha256(item["digest"])
        integer(item["bytes"], 0, 64 * 1024**2, "artifact reference bytes")
        if item["digest"] in seen:
            raise DeliveryError("duplicate backup artifact reference")
        seen.add(item["digest"])
    return value


def validate(value: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "format",
            "backup_id",
            "source_sha",
            "manifest_sha256",
            "configuration_id",
            "created_unix_seconds",
            "expires_unix_seconds",
            "consistent_cut",
            "selected",
            "artifact_references",
            "images",
            "directories",
            "files",
            "payload_bytes",
            "restored",
            "execution_verified",
        },
        "private backup",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["format"] != FORMAT
        or not isinstance(value["backup_id"], str)
        or re.fullmatch(r"bk-[0-9a-f]{32}", value["backup_id"]) is None
        or not isinstance(value["source_sha"], str)
        or re.fullmatch(r"[0-9a-f]{40}", value["source_sha"]) is None
        or not isinstance(value["configuration_id"], str)
        or re.fullmatch(r"cfg-[0-9a-f]{24}", value["configuration_id"]) is None
        or value["restored"] is not False
        or value["execution_verified"] is not False
    ):
        raise DeliveryError("private backup identity or authority differs")
    sha256(value["manifest_sha256"])
    start = integer(value["created_unix_seconds"], 1, 2**63 - 1, "backup time")
    end = integer(value["expires_unix_seconds"], start + 1, start + 7 * 86400, "backup retention")
    if end - start != 7 * 86400:
        raise DeliveryError("fixed seven-day private backup retention required")
    from .lifecycle import projection_empty, validate_record

    cut = validate_record(value["consistent_cut"])
    if (
        cut["source_sha"] != value["source_sha"]
        or cut["configuration_id"] != value["configuration_id"]
        or cut["action"] not in {"drain", "stop"}
        or cut["phase"] != "complete"
        or cut["success"] is not True
        or cut["root_cleanup_confirmed"] is not True
        or "drain_projection" not in cut
        or not projection_empty(cut["drain_projection"])
    ):
        raise DeliveryError("cold backup lacks a confirmed owner-derived cut")
    selected = fields(
        value["selected"],
        {
            "schema_version",
            "release_id",
            "manifest_sha256",
            "configuration_id",
            "receipt_sha256",
        },
        "backup selected binding",
    )
    if (
        type(selected["schema_version"]) is not int
        or selected["schema_version"] != 1
        or selected["release_id"] != "sd-" + value["source_sha"][:12]
        or selected["manifest_sha256"] != value["manifest_sha256"]
        or selected["configuration_id"] != value["configuration_id"]
    ):
        raise DeliveryError("backup selected binding differs")
    sha256(selected["receipt_sha256"])
    validate_references(value["artifact_references"])
    if value["artifact_references"]["boot_id"] != cut["boot_id"]:
        raise DeliveryError("artifact snapshot and cold cut are from different boots")
    fields(value["images"], {"gnu", "python313", "java21"}, "backup image identities")
    for identity in value["images"].values():
        fields(
            identity,
            {
                "toolchain_id",
                "image_kind",
                "image_revision",
                "runtime_version",
                "image_digest",
            },
            "backup runtime identity",
        )
        sha256(identity["image_digest"])
        integer(identity["image_revision"], 1, 2**32 - 1, "backup image revision")
        for key in ("toolchain_id", "image_kind", "runtime_version"):
            if (
                not isinstance(identity[key], str)
                or re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", identity[key]) is None
            ):
                raise DeliveryError("backup runtime label differs")
    dirs, files = value["directories"], value["files"]
    if (
        not isinstance(dirs, list)
        or not isinstance(files, list)
        or not 1 <= len(dirs) + len(files) <= MAX_ENTRIES
    ):
        raise DeliveryError("backup inventory count exceeds bound")
    seen = set()
    for item in dirs:
        fields(item, {"path", "uid", "gid", "mode"}, "backup directory")
        _member(item, seen, directory=True)
    total = 0
    for item in files:
        fields(item, {"path", "uid", "gid", "mode", "bytes", "sha256"}, "backup file")
        _member(item, seen, directory=False)
        total += integer(item["bytes"], 0, MAX_FILE_BYTES, "backup file bytes")
        sha256(item["sha256"])
    if (
        type(value["payload_bytes"]) is not int
        or value["payload_bytes"] != total
        or total > MAX_BYTES
    ):
        raise DeliveryError("private backup aggregate bytes differ")
    directories = {item["path"] for item in dirs}
    if not DOMAINS <= directories:
        raise DeliveryError("private backup omitted a required source domain")
    for path in seen:
        for parent in Path(path).parents:
            if parent.as_posix() != "." and parent.as_posix() not in directories:
                raise DeliveryError("private backup parent missing")
    lookup = {item["path"]: item for item in files}
    for reference in value["artifact_references"]["objects"]:
        item = lookup.get("artifacts/" + reference["digest"])
        if (
            item is None
            or item["bytes"] != reference["bytes"]
            or item["sha256"] != reference["digest"]
        ):
            raise DeliveryError("registered source or testcase absent from backup")
    if "postgres/PG_VERSION" not in lookup or "postgres/global/pg_control" not in lookup:
        raise DeliveryError("private backup lacks a cold PostgreSQL cluster")
    return value


def _member(item: dict, seen: set[str], *, directory: bool) -> None:
    path = name(item["path"])
    if path in seen:
        raise DeliveryError("duplicate private backup member")
    seen.add(path)
    integer(item["uid"], 0, 1205, "backup member uid")
    integer(item["gid"], 0, 1205, "backup member gid")
    if item["uid"] not in {0, 1200, 1201, 1202, 1203, 1204, 1205} or (
        item["gid"] not in ({0, 1203, 1204, 1205} if item["uid"] == 0 else {item["uid"]})
    ):
        raise DeliveryError("unsupported backup OS identity")
    mode = item["mode"]
    if type(mode) is not int or mode not in (
        {0o555, 0o700, 0o711, 0o755}
        if directory
        else {0o400, 0o444, 0o600, 0o640, 0o644, 0o555, 0o755}
    ):
        raise DeliveryError("backup member privilege bits differ")


def verify(root: Path, expected: str) -> dict:
    """Expected digest is a separate private operator receipt, never self-trust."""
    from . import configuration, preflight

    preflight.protected(root, directory=True)
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise DeliveryError("private backup directory required")
    path = root / "manifest.json"
    configuration.private(path)
    if digest(path) != sha256(expected) or {p.name for p in root.iterdir()} != {
        "manifest.json",
        "payload",
    }:
        raise DeliveryError("private backup receipt or layout differs")
    value = validate(read_metadata(path))
    found_files, found_dirs = set(), set()
    payload = root / "payload"
    preflight.protected(payload, directory=True)
    if stat.S_IMODE(payload.stat().st_mode) != 0o700:
        raise DeliveryError("private backup payload directory required")
    for directory, names, files in os.walk(payload, followlinks=False):
        for member in names + files:
            p = Path(directory) / member
            info = p.lstat()
            relative = name(p.relative_to(payload).as_posix())
            if info.st_uid != 0 or info.st_gid != 0 or p.is_symlink():
                raise DeliveryError("private backup member ownership differs")
            if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700:
                found_dirs.add(relative)
            elif (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
            ):
                found_files.add(relative)
            else:
                raise DeliveryError("private backup special or non-private member")
            if len(found_files) + len(found_dirs) > MAX_ENTRIES:
                raise DeliveryError("private backup traversal exceeds bound")
    if found_dirs != {x["path"] for x in value["directories"]} or found_files != {
        x["path"] for x in value["files"]
    }:
        raise DeliveryError("private backup inventory differs")
    for item in value["files"]:
        p = payload / item["path"]
        if p.stat().st_size != item["bytes"] or digest(p) != item["sha256"]:
            raise DeliveryError("private backup byte verification failed")
    return value
