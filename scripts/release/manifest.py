"""Explicit directory bundle inventories, verified before installation."""

import os
import re
import stat
from pathlib import Path

from .policy import (
    FORMAT,
    MAX_BUNDLE_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_METADATA_BYTES,
    PLATFORM,
    TOOLCHAINS,
    DeliveryError,
    canonical,
    digest,
    fields,
    integer,
    read_json,
    relative_name,
    sha256,
)


def inventory(root: Path) -> list[dict]:
    """No links/devices or inferred inclusion; bounds apply before hashing."""
    if not root.is_dir() or root.is_symlink():
        raise DeliveryError("payload root must be an ordinary directory")
    entries = []
    total = 0
    directory_count = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        directory_count += len(directories)
        if directory_count + len(entries) + len(files) > MAX_FILES:
            raise DeliveryError("payload entry count limit")
        for name in directories:
            path = Path(directory) / name
            if path.is_symlink():
                raise DeliveryError("payload directory link refused")
        for name in sorted(files):
            path = Path(directory) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise DeliveryError("payload must contain single-link regular files")
            if stat.S_IMODE(info.st_mode) not in (0o444, 0o555):
                raise DeliveryError("payload file must be read-only")
            size = integer(info.st_size, 0, MAX_FILE_BYTES, "payload file size")
            total += size
            if total > MAX_BUNDLE_BYTES or len(entries) >= MAX_FILES:
                raise DeliveryError("payload aggregate limit")
            relative = relative_name(path.relative_to(root).as_posix())
            entries.append(
                {
                    "path": relative,
                    "bytes": size,
                    "sha256": digest(path),
                    "mode": "0555" if info.st_mode & 0o111 else "0444",
                }
            )
    return sorted(entries, key=lambda x: x["path"])


def directories(root: Path) -> list[str]:
    result = []
    for directory, names, _ in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            value = path.relative_to(root).as_posix()
            relative_name(value + "/.__directory__")
            if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o555:
                raise DeliveryError("payload directories must be read-only ordinary directories")
            result.append(value)
            if len(result) > MAX_FILES:
                raise DeliveryError("payload directory count limit")
    return sorted(result)


