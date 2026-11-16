"""Explicit TLS replacement while stopped, with bounded retained prior material."""

import fcntl
import hashlib
import os
import re
import stat
from pathlib import Path

from . import configuration, install, preflight, proxy_service
from .policy import DeliveryError, canonical, fields, read_json, sha256

MAX_ROTATIONS = 16
MAX_MATERIAL_BYTES = 65536


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _material(path: Path, mode: int, gid: int) -> bytes:
    preflight.protected(path)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) != (0, gid, mode)
        or not 1 <= before.st_size <= MAX_MATERIAL_BYTES
    ):
        raise DeliveryError("protected bounded TLS material required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(MAX_MATERIAL_BYTES + 1)
        after = os.fstat(stream.fileno())
    keys = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if len(data) != before.st_size or any(
        getattr(before, key) != getattr(opened, key) or getattr(opened, key) != getattr(after, key)
        for key in keys
    ):
        raise DeliveryError("TLS material changed while observed")
    return data


def _pair(directory: Path) -> tuple[str, str]:
    preflight.protected(directory, directory=True)
    info = directory.lstat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o711):
        raise DeliveryError("protected TLS directory differs")
    entries = []
    for entry in directory.iterdir():
        entries.append(entry.name)
        if len(entries) > 2:
            raise DeliveryError("unknown TLS directory member")
    if set(entries) != {"fullchain.pem", "key.pem"}:
        raise DeliveryError("complete TLS pair required")
    return (
        _hash(_material(directory / "fullchain.pem", 0o644, 0)),
        _hash(_material(directory / "key.pem", 0o640, proxy_service.UID)),
    )


