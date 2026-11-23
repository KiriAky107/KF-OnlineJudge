"""Private reporting and moderation contracts for published skill versions."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from kf_oj.contracts.judging import Counter, Identifier, WireModel

SkillReportReason = Literal["incorrect_content", "privacy", "licensing", "other"]
SkillReportState = Literal["pending", "confirmed", "dismissed"]
ReportMessage = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]


class SubmitSkillReport(WireModel):
    request_id: Identifier
    reason: SkillReportReason
    details: ReportMessage


class ReviewSkillReport(WireModel):
    action: Literal["confirm", "dismiss"]
    note: ReportMessage


class SkillReportView(WireModel):
    report_id: Identifier
    version_id: Identifier
    reason: SkillReportReason
    details: ReportMessage
    state: SkillReportState
    submitted_at: AwareDatetime
    decided_at: AwareDatetime | None
    decision_note: ReportMessage | None


class SkillReportPage(WireModel):
    items: Annotated[tuple[SkillReportView, ...], Field(max_length=20)]
    truncated: bool


class SkillReportReviewView(SkillReportView):
    release_title: str
    version: Counter


class SkillReportReviewPage(WireModel):
    items: Annotated[tuple[SkillReportReviewView, ...], Field(max_length=50)]
    truncated: bool
