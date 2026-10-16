"""Bounded single-level ZIP reader; no extractall, metadata restoration or content execution."""

import binascii
import hashlib
import os
import re
import stat
import struct
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

from kf_oj.contracts.admin_problems import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_FILES,
    MAX_UNPACKED_BYTES,
)

NAME = re.compile(rb"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
CHUNK = 65536
LOCAL = struct.Struct("<4s5H3I2H")
CENTRAL = struct.Struct("<4s6H3I5H2I")
END = struct.Struct("<4s4H2IH")


class ArchiveError(ValueError):
    """A fixed sanitized failure; never echo an archive name, data or local path."""


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    method: int
    crc32: int
    compressed_bytes: int
    bytes: int
    offset: int
    flags: int


class ArchiveReader:
    def __init__(self, descriptor: int, expected_bytes: int, *, deadline: float):
        self.descriptor, self.deadline = descriptor, deadline
        self.before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(self.before.st_mode)
            or self.before.st_uid != os.getuid()
            or self.before.st_nlink != 1
            or stat.S_IMODE(self.before.st_mode) != 0o600
            or not 22 <= expected_bytes <= MAX_ARCHIVE_BYTES
            or self.before.st_size != expected_bytes
        ):
            raise ArchiveError("invalid_archive")
        self.size = expected_bytes
        self.unpacked_bytes, self.steps = 0, 0
        self.read_members: set[str] = set()
        self.members = self._members()

    def _tick(self) -> None:
        self.steps += 1
        if self.steps > 8192 or time.monotonic() >= self.deadline:
            raise ArchiveError("archive_deadline")

    def _read(self, offset: int, size: int) -> bytes:
        self._tick()
        if not 0 <= size <= 1024**2 or offset < 0 or offset + size > self.size:
            raise ArchiveError("invalid_archive")
        data = os.pread(self.descriptor, size, offset)
        if len(data) != size:
            raise ArchiveError("invalid_archive")
        return data

    @staticmethod
    def _extra(data: bytes) -> None:
        offset, fields = 0, 0
        # These bounded timestamps/UID attributes are ignored, never restored.
        # ZIP64, Unicode aliases and all other extension semantics are unsupported.
        while offset < len(data):
            if offset + 4 > len(data) or fields >= 32:
                raise ArchiveError("invalid_archive")
            kind, size = struct.unpack_from("<HH", data, offset)
            offset += 4
            if kind not in (0x5455, 0x7875, 0x5855, 0x7855, 0x000A) or offset + size > len(data):
                raise ArchiveError("invalid_archive")
            offset += size
            fields += 1

    def _members(self) -> tuple[ArchiveMember, ...]:
        tail_offset = max(0, self.size - 1046)
        tail = self._read(tail_offset, self.size - tail_offset)
        position = tail.rfind(b"PK\x05\x06")
        if position < 0 or position + END.size > len(tail):
            raise ArchiveError("invalid_archive")
        sig, disk, cd_disk, disk_count, count, cd_bytes, cd_offset, comment = END.unpack_from(
            tail, position
        )
        end_offset = tail_offset + position
        if (
            sig != b"PK\x05\x06"
            or disk != 0
            or cd_disk != 0
            or disk_count != count
            or not 1 <= count <= MAX_ARCHIVE_FILES
            or comment > 1024
            or end_offset + END.size + comment != self.size
            or not 46 <= cd_bytes <= 1024**2
            or cd_offset + cd_bytes != end_offset
        ):
            raise ArchiveError("invalid_archive")
        directory = self._read(cd_offset, cd_bytes)
        offset, records, names = 0, [], set()
        for _ in range(count):
            if offset + CENTRAL.size > len(directory):
                raise ArchiveError("invalid_archive")
            values = CENTRAL.unpack_from(directory, offset)
            (
                sig,
                _,
                needed,
                flags,
                method,
                _,
                _,
                crc,
                compressed,
                size,
                name_size,
                extra_size,
                comment_size,
                disk,
                _,
                attributes,
                local_offset,
            ) = values
            allowed_flags = 0x0808 | (0x0006 if method == 8 else 0)
            if (
                sig != b"PK\x01\x02"
                or needed not in (10, 20)
                or method not in (0, 8)
                or (method == 8 and needed != 20)
                or flags & ~allowed_flags
                or disk != 0
                or not 1 <= name_size <= 128
                or extra_size > 1024
                or comment_size > 256
                or size > MAX_UNPACKED_BYTES
                or compressed > MAX_ARCHIVE_BYTES
                or local_offset + LOCAL.size > cd_offset
                or attributes & 0x10
                or stat.S_IFMT(attributes >> 16) not in (0, stat.S_IFREG)
            ):
                raise ArchiveError("invalid_archive")
            start = offset + CENTRAL.size
            finish = start + name_size + extra_size + comment_size
            if finish > len(directory):
                raise ArchiveError("invalid_archive")
            name_bytes = directory[start : start + name_size]
            if not NAME.fullmatch(name_bytes):
                raise ArchiveError("invalid_archive")
            name = name_bytes.decode("ascii")
            if name.lower() in names:
                raise ArchiveError("invalid_archive")
            names.add(name.lower())
            self._extra(directory[start + name_size : start + name_size + extra_size])
            records.append(ArchiveMember(name, method, crc, compressed, size, local_offset, flags))
            offset = finish
        if offset != len(directory) or sum(m.bytes for m in records) > MAX_UNPACKED_BYTES:
            raise ArchiveError("invalid_archive")
        cursor, members = 0, []
        for member in sorted(records, key=lambda m: m.offset):
            if member.offset != cursor:
                raise ArchiveError("invalid_archive")
            sig, needed, flags, method, _, _, crc, compressed, size, name_size, extra_size = (
                LOCAL.unpack(self._read(cursor, LOCAL.size))
            )
            if (
                sig != b"PK\x03\x04"
                or needed not in (10, 20)
                or flags != member.flags
                or method != member.method
                or (method == 8 and needed != 20)
                or name_size != len(member.name)
                or extra_size > 1024
            ):
                raise ArchiveError("invalid_archive")
            metadata = self._read(cursor + LOCAL.size, name_size + extra_size)
            if metadata[:name_size] != member.name.encode("ascii"):
                raise ArchiveError("invalid_archive")
            self._extra(metadata[name_size:])
            data_offset = cursor + LOCAL.size + name_size + extra_size
            cursor = data_offset + member.compressed_bytes
            if cursor > cd_offset or (method == 0 and member.compressed_bytes != member.bytes):
                raise ArchiveError("invalid_archive")
            if flags & 8:
                if (crc, compressed, size) not in (
                    (0, 0, 0),
                    (member.crc32, member.compressed_bytes, member.bytes),
                ):
                    raise ArchiveError("invalid_archive")
                marker = self._read(cursor, 4)
                descriptor_size = 16 if marker == b"PK\x07\x08" else 12
                descriptor = self._read(cursor, descriptor_size)
                described = struct.unpack("<III", descriptor[-12:])
                if described != (member.crc32, member.compressed_bytes, member.bytes):
                    raise ArchiveError("invalid_archive")
                cursor += descriptor_size
            elif (crc, compressed, size) != (member.crc32, member.compressed_bytes, member.bytes):
                raise ArchiveError("invalid_archive")
            members.append(
                ArchiveMember(
                    member.name,
                    method,
                    member.crc32,
                    member.compressed_bytes,
                    member.bytes,
                    data_offset,
                    flags,
                )
            )
        if cursor != cd_offset:
            raise ArchiveError("invalid_archive")
        return tuple(members)

    def verify_digest(self, expected: str) -> None:
        digest = hashlib.sha256()
        for offset in range(0, self.size, CHUNK):
            digest.update(self._read(offset, min(CHUNK, self.size - offset)))
        if digest.hexdigest() != expected:
            raise ArchiveError("archive_digest_mismatch")

    def verify_unchanged(self) -> None:
        after = os.fstat(self.descriptor)
        fields = (
            "st_dev",
            "st_ino",
            "st_nlink",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(self.before, f) != getattr(after, f) for f in fields):
            raise ArchiveError("archive_changed")

    @staticmethod
    def _nested(prefix: bytes) -> bool:
        return prefix.startswith(
            (
                b"PK\x03\x04",
                b"PK\x05\x06",
                b"PK\x07\x08",
                b"\x1f\x8b",
                b"BZh",
                b"\xfd7zXZ\x00",
                b"Rar!\x1a\x07",
                b"7z\xbc\xaf\x27\x1c",
            )
        ) or (len(prefix) >= 262 and prefix[257:262] == b"ustar")

    def chunks(self, name: str, maximum: int) -> Iterator[bytes]:
        member = next((m for m in self.members if m.name == name), None)
        if member is None or name in self.read_members or member.bytes > maximum:
            raise ArchiveError("invalid_archive")
        self.read_members.add(name)
        used, crc, prefix = 0, 0, bytearray()
        inflater = zlib.decompressobj(-15) if member.method == 8 else None
        try:
            for offset in range(0, member.compressed_bytes, CHUNK):
                pending = self._read(
                    member.offset + offset, min(CHUNK, member.compressed_bytes - offset)
                )
                while pending:
                    self._tick()
                    if inflater:
                        data = inflater.decompress(pending, min(CHUNK, member.bytes - used + 1))
                        pending = inflater.unconsumed_tail
                        if inflater.unused_data:
                            raise ArchiveError("invalid_archive")
                    else:
                        data, pending = pending, b""
                    used += len(data)
                    self.unpacked_bytes += len(data)
                    if (
                        used > member.bytes
                        or used > maximum
                        or self.unpacked_bytes > MAX_UNPACKED_BYTES
                    ):
                        raise ArchiveError("invalid_archive")
                    prefix.extend(data[: max(0, 512 - len(prefix))])
                    if self._nested(bytes(prefix)):
                        raise ArchiveError("invalid_archive")
                    crc = binascii.crc32(data, crc)
                    if data:
                        yield data
            if (inflater and not inflater.eof) or used != member.bytes or crc != member.crc32:
                raise ArchiveError("invalid_archive")
        except zlib.error:
            raise ArchiveError("invalid_archive") from None

    def read(self, name: str, maximum: int) -> bytes:
        return b"".join(self.chunks(name, maximum))

    def extract_remaining(self, directory: int) -> dict[str, tuple[str, int]]:
        info = os.fstat(directory)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ArchiveError("invalid_extraction_directory")
        references = {}
        for member in self.members:
            if member.name in self.read_members:
                continue
            fd = os.open(
                member.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                digest, size = hashlib.sha256(), 0
                for chunk in self.chunks(member.name, member.bytes):
                    digest.update(chunk)
                    size += len(chunk)
                    remaining = memoryview(chunk)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise ArchiveError("extraction_unavailable")
                        remaining = remaining[written:]
                os.fsync(fd)
                references[member.name] = (digest.hexdigest(), size)
            finally:
                os.close(fd)
        os.fsync(directory)
        self.verify_unchanged()
        return references