def _stage_file(path: Path, data: bytes, mode: int, gid: int) -> None:
    """Finish only an exact prefix in this operation's protected staging directory."""
    preflight.protected(path.parent, directory=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 0
            or info.st_gid not in {0, gid}
            or (
                stat.S_IMODE(info.st_mode) != mode
                and not (
                    info.st_size == 0
                    and info.st_gid == 0
                    and not stat.S_IMODE(info.st_mode) & ~mode
                )
            )
            or not 0 <= info.st_size <= len(data) <= MAX_MATERIAL_BYTES
        ):
            raise DeliveryError("unknown TLS staging file")
        prefix = os.read(fd, len(data) + 1)
        if not data.startswith(prefix) or len(prefix) != info.st_size:
            raise DeliveryError("TLS staging bytes differ")
        os.fchmod(fd, mode)
        if len(prefix) != len(data):
            with os.fdopen(os.dup(fd), "wb", closefd=True) as stream:
                stream.write(data[len(prefix) :])
                stream.flush()
        os.fchown(fd, 0, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    if _material(path, mode, gid) != data:
        raise DeliveryError("TLS staging readback differs")


def _previous_binding(binding: dict, previous: tuple[str, str], known_snapshot) -> None:
    directory = proxy_service.UNITS / proxy_service.batch(binding).name
    if canonical(known_snapshot(directory)) != canonical(binding):
        raise DeliveryError("previous TLS unit binding differs")
    path = proxy_service.batch(binding) / "launch.json"
    preflight.protected(path)
    value = fields(
        read_json(path, 4096),
        {
            "schema_version",
            "release_id",
            "configuration_id",
            "origin",
            "configuration_sha256",
            "certificate_sha256",
            "key_sha256",
        },
        "previous proxy launch spec",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or (value["release_id"], value["configuration_id"])
        != (binding["release_id"], binding["configuration_id"])
        or (value["certificate_sha256"], value["key_sha256"]) != previous
    ):
        raise DeliveryError("previous TLS fingerprints do not match the installed unit")


def rotate(
    selected: dict,
    spec: dict,
    certificate: Path,
    key: Path,
    previous_certificate: str,
    previous_key: str,
    old_directory: Path | None,
    known_snapshot,
) -> dict:
    if (os.getuid(), os.geteuid(), os.getgid()) != (0, 0, 0):
        raise DeliveryError("root TLS rotation operator required")
    previous = (sha256(previous_certificate), sha256(previous_key))
    desired = (sha256(spec["certificate_sha256"]), sha256(spec["key_sha256"]))
    new_certificate = _material(certificate, 0o600, 0)
    new_key = _material(key, 0o600, 0)
    if (_hash(new_certificate), _hash(new_key)) != desired:
        raise DeliveryError("new TLS inputs changed")
    header = {
        "schema_version": 1,
        "selected": selected,
        "configuration_sha256": spec["configuration_sha256"],
        "origin": spec["origin"],
        "previous_certificate_sha256": previous[0],
        "previous_key_sha256": previous[1],
        "certificate_sha256": desired[0],
        "key_sha256": desired[1],
    }
    identifier = "tls-" + _hash(canonical(header))
    current = proxy_service.ROOT / "tls"
    history = proxy_service.ROOT / "tls-history"
    operation = history / identifier
    pending, archived = operation / "pending", operation / "previous"
    plan_path = operation / "plan.json"
    # Serialize with the existing lifecycle owner; its empty lock is not a log.
    state = Path("/var/lib/kf-onlinejudge/operations")
    install.directory(state, 0o700)
    lock = state / "maintenance.lock"
    install.exclusive_file(lock, b"", 0o600)
    configuration.private(lock)
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install.require_stopped()
        if install.selection() != selected:
            raise DeliveryError("TLS rotation selection changed")
        if previous == desired:
            if _pair(current) != desired:
                raise DeliveryError("current TLS pair differs from explicit fingerprints")
            return {"rotation_id": None, "changed": False, "services_started": False}
        install.directory(history, 0o700)
        if history.stat().st_dev != proxy_service.ROOT.stat().st_dev or (
            current.exists() and current.stat().st_dev != history.stat().st_dev
        ):
            raise DeliveryError("TLS rotation requires one protected filesystem")
        entries = []
        for entry in history.iterdir():
            entries.append(entry)
            if (
                len(entries) > MAX_ROTATIONS
                or re.fullmatch(r"tls-[0-9a-f]{64}", entry.name) is None
            ):
                raise DeliveryError("bounded TLS history capacity or membership differs")
            preflight.protected(entry, directory=True)
        if not operation.exists() and len(entries) >= MAX_ROTATIONS:
            raise DeliveryError(
                "TLS history is full; explicit verified archive maintenance required"
            )
        install.directory(operation, 0o700)
        if not {p.name for p in operation.iterdir()} <= {
            "pending",
            "previous",
            "plan.pending",
            "plan.json",
        }:
            raise DeliveryError("unknown TLS rotation member")
        if plan_path.exists() or plan_path.is_symlink():
            configuration.private(plan_path)
            saved = fields(
                read_json(plan_path, 16384),
                set(header) | {"rotation_id", "previous_binding"},
                "TLS rotation plan",
            )
            if (
                canonical({k: saved[k] for k in header}) != canonical(header)
                or saved["rotation_id"] != identifier
            ):
                raise DeliveryError("TLS rotation plan differs")
            binding = saved["previous_binding"]
            _previous_binding(binding, previous, known_snapshot)
        else:
            if (
                archived.exists()
                or archived.is_symlink()
                or old_directory is None
                or _pair(current) != previous
            ):
                raise DeliveryError("unrecorded TLS rotation state")
            binding = known_snapshot(old_directory)
            _previous_binding(binding, previous, known_snapshot)
            install.directory(pending, 0o711)
            _stage_file(pending / "fullchain.pem", new_certificate, 0o644, 0)
            _stage_file(pending / "key.pem", new_key, 0o640, proxy_service.UID)
            if _pair(pending) != desired:
                raise DeliveryError("new TLS pair readback differs")
            value = canonical({**header, "rotation_id": identifier, "previous_binding": binding})
            _stage_file(operation / "plan.pending", value, 0o600, 0)
            os.rename(operation / "plan.pending", plan_path)
            install.sync_directory(operation)
        changed = False
        if current.exists() or current.is_symlink():
            observed = _pair(current)
            if observed == desired:
                if pending.exists() or pending.is_symlink() or _pair(archived) != previous:
                    raise DeliveryError("completed TLS rotation archive differs")
            elif observed == previous and not archived.exists() and not archived.is_symlink():
                if _pair(pending) != desired:
                    raise DeliveryError("prepared TLS pair differs")
                os.rename(current, archived)
                install.sync_directory(proxy_service.ROOT)
                install.sync_directory(operation)
                changed = True
            else:
                raise DeliveryError("unknown current TLS rotation state")
        if not current.exists() and not current.is_symlink():
            if _pair(archived) != previous or _pair(pending) != desired:
                raise DeliveryError("interrupted TLS rotation state differs")
            os.rename(pending, current)
            install.sync_directory(proxy_service.ROOT)
            install.sync_directory(operation)
            changed = True
        if _pair(current) != desired or _pair(archived) != previous:
            raise DeliveryError("TLS rotation final readback differs")
        install.require_stopped()
        if install.selection() != selected:
            raise DeliveryError("TLS rotation selection changed after publication")
        return {
            "rotation_id": identifier,
            "changed": changed,
            "previous_pair_retained": True,
            "tls_pair_published": True,
            "tls_verified": False,
            "services_started": False,
        }
    finally:
        os.close(fd)
