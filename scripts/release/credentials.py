"""Strict systemd credential metadata, including service-only POSIX ACLs."""

import os
import stat
import struct
from pathlib import Path

from . import preflight
from .policy import DeliveryError

ACL_INPUTS = {
    "kf-coordinator.service": (1201, {"coordinator.json"}),
    "kf-worker.service": (1202, {"worker.json"}),
    "kf-postgres-initialize.service": (1203, {"bootstrap"}),
    "kf-postgres-recover.service": (1203, {"bootstrap", "connections"}),
}


def validate_acl(contents: bytes, uid: int) -> None:
    # Linux POSIX ACL xattr v2: owner and exactly this user may read;
    # the owning group and other users have no rights. The group mode bits
    # describe the ACL mask, rather than an owning-group permission grant.
    undefined = 0xFFFFFFFF
    entries = [
        (1, 4, undefined),
        (2, 4, uid),
        (4, 0, undefined),
        (16, 4, undefined),
        (32, 0, undefined),
    ]
    expected = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *row) for row in entries)
    if contents != expected:
        raise DeliveryError("service-only credential ACL differs")


def validate_metadata(path: Path, uid: int, maximum: int) -> os.stat_result:
    """Call after the fixed service identity/cgroup/budgets have been verified."""
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in {0, uid}
        or not 1 <= info.st_size <= maximum
    ):
        raise DeliveryError("private bounded systemd credential required")
    mode = stat.S_IMODE(info.st_mode)
    if mode in {0o400, 0o600}:
        return info
    bound_uid, members = ACL_INPUTS.get(path.parent.name, (None, set()))
    if (
        mode != 0o440
        or (info.st_uid, info.st_gid) != (0, 0)
        or path.parent.parent != Path("/run/credentials")
        or uid != bound_uid
        or path.name not in members
        or not os.statvfs(path).f_flag & os.ST_RDONLY
    ):
        raise DeliveryError("private bounded systemd credential required")
    preflight.protected(path.parent, directory=True)
    validate_acl(os.getxattr(path, "system.posix_acl_access"), uid)
    return info
