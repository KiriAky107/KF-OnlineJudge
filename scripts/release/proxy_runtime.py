"""Closed public NGINX runtime inventory, independent from TLS and service state."""

import re
import stat
from pathlib import Path

from .policy import DeliveryError, digest, fields, integer, read_json, sha256

VERSION = "1.30.5"
MAX_BYTES = 64 * 1024**2


def validate(value: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "format",
            "version",
            "architecture",
            "package_sha256",
            "files",
            "directories",
            "payload_bytes",
        },
        "proxy runtime",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["format"] != "kf.nginx.runtime.v1"
        or value["version"] != VERSION
        or value["architecture"] != "x86_64"
        or value["directories"] != ["bin", "lib"]
        or not isinstance(value["files"], list)
        or not 1 <= len(value["files"]) <= 32
    ):
        raise DeliveryError("unsupported proxy runtime profile")
    sha256(value["package_sha256"])
    names = set()
    total = 0
    for entry in value["files"]:
        fields(entry, {"path", "bytes", "sha256", "mode"}, "proxy runtime file")
        name = entry["path"]
        if not isinstance(name, str) or (
            name != "bin/nginx" and not re.fullmatch(r"lib/[A-Za-z0-9_.+-]+\.so(?:\.[0-9]+)*", name)
        ):
            raise DeliveryError("non-runtime or private proxy member")
        if name in names or entry["mode"] != ("0555" if name == "bin/nginx" else "0444"):
            raise DeliveryError("duplicate proxy member or unsafe mode")
        names.add(name)
        total += integer(entry["bytes"], 1, 32 * 1024**2, "proxy file bytes")
        sha256(entry["sha256"])
    if (
        not {"bin/nginx", "lib/libc.so.6", "lib/ld-linux-x86-64.so.2"} <= names
        or total > MAX_BYTES
        or type(value["payload_bytes"]) is not int
        or value["payload_bytes"] != total
    ):
        raise DeliveryError("incomplete or unbounded proxy runtime")
    return value


def verify(root: Path, expected: str) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise DeliveryError("ordinary proxy runtime directory required")
    catalog = root / "manifest.json"
    if catalog.is_symlink() or digest(catalog) != sha256(expected):
        raise DeliveryError("proxy runtime out-of-band fingerprint differs")
    value = validate(read_json(catalog, 16384))
    if {p.name for p in root.iterdir()} != {"manifest.json", "bin", "lib"}:
        raise DeliveryError("unexpected proxy runtime tree member")
    for directory in (root / "bin", root / "lib"):
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or stat.S_IMODE(directory.stat().st_mode) != 0o555
        ):
            raise DeliveryError("readonly ordinary proxy runtime directories required")
    names = set()
    for directory in (root / "bin", root / "lib"):
        for p in directory.iterdir():
            names.add(p.relative_to(root).as_posix())
    if names != {entry["path"] for entry in value["files"]}:
        raise DeliveryError("proxy runtime inventory differs")
    for entry in value["files"]:
        p = root / entry["path"]
        info = p.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != entry["bytes"]
            or stat.S_IMODE(info.st_mode) != int(entry["mode"], 8)
            or digest(p) != entry["sha256"]
        ):
            raise DeliveryError("proxy runtime bytes or permission differ")
    return value


def copy_to_payload(root: Path, expected: str, destination: Path) -> None:
    from .build import copy

    value = verify(root, expected)
    if destination.exists() or destination.is_symlink():
        raise DeliveryError("proxy runtime payload must be new")
    destination.mkdir()
    for directory in value["directories"]:
        (destination / directory).mkdir()
    for entry in value["files"]:
        copy(root / entry["path"], destination / entry["path"])
    copy(root / "manifest.json", destination / "manifest.json")
