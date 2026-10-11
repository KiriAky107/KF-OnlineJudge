"""Versioned problem-import DTOs; hidden bytes, paths and toolchain selection stay private."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictBool, model_validator

from kf_oj.contracts.judging import Digest, Identifier, Label, RunLimits, Version, WireModel
from kf_oj.contracts.judging_v2 import Language
from kf_oj.contracts.problems import PublicLanguageConfiguration, PublicSample

MAX_ARCHIVE_BYTES = 64 * 1024**2
MAX_UNPACKED_BYTES = 64 * 1024**2
MAX_ARCHIVE_FILES = 534
MAX_ARCHIVE_NAME_BYTES = 128
MAX_UPLOAD_CHUNKS = 4096
MAX_UPLOAD_CHUNK_BYTES = 1024**2
UPLOAD_DEADLINE_SECONDS = 30
VALIDATION_DEADLINE_SECONDS = 60
MAX_IMPORT_ATTEMPTS = 2
IMPORT_LIFETIME_SECONDS = 86400
MAX_OWNER_CANDIDATES = 10
MAX_TOTAL_CANDIDATES = 32
STAGING_MAX_BYTES = 256 * 1024**2
STAGING_RESERVATION_BYTES = MAX_ARCHIVE_BYTES + MAX_UNPACKED_BYTES


class ImportState(StrEnum):
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"
    PUBLISHED = "published"
    DISCARDED = "discarded"
    EXPIRED = "expired"


class ImportFailure(StrEnum):
    INVALID_ARCHIVE = "invalid_archive"
    INVALID_MANIFEST = "invalid_manifest"
    INVALID_DATA = "invalid_data"
    DIGEST_MISMATCH = "digest_mismatch"
    POLICY_REJECTED = "policy_rejected"
    UPLOAD_INTERRUPTED = "upload_interrupted"
    UPLOAD_DEADLINE = "upload_deadline"
    VALIDATION_DEADLINE = "validation_deadline"
    VALIDATION_UNAVAILABLE = "validation_unavailable"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    AUTHORITY_REVOKED = "authority_revoked"
    EXPIRED = "expired"


class ProblemImportLimits(WireModel):
    schema_version: Version = 1
    archive_format: Literal["single_level_zip"] = "single_level_zip"
    manifest_name: Literal["problem.json"] = "problem.json"
    manifest_schema_versions: tuple[Literal[1, 2], ...] = (1, 2)
    languages: tuple[Language, ...] = tuple(Language)
    archive_bytes: int = MAX_ARCHIVE_BYTES
    unpacked_bytes: int = MAX_UNPACKED_BYTES
    files: int = MAX_ARCHIVE_FILES
    filename_ascii_bytes: int = MAX_ARCHIVE_NAME_BYTES
    upload_chunks: int = MAX_UPLOAD_CHUNKS
    chunk_bytes: int = MAX_UPLOAD_CHUNK_BYTES
    upload_deadline_seconds: int = UPLOAD_DEADLINE_SECONDS
    validation_deadline_seconds: int = VALIDATION_DEADLINE_SECONDS
    global_concurrency: Literal[1] = 1
    owner_concurrency: Literal[1] = 1
    owner_candidates: int = MAX_OWNER_CANDIDATES
    total_candidates: int = MAX_TOTAL_CANDIDATES
    staging_bytes: int = STAGING_MAX_BYTES
    candidate_lifetime_seconds: int = IMPORT_LIFETIME_SECONDS


class ProblemImportCapabilities(WireModel):
    schema_version: Version = 1
    limits: ProblemImportLimits = ProblemImportLimits()
    uploads_available: StrictBool
    validation_available: StrictBool


class CreateProblemImport(WireModel):
    schema_version: Version = 1
    operation_id: Identifier
    archive_digest: Digest
    archive_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_ARCHIVE_BYTES)]


class LegacyImportBudget(WireModel):
    compile_limits: RunLimits
    run_limits: RunLimits
    job_wall_time_us: Annotated[int, Field(strict=True, ge=1000, le=3600000000)]


class CandidatePreview(WireModel):
    candidate_id: Identifier
    slug: Label
    title: Annotated[str, Field(strict=True, min_length=1, max_length=160)]
    statement: Annotated[str, Field(strict=True, min_length=1, max_length=262144)]
    samples: Annotated[tuple[PublicSample, ...], Field(max_length=10)]
    judging_schema_version: Annotated[int, Field(strict=True, ge=1, le=2)]
    languages: Annotated[tuple[Language, ...], Field(min_length=1, max_length=4)]
    legacy_budget: LegacyImportBudget | None
    language_configurations: Annotated[
        tuple[PublicLanguageConfiguration, ...], Field(max_length=4)
    ] = ()
    case_count: Annotated[int, Field(strict=True, ge=1, le=256)]

    @model_validator(mode="after")
    def bounded_text(self):
        if len(self.statement.encode("utf-8")) > 262144 or "\x00" in self.statement + self.title:
            raise ValueError("invalid public text")
        languages = [c.language for c in self.language_configurations]
        if len(set(languages)) != len(languages):
            raise ValueError("duplicate language configuration")
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("duplicate language")
        if self.judging_schema_version == 1:
            if (
                self.language_configurations
                or self.legacy_budget is None
                or any(
                    language not in (Language.C17, Language.CPP20) for language in self.languages
                )
            ):
                raise ValueError("invalid legacy candidate")
        elif tuple(languages) != self.languages or self.legacy_budget is not None:
            raise ValueError("invalid multi-runtime candidate")
        return self


class ProblemImportSummary(WireModel):
    schema_version: Version = 1
    operation_id: Identifier
    state: ImportState
    expected_archive_digest: Digest
    archive_bytes: Annotated[int, Field(strict=True, ge=1, le=MAX_ARCHIVE_BYTES)]
    created_at: datetime
    expires_at: datetime
    updated_at: datetime
    failure_code: ImportFailure | None
    candidate_id: Identifier | None
    published_version_id: Identifier | None

    @model_validator(mode="after")
    def consistent_state(self):
        if self.expires_at <= self.created_at or self.updated_at < self.created_at:
            raise ValueError("invalid import time range")
        if (self.state in (ImportState.READY, ImportState.PUBLISHED)) != (
            self.candidate_id is not None
        ):
            raise ValueError("candidate state mismatch")
        if (self.state == ImportState.PUBLISHED) != (self.published_version_id is not None):
            raise ValueError("published state mismatch")
        if self.state in (ImportState.FAILED, ImportState.EXPIRED):
            if self.failure_code is None:
                raise ValueError("failure code required")
        elif self.failure_code is not None:
            raise ValueError("unexpected failure code")
        return self


class ProblemImportDetail(ProblemImportSummary):
    candidate: CandidatePreview | None

    @model_validator(mode="after")
    def candidate_matches(self):
        if (self.candidate_id is None) != (self.candidate is None):
            raise ValueError("candidate preview missing")
        if self.candidate and self.candidate.candidate_id != self.candidate_id:
            raise ValueError("candidate preview mismatch")
        return self


class ProblemImportPage(WireModel):
    schema_version: Version = 1
    items: Annotated[tuple[ProblemImportSummary, ...], Field(max_length=100)]
    next_after: Identifier | None
