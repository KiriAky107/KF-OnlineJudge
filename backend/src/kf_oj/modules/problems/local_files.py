import os
import re
import stat
from pathlib import Path


class LocalImportFiles:
    def __init__(self, root: Path):
        root = root.absolute()
        if root.resolve() != root:
            raise ValueError("import directory must not contain links")
        self.descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            self.close()
            raise ValueError("import directory must be private and owned")

    def close(self):
        os.close(self.descriptor)

    def read(self, filename: str, maximum: int) -> bytes:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", filename):
            raise ValueError("invalid local filename")
        try:
            descriptor = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.descriptor
            )
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_uid != os.getuid()
                    or before.st_size > maximum
                ):
                    raise ValueError("invalid local input file")
                data = stream.read(maximum + 1)
                after = os.fstat(stream.fileno())
                if (
                    len(data) > maximum
                    or before.st_size != len(data)
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    raise ValueError("local input changed during import")
                return data
        except OSError:
            raise ValueError("local input unavailable") from None
