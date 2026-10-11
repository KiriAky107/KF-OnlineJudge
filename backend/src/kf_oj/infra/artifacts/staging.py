"""Owned, private, bounded import staging. A kernel lock survives parent/child handoff."""

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from kf_oj.contracts.admin_problems import MAX_ARCHIVE_FILES, STAGING_MAX_BYTES
from kf_oj.infra.artifacts.store import ArtifactError


class ImportStaging:
    def __init__(self, root: Path):
        root = root.absolute()
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if root.resolve() != root:
            raise ArtifactError("invalid staging root")
        self.root = root
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.directory(self.fd)

    @staticmethod
    def directory(fd):
        s = os.fstat(fd)
        if (
            not stat.S_ISDIR(s.st_mode)
            or s.st_uid != os.getuid()
            or stat.S_IMODE(s.st_mode) != 0o700
        ):
            raise ArtifactError("invalid staging directory")

    def close(self):
        os.close(self.fd)

    @staticmethod
    def names(descriptor, maximum):
        result = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if len(result) >= maximum:
                    raise ArtifactError("staging file capacity exhausted")
                result.append(entry.name)
        return result

    def operation_ids(self):
        identifiers = []
        for name in self.names(self.fd, 33):
            if name == ".lock":
                continue
            try:
                identifier = UUID(name)
                if str(identifier) != name:
                    raise ValueError
            except ValueError:
                raise ArtifactError("unexpected staging entry") from None
            identifiers.append(identifier)
        return identifiers

    @contextmanager
    def exclusive(self):
        lock = os.open(
            ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd
        )
        try:
            from kf_oj.infra.artifacts.store import ArtifactStore

            ArtifactStore._regular(os.fstat(lock))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ArtifactError("import capacity busy") from None
            yield lock
        finally:
            os.close(lock)

    def usage(self):
        total, count = 0, 0
        for name in self.names(self.fd, 33):
            if name == ".lock":
                continue
            try:
                if str(UUID(name)) != name:
                    raise ValueError
            except ValueError:
                raise ArtifactError("unexpected staging entry") from None
            descriptor = self.operation(UUID(name))
            try:
                for entry in self.names(descriptor, 3):
                    count += 1
                    if count > 2 * (MAX_ARCHIVE_FILES + 2):
                        raise ArtifactError("staging file capacity exhausted")
                    if entry == "members":
                        child = self.members(descriptor)
                        try:
                            for member in self.names(child, MAX_ARCHIVE_FILES):
                                count += 1
                                info = os.stat(member, dir_fd=child, follow_symlinks=False)
                                from kf_oj.infra.artifacts.store import ArtifactStore

                                ArtifactStore._regular(info)
                                total += info.st_size
                        finally:
                            os.close(child)
                    elif entry in ("archive", "validated.json"):
                        info = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
                        from kf_oj.infra.artifacts.store import ArtifactStore

                        ArtifactStore._regular(info)
                        total += info.st_size
                    else:
                        raise ArtifactError("unexpected staging entry")
                    if count > 2 * (MAX_ARCHIVE_FILES + 2) or total > STAGING_MAX_BYTES:
                        raise ArtifactError("staging capacity exhausted")
            finally:
                os.close(descriptor)
        return total

    def operation(self, operation_id: UUID, *, create=False):
        name = str(operation_id)
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
            except FileExistsError:
                pass
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd
        )
        try:
            self.directory(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def members(operation, *, create=False):
        if create:
            os.mkdir("members", 0o700, dir_fd=operation)
        descriptor = os.open(
            "members", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=operation
        )
        try:
            ImportStaging.directory(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def remove(self, operation_id):
        try:
            descriptor = self.operation(operation_id)
        except FileNotFoundError:
            return
        try:
            names = self.names(descriptor, 3)
            if len(names) > 3:
                raise ArtifactError("unexpected staging entry")
            for name in names:
                if name == "members":
                    child = self.members(descriptor)
                    try:
                        entries = self.names(child, MAX_ARCHIVE_FILES)
                        if len(entries) > MAX_ARCHIVE_FILES:
                            raise ArtifactError("unexpected staging entry")
                        for member in entries:
                            from kf_oj.infra.artifacts.store import ArtifactStore

                            ArtifactStore._regular(
                                os.stat(member, dir_fd=child, follow_symlinks=False)
                            )
                            os.unlink(member, dir_fd=child)
                    finally:
                        os.close(child)
                    os.rmdir("members", dir_fd=descriptor)
                elif name in ("archive", "validated.json"):
                    from kf_oj.infra.artifacts.store import ArtifactStore

                    ArtifactStore._regular(os.stat(name, dir_fd=descriptor, follow_symlinks=False))
                    os.unlink(name, dir_fd=descriptor)
                else:
                    raise ArtifactError("unexpected staging entry")
        finally:
            os.close(descriptor)
        os.rmdir(str(operation_id), dir_fd=self.fd)
        os.fsync(self.fd)

    def reset_validation(self, operation_id):
        directory = self.operation(operation_id)
        try:
            names = self.names(directory, 3)
            if "members" in names:
                members = self.members(directory)
                try:
                    for name in self.names(members, MAX_ARCHIVE_FILES):
                        from kf_oj.infra.artifacts.store import ArtifactStore

                        ArtifactStore._regular(os.stat(name, dir_fd=members, follow_symlinks=False))
                        os.unlink(name, dir_fd=members)
                finally:
                    os.close(members)
                os.rmdir("members", dir_fd=directory)
            if "validated.json" in names:
                os.unlink("validated.json", dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)
