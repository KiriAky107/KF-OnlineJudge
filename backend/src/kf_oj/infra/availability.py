"""Ordinary read-only view of the fixed protected Root admission record."""

import json
import os
import stat
from pathlib import Path

from kf_oj.contracts.availability import AVAILABILITY_PATH, AvailabilityRecord

GATE = Path(AVAILABILITY_PATH)


def protected_record() -> AvailabilityRecord:
    for path in GATE.parents:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("protected availability directory required")
    fd = os.open(GATE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o644
            or info.st_size > 4096
        ):
            raise ValueError("protected availability file required")
        with os.fdopen(fd, "rb", closefd=False) as source:
            contents = source.read(4097)
        if len(contents) > 4096:
            raise ValueError("availability size bound")
    finally:
        os.close(fd)

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate availability field")
            value[key] = item
        return value

    return AvailabilityRecord.model_validate(json.loads(contents, object_pairs_hook=unique))


def current(release: str | None, batch: str | None) -> AvailabilityRecord:
    value = protected_record()
    if value.release_id != release or value.configuration_id != batch:
        raise ValueError("availability is not this running API binding")
    return value
