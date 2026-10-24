"""Explicit directory bundle inventories, verified before installation."""

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

from . import file_copy
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


@dataclass(slots=True)
class _ReadScope:
    pid: int
    entries: dict = field(default_factory=dict)
    nodes: int = 0
    metadata_bytes: int = 0


_READ_SCOPE: ContextVar[_ReadScope | None] = ContextVar("delivery_bundle_reads", default=None)
_MAX_SCOPED_BUNDLES = 4
_MAX_SCOPED_NODES = 2 * (MAX_FILES + 3)
_MAX_SCOPED_METADATA = 2 * MAX_METADATA_BYTES


def _bundle_signature(bundle: Path) -> dict[str, tuple[int, ...]]:
    """Observe topology and all metadata that can invalidate a prior byte read."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(bundle, flags)
    members = {}
    try:
        root = os.fstat(descriptor)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                members[entry.name] = entry.stat(follow_symlinks=False)
                if len(members) > 2:
                    raise DeliveryError("unexpected bundle member")
    finally:
        os.close(descriptor)
    if set(members) != {"manifest.json", "payload"}:
        raise DeliveryError("unexpected bundle member")
    result = {}

    def identity(info: os.stat_result) -> tuple[int, ...]:
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

    def add(name: str, info: os.stat_result, *, directory: bool) -> None:
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise DeliveryError("bundle proof requires ordinary directories and files")
        if not directory and info.st_nlink != 1:
            raise DeliveryError("bundle proof requires single-link files")
        result[name] = identity(info)
        if len(result) > MAX_FILES + 3:
            raise DeliveryError("bundle proof entry count limit")

    add(".", root, directory=True)
    add("manifest.json", members["manifest.json"], directory=False)
    payload = bundle / "payload"
    add("payload", members["payload"], directory=True)
    pending = [(str(payload), "", result["payload"])]
    while pending:
        directory, prefix, expected = pending.pop()
        # Close each iterator before visiting another directory. Wide or deep
        # verified inventories never require one open descriptor per directory.
        descriptor = os.open(directory, flags)
        try:
            if identity(os.fstat(descriptor)) != expected:
                raise DeliveryError("verified bundle changed during the operator operation")
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    name = prefix + entry.name
                    info = entry.stat(follow_symlinks=False)
                    is_directory = stat.S_ISDIR(info.st_mode)
                    relative_name(name + "/.__directory__" if is_directory else name)
                    add("payload/" + name, info, directory=is_directory)
                    if is_directory:
                        pending.append(
                            (
                                os.path.join(directory, entry.name),
                                name + "/",
                                identity(info),
                            )
                        )
        finally:
            os.close(descriptor)
    return result


def _unchanged_bundle(bundle: Path, signature: dict[str, tuple[int, ...]]) -> None:
    if _bundle_signature(bundle) != signature:
        raise DeliveryError("verified bundle changed during the operator operation")


@contextmanager
def verification_scope():
    """Reuse actual byte reads only inside one synchronous same-PID operation."""
    previous = _READ_SCOPE.get()
    if previous is not None and previous.pid == os.getpid():
        yield
        return
    current = _ReadScope(os.getpid())
    token = _READ_SCOPE.set(current)
    try:
        yield
        # This happens before the decorated operation returns its success value.
        for (bundle, _), (_, signature) in current.entries.items():
            _unchanged_bundle(bundle, signature)
    finally:
        _READ_SCOPE.reset(token)


def verified_operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with verification_scope():
            return function(*args, **kwargs)

    return wrapped


def _file_stamp(info: os.stat_result) -> tuple[int, ...]:
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


def _read_payload(row: tuple[Path, tuple[int, ...], dict]) -> dict:
    path, expected, record = row
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        if _file_stamp(os.fstat(stream.fileno())) != expected:
            raise DeliveryError("payload file changed before byte verification")
        checked = hashlib.file_digest(stream, "sha256").hexdigest()
        after = _file_stamp(os.fstat(stream.fileno()))
    if after != expected or _file_stamp(path.lstat()) != expected:
        raise DeliveryError("payload file changed during byte verification")
    return {**record, "sha256": checked}


def inventory(root: Path, *, path_prefix: str = "") -> list[dict]:
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
            relative = path.relative_to(root).as_posix()
            relative_name(path_prefix + relative)
            entries.append(
                (
                    path,
                    _file_stamp(info),
                    {
                        "path": relative,
                        "bytes": size,
                        "mode": "0555" if info.st_mode & 0o111 else "0444",
                    },
                )
            )
    # The same finite four-call scheduler used for durable file copies also
    # bounds reads. It joins every dispatched reader before returning or raising.
    verified = file_copy.bounded_copies(entries, _read_payload)
    for path, expected, _ in entries:
        if _file_stamp(path.lstat()) != expected:
            raise DeliveryError("payload file changed before inventory completion")
    return sorted(verified, key=lambda x: x["path"])


def directories(root: Path, *, path_prefix: str = "") -> list[str]:
    result = []
    for directory, names, _ in os.walk(root, followlinks=False):
        for name in names:
            path = Path(directory) / name
            value = path.relative_to(root).as_posix()
            relative_name(path_prefix + value + "/.__directory__")
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
            {
                "toolchain_id",
                "image_kind",
                "image_revision",
                "runtime_version",
                "image_digest",
            },
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
        # Names are already canonical; walk their lexical parent components.
        parent, separator, _ = name.rpartition("/")
        while separator:
            if parent not in directory_set:
                raise DeliveryError("missing payload parent directory")
            parent, separator, _ = parent.rpartition("/")
    return data


def verify(bundle: Path, expected_sha256: str) -> dict:
    """A bounded operation may reuse a complete byte proof only while unchanged."""
    from .operation_proof import reuse

    expected_sha256 = sha256(expected_sha256)
    # Inherited proofs are revalidated at every call and never remembered in a
    # same-PID scope. They cannot outlive or be exported beyond their owner.
    inherited = reuse(bundle, expected_sha256)
    if inherited is not None:
        return inherited
    scope = _READ_SCOPE.get()
    if scope is None or scope.pid != os.getpid():
        return _verify_contents(bundle, expected_sha256)
    key = (bundle, expected_sha256)
    if key in scope.entries:
        data, signature = scope.entries[key]
        _unchanged_bundle(bundle, signature)
        # Never expose the remembered metadata object to a mutating caller.
        return json.loads(data)
    if len(scope.entries) >= _MAX_SCOPED_BUNDLES:
        return _verify_contents(bundle, expected_sha256)
    before = _bundle_signature(bundle)
    data = _verify_contents(bundle, expected_sha256)
    _unchanged_bundle(bundle, before)
    encoded = canonical(data)
    if (
        scope.nodes + len(before) <= _MAX_SCOPED_NODES
        and scope.metadata_bytes + len(encoded) <= _MAX_SCOPED_METADATA
    ):
        scope.entries[key] = encoded, before
        scope.nodes += len(before)
        scope.metadata_bytes += len(encoded)
    return data


def verify_staged(bundle: Path, expected_sha256: str) -> dict:
    """Read every byte of a staging tree without remembering its temporary name."""
    return _verify_contents(bundle, expected_sha256)


def _verify_contents(bundle: Path, expected_sha256: str) -> dict:
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
