"""Bounded administrator projections and immutable operation receipts, separate from judging."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool

from kf_oj.contracts.admin_problems import CandidatePreview
from kf_oj.contracts.judging import Identifier, Label, Version, WireModel
from kf_oj.contracts.problems import ProblemSummary

MaintenanceAction = Literal["publish", "disable", "restore"]
AuditAction = Literal[
    "upload_created",
    "upload_received",
    "import_ready",
    "import_failed",
    "import_discarded",
    "import_expired",
    "published",
    "disabled",
    "restored",
    "authority_bootstrap",
    "authority_grant",
    "authority_revoke",
    "authority_enable",
    "authority_disable",
]


class MaintainedProblem(WireModel):
    id: Identifier
    slug: Label
    enabled: StrictBool
    active_version_id: Identifier | None
    revision: Annotated[int, Field(strict=True, ge=0)]
    title: Annotated[str | None, Field(max_length=160)]
    created_at: datetime


class MaintainedProblemPage(WireModel):
    schema_version: Version = 1
    items: Annotated[tuple[MaintainedProblem, ...], Field(max_length=100)]
    next_after: Label | None


class MaintainedVersion(ProblemSummary):
    published_at: datetime
    case_count: Annotated[int, Field(strict=True, ge=1, le=256)]


class MaintainedVersionPage(WireModel):
    schema_version: Version = 1
    items: Annotated[tuple[MaintainedVersion, ...], Field(max_length=100)]
    next_before: Annotated[int | None, Field(strict=True, ge=1)]


class PublishCandidate(WireModel):
    schema_version: Version = 1
    operation_id: Identifier
    import_operation_id: Identifier
    candidate_id: Identifier
    expected_active_version_id: Identifier | None


class ChangeProblemAvailability(WireModel):
    schema_version: Version = 1
    operation_id: Identifier
    expected_active_version_id: Identifier
    expected_enabled: StrictBool


class MaintenanceReceipt(WireModel):
    schema_version: Version = 1
    operation_id: Identifier
    action: MaintenanceAction
    completed_at: datetime
    problem: MaintainedProblem
    published_version_id: Identifier | None = None


class MaintenanceAuditEvent(WireModel):
    id: Identifier
    created_at: datetime
    actor_kind: Literal["account", "operator"]
    actor_id: Identifier | None
    operator_name: Annotated[str | None, Field(max_length=63)]
    authorization_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")]
    operation_id: Identifier
    action: AuditAction
    target_id: Identifier | None
    result: Literal["committed"] = "committed"


class MaintenanceAuditPage(WireModel):
    schema_version: Version = 1
    items: Annotated[tuple[MaintenanceAuditEvent, ...], Field(max_length=100)]
    next_before: Annotated[str | None, Field(max_length=100)]


# This alias deliberately uses the existing sanitized candidate contract. No new
# endpoint exposes the private manifest, artifact digest or hidden case names.
PublicCandidate = CandidatePreview
