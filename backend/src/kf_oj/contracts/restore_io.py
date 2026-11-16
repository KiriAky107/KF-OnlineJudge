"""Private offline restore byte-copy protocol; conveys no execution authority."""

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    StrictInt,
    model_validator,
)

from kf_oj.contracts.judging import integer_version

MAX_FILES = 131072
MAX_LOGICAL_BYTES = 12 * 1024**3
MAX_FILE_BYTES = 256 * 1024**2
MAX_BATCH_BYTES = 64 * 1024**2
DOMAINS = frozenset(
    "postgres artifacts imports app-config judge-config factory operations releases "
    "images dependencies proxy recovery".split()
)
Count = Annotated[StrictInt, Field(ge=0, le=MAX_FILES)]
ByteCount = Annotated[StrictInt, Field(ge=0, le=MAX_LOGICAL_BYTES)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RestoreFile(Record):
    path: Annotated[str, Field(min_length=1, max_length=512)]
    uid: Annotated[StrictInt, Field(ge=0, le=1205)]
    gid: Annotated[StrictInt, Field(ge=0, le=1205)]
    mode: StrictInt
    bytes: Annotated[StrictInt, Field(ge=0, le=MAX_FILE_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def check_member(self) -> "RestoreFile":
        parts = self.path.split("/")
        if (
            len(parts) < 2
            or parts[0] not in DOMAINS
            or any(
                p in {"", ".", "..", ".git", ".ssh", ".localplan", "docs"}
                or re.fullmatch(r"[A-Za-z0-9_.+@-]{1,255}", p) is None
                for p in parts
            )
        ):
            raise ValueError("restore file must be below a fixed backup domain")
        if self.uid not in {0, 1200, 1201, 1202, 1203, 1204, 1205} or self.gid not in (
            {0, 1203, 1204, 1205} if self.uid == 0 else {self.uid}
        ):
            raise ValueError("unsupported restore identity")
        if self.mode not in {0o400, 0o444, 0o600, 0o640, 0o644, 0o555, 0o755}:
            raise ValueError("unsupported restore file mode")
        if self.sha256 in {"a" * 64, "0" * 64}:
            raise ValueError("placeholder digest refused")
        return self


class Open(Record):
    schema_version: Annotated[Literal[1], BeforeValidator(integer_version)]
    kind: Literal["restore.io.open"]
    backup_id: Annotated[str, Field(pattern=r"^bk-[0-9a-f]{32}$")]
    recovery_id: Annotated[str, Field(pattern=r"^rr-[0-9a-f]{32}$")]
    expected_files: Annotated[StrictInt, Field(ge=1, le=MAX_FILES)]
    expected_logical_bytes: ByteCount
    deadline_monotonic_ns: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]


class Batch(Record):
    schema_version: Annotated[Literal[1], BeforeValidator(integer_version)]
    kind: Literal["restore.io.batch"]
    batch_id: Annotated[StrictInt, Field(ge=1, le=MAX_FILES)]
    files: Annotated[list[RestoreFile], Field(min_length=1, max_length=256)]

    @model_validator(mode="after")
    def check_batch(self) -> "Batch":
        if len(self.files) > 1 and sum(f.bytes for f in self.files) > MAX_BATCH_BYTES:
            raise ValueError("restore batch byte bound exceeded")
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("duplicate restore batch member")
        return self


class Finish(Record):
    schema_version: Annotated[Literal[1], BeforeValidator(integer_version)]
    kind: Literal["restore.io.finish"]


class RestoreIORequest(RootModel[Annotated[Open | Batch | Finish, Field(discriminator="kind")]]):
    pass


class RestoreIOReply(Record):
    schema_version: Annotated[Literal[1], BeforeValidator(integer_version)]
    kind: Literal["restore.io.reply"]
    stage: Literal["opened", "verified", "finished", "refused"]
    batch_id: Count
    verified_files: Count
    verified_logical_bytes: ByteCount
    execution_verified: Annotated[
        Literal[False],
        BeforeValidator(lambda value: value if type(value) is bool else _bad_boolean()),
    ]
    error: Literal["none", "protocol", "guard", "deadline", "filesystem", "bytes", "identity"]

    @model_validator(mode="after")
    def check_reply(self) -> "RestoreIOReply":
        if (self.stage == "refused") != (self.error != "none"):
            raise ValueError("restore refusal state differs")
        if self.stage == "opened" and (
            self.batch_id or self.verified_files or self.verified_logical_bytes
        ):
            raise ValueError("restore open cannot claim verified data")
        if self.stage in {"verified", "finished"} and not (
            1 <= self.batch_id <= self.verified_files
        ):
            raise ValueError("restore completion has no verified batch")
        return self


def _bad_boolean() -> bool:
    raise ValueError("execution verification must be a boolean false")
