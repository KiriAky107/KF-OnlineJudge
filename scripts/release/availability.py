"""Only lifecycle checks may open this fixed Root-owned API admission binding."""

import os
import secrets
import stat
from pathlib import Path

from kf_oj.contracts.availability import AVAILABILITY_PATH, AvailabilityRecord

from . import install, preflight
from .policy import DeliveryError, canonical, read_json

GATE = Path(AVAILABILITY_PATH)


def read() -> AvailabilityRecord | None:
    try:
        GATE.lstat()
    except FileNotFoundError:
        return None
    preflight.protected(GATE)
    info = GATE.stat()
    if info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o644:
        raise DeliveryError("unknown availability record; refused adoption")
    return AvailabilityRecord.model_validate(read_json(GATE, 4096))


def require_root() -> None:
    if os.getuid() != 0 or os.geteuid() != 0 or os.getgid() != 0:
        raise DeliveryError("root availability operator required")


def write(selected: dict, state: str) -> dict:
    require_root()
    value = AvailabilityRecord(
        schema_version=1,
        release_id=selected["release_id"],
        configuration_id=selected["configuration_id"],
        state=state,
    )
    if install.selection(check_configuration=False) != selected:
        raise DeliveryError("availability selection changed")
    install.directory(GATE.parent, 0o711)
    before = read()
    if before is not None and (
        before.release_id != selected["release_id"]
        or before.configuration_id != selected["configuration_id"]
    ):
        # A new stopped installation can replace an old quiescent binding only.
        install.require_stopped()
        if before.state != "quiescent" or state != "draining":
            raise DeliveryError("availability binding replacement requires quiescence")
    return _publish(value, before, selected)


def prepare_activation(previous: dict | None, selected: dict) -> dict:
    """Close admission for a fully checked, stopped configuration selection.

    Activation validates both releases, the new hardware-bound configuration,
    and empty execution resources before calling here. Stopped dependencies
    are required too. Pending owner work stays on disk: this publishes only
    ``draining``, and makes no claim of a completed business drain. Ordinary
    lifecycle readiness remains the only operation that may reopen admission.
    """
    from . import dependency_install

    require_root()
    value = AvailabilityRecord(
        schema_version=1,
        release_id=selected["release_id"],
        configuration_id=selected["configuration_id"],
        state="draining",
    )
    install.directory(GATE.parent, 0o711)
    before = read()
    if before is None:
        if previous is not None:
            raise DeliveryError("selected installation has no admission record")
    elif previous is None or (
        before.release_id != previous["release_id"]
        or before.configuration_id != previous["configuration_id"]
    ):
        raise DeliveryError("admission is not bound to the previous selection")
    dependency_install.require_stopped()
    return _publish(value, before, previous)


def _selection_matches(selected: dict | None) -> bool:
    if selected is not None:
        return install.selection(check_configuration=False) == selected
    return not any(path.exists() or path.is_symlink() for path in (install.ACTIVE, install.CURRENT))


def _publish(
    value: AvailabilityRecord, before: AvailabilityRecord | None, selected: dict | None
) -> dict:
    if read() != before or not _selection_matches(selected):
        raise DeliveryError("availability changed before publication")
    temporary = GATE.parent / (".availability-" + secrets.token_hex(8))
    created = None
    try:
        install.exclusive_file(temporary, canonical(value.model_dump(mode="json")), 0o644)
        info = temporary.stat()
        created = (info.st_dev, info.st_ino)
        if read() != before or not _selection_matches(selected):
            raise DeliveryError("availability changed during publication")
        os.replace(temporary, GATE)
        install.sync_directory(GATE.parent)
    finally:
        if created is not None and temporary.exists():
            info = temporary.lstat()
            if (info.st_dev, info.st_ino) == created:
                temporary.unlink()
    if read() != value:
        raise DeliveryError("availability publication readback differs")
    return value.model_dump(mode="json")
