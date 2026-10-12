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


def write(selected: dict, state: str) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0 or os.getgid() != 0:
        raise DeliveryError("root availability operator required")
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
    temporary = GATE.parent / (".availability-" + secrets.token_hex(8))
    created = None
    try:
        install.exclusive_file(temporary, canonical(value.model_dump(mode="json")), 0o644)
        info = temporary.stat()
        created = (info.st_dev, info.st_ino)
        if read() != before or install.selection(check_configuration=False) != selected:
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