def validate(data: dict) -> dict:
    fields(
        data,
        {
            "schema_version",
            "format",
            "release_id",
            "source_sha",
            "platform",
            "toolchains",
            "locks",
            "images",
            "files",
            "directories",
            "payload_bytes",
        },
        "bundle",
    )
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or data["format"] != FORMAT
    ):
        raise DeliveryError("unsupported bundle format")
    if not isinstance(data["source_sha"], str) or not re.fullmatch(
        r"[0-9a-f]{40}", data["source_sha"]
    ):
        raise DeliveryError("invalid source SHA")
    if (
        not isinstance(data["release_id"], str)
        or data["release_id"] != "sd-" + data["source_sha"][:12]
    ):
        raise DeliveryError("release identity mismatch")
    if data["platform"] != PLATFORM or data["toolchains"] != TOOLCHAINS:
        raise DeliveryError("bundle platform/toolchain mismatch")
    fields(
        data["locks"],
        {
            "backend/uv.lock",
            "judge/Cargo.lock",
            "frontend/pnpm-lock.yaml",
            "deploy/release/contract.v1.json",
        },
        "lock fingerprints",
    )
    for value in data["locks"].values():
        sha256(value)
    fields(data["images"], {"gnu", "python313", "java21"}, "runtime images")
    for image in data["images"].values():
        fields(
            image,
            {"toolchain_id", "image_kind", "image_revision", "runtime_version", "image_digest"},
            "runtime identity",
        )
        sha256(image["image_digest"])
        integer(image["image_revision"], 1, 2**32 - 1, "image revision")
        for name in ("toolchain_id", "image_kind", "runtime_version"):
            if not isinstance(image[name], str) or not re.fullmatch(
                r"[A-Za-z0-9_.+-]{1,128}", image[name]
            ):
                raise DeliveryError("invalid runtime identity")
    runtimes = {
        "gnu": ("gnu-cxx-v1", "linux_gnu_cxx_x86_64_v1", None),
        "python313": ("cpython313-v1", "linux_cpython313_x86_64_v1", "3.13.16"),
        "java21": ("openjdk21-v1", "linux_openjdk21_x86_64_v1", "21.0.12.1+1-LTS"),
    }
    for name, (toolchain, kind, version) in runtimes.items():
        image = data["images"][name]
        if (
            image["toolchain_id"] != toolchain
            or image["image_kind"] != kind
            or (version is not None and image["runtime_version"] != version)
        ):
            raise DeliveryError("unsupported runtime image")
    files = data["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise DeliveryError("bundle file count")
    seen = set()
    total = 0
    for entry in files:
        fields(entry, {"path", "bytes", "sha256", "mode"}, "bundle file")
        path = relative_name(entry["path"])
        if path in seen or entry["mode"] not in {"0444", "0555"}:
            raise DeliveryError("duplicate file or unsafe mode")
        seen.add(path)
        total += integer(entry["bytes"], 0, MAX_FILE_BYTES, "bundle file size")
        sha256(entry["sha256"])
    if (
        total > MAX_BUNDLE_BYTES
        or type(data["payload_bytes"]) is not int
        or data["payload_bytes"] != total
    ):
        raise DeliveryError("bundle byte total")
    required = {
        "bin/kf-sandbox",
        "bin/kf-worker",
        "bin/kf-coordinator",
        "python/bin/python3.13",
        "backend/alembic.ini",
        "frontend/index.html",
        "frontend/admin/index.html",
        "deploy/release/contract.v1.json",
    }
    required.update(f"images/{name}/manifest.json" for name in data["images"])
    if not required <= seen:
        raise DeliveryError("incomplete application bundle")
    directory_names = data["directories"]
    if not isinstance(directory_names, list) or len(directory_names) + len(files) > MAX_FILES:
        raise DeliveryError("bundle directory count")
    directory_set = set()
    for name in directory_names:
        if not isinstance(name, str):
            raise DeliveryError("invalid bundle directory")
        relative_name(name + "/.__directory__")
        if name in directory_set or name in seen:
            raise DeliveryError("duplicate bundle directory")
        directory_set.add(name)
    for name in seen | directory_set:
        for parent in Path(name).parents:
            if parent.as_posix() != "." and parent.as_posix() not in directory_set:
                raise DeliveryError("missing payload parent directory")
    return data


def verify(bundle: Path, expected_sha256: str) -> dict:
    """The manifest digest comes from a separate trusted operator receipt."""
    expected_sha256 = sha256(expected_sha256)
    if not bundle.is_dir() or bundle.is_symlink():
        raise DeliveryError("bundle directory required")
    metadata = bundle / "manifest.json"
    info = metadata.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_METADATA_BYTES:
        raise DeliveryError("unsafe manifest file")
    if digest(metadata) != expected_sha256:
        raise DeliveryError("bundle manifest fingerprint mismatch")
    if set(p.name for p in bundle.iterdir()) != {"manifest.json", "payload"}:
        raise DeliveryError("unexpected bundle member")
    data = validate(read_json(metadata))
    if inventory(bundle / "payload") != sorted(data["files"], key=lambda x: x["path"]):
        raise DeliveryError("bundle contents differ from manifest")
    if directories(bundle / "payload") != sorted(data["directories"]):
        raise DeliveryError("bundle directories differ from manifest")
    for name, identity in data["images"].items():
        if digest(bundle / "payload/images" / name / "manifest.json") != identity["image_digest"]:
            raise DeliveryError("image catalog differs from runtime identity")
    if (
        digest(bundle / "payload/deploy/release/contract.v1.json")
        != data["locks"]["deploy/release/contract.v1.json"]
    ):
        raise DeliveryError("delivery contract fingerprint mismatch")
    return data


def write(bundle: Path, source_sha: str, locks: dict, images: dict) -> dict:
    files = inventory(bundle / "payload")
    data = validate(
        {
            "schema_version": 1,
            "format": FORMAT,
            "release_id": "sd-" + source_sha[:12],
            "source_sha": source_sha,
            "platform": PLATFORM,
            "toolchains": TOOLCHAINS,
            "locks": locks,
            "images": images,
            "files": files,
            "directories": directories(bundle / "payload"),
            "payload_bytes": sum(x["bytes"] for x in files),
        }
    )
    raw = canonical(data)
    if len(raw) > MAX_METADATA_BYTES:
        raise DeliveryError("bundle manifest exceeds metadata limit")
    with (bundle / "manifest.json").open("xb") as stream:
        stream.write(raw)
    (bundle / "manifest.json").chmod(0o444)
    return data
