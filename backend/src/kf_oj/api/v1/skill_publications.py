"""Send drafts for review, settle versions, and discover published skills."""

from collections.abc import Awaitable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.agent_runtime import AgentPurpose
from kf_oj.contracts.skill_publications import (
    ChangeSkillFavorite,
    ChangeSkillRating,
    DisableSkillVersion,
    PublicationState,
    PublishedSkill,
    PublishedSkillPage,
    ReviewSkillVersion,
    SkillVersionRating,
    SkillVersionReview,
    SkillVersionReviewPage,
    SubmitSkillVersion,
)
from kf_oj.contracts.skill_reports import (
    ReviewSkillReport,
    SkillReportPage,
    SkillReportReviewPage,
    SkillReportReviewView,
    SkillReportState,
    SkillReportView,
    SubmitSkillReport,
)
from kf_oj.modules.identity.public import AdministrationDenied, AuthenticationFailed, Principal
from kf_oj.modules.skills.public import (
    SkillDraftConflict,
    SkillDraftUnavailable,
    SkillPublications,
    SkillReports,
)

router = APIRouter(tags=["skill-publications"])
Reader = Annotated[Principal, Depends(current_principal)]
Writer = Annotated[Principal, Depends(unsafe_principal)]


def _service(request: Request) -> SkillPublications:
    service = getattr(request.app.state, "skill_publications", None)
    if service is None:
        raise HTTPException(503, "skill publications unavailable")
    return service


Publications = Annotated[SkillPublications, Depends(_service)]


def _report_service(request: Request) -> SkillReports:
    service = getattr(request.app.state, "skill_reports", None)
    if service is None:
        raise HTTPException(503, "skill reports unavailable")
    return service


Reports = Annotated[SkillReports, Depends(_report_service)]


async def _action[Result](operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except AdministrationDenied:
        raise HTTPException(403, "skill review requires administrator access") from None
    except SkillDraftUnavailable:
        raise HTTPException(404, "skill unavailable") from None
    except SkillDraftConflict:
        raise HTTPException(409, "skill version conflicts with current state") from None
    except ValueError:
        raise HTTPException(422, "invalid skill request") from None


@router.get(
    "/learning/agent/skill-drafts/{draft_id}/releases", response_model=SkillVersionReviewPage
)
async def author_versions(
    draft_id: UUID, service: Publications, principal: Reader
) -> SkillVersionReviewPage:
    return await _action(service.author_versions(principal, draft_id))


@router.post(
    "/learning/agent/skill-drafts/{draft_id}/releases",
    response_model=SkillVersionReview,
    status_code=201,
)
async def submit_version(
    draft_id: UUID, body: SubmitSkillVersion, service: Publications, principal: Writer
) -> SkillVersionReview:
    return await _action(service.submit(principal, draft_id, body))


@router.post(
    "/learning/agent/skill-versions/{version_id}/withdraw", response_model=SkillVersionReview
)
async def withdraw_version(
    version_id: UUID, service: Publications, principal: Writer
) -> SkillVersionReview:
    return await _action(service.withdraw(principal, version_id))


@router.post(
    "/learning/agent/skill-versions/{version_id}/archive", response_model=SkillVersionReview
)
async def archive_version(
    version_id: UUID, service: Publications, principal: Writer
) -> SkillVersionReview:
    return await _action(service.archive(principal, version_id))


@router.get("/learning/agent/skill-library", response_model=PublishedSkillPage)
async def discover_skills(
    service: Publications,
    principal: Reader,
    search: Annotated[str, Query(max_length=80)] = "",
    purpose: AgentPurpose | None = None,
    favorites_only: bool = False,
) -> PublishedSkillPage:
    return await _action(service.discover(principal, search.strip(), purpose, favorites_only))


@router.get("/learning/agent/skill-library/{version_id}", response_model=PublishedSkill)
async def published_skill(
    version_id: UUID, service: Publications, principal: Reader
) -> PublishedSkill:
    return await _action(service.published(principal, version_id))


@router.put("/learning/agent/skill-library/{skill_id}/favorite", response_model=ChangeSkillFavorite)
async def change_favorite(
    skill_id: UUID, body: ChangeSkillFavorite, service: Publications, principal: Writer
) -> ChangeSkillFavorite:
    saved = await _action(service.set_favorite(principal, skill_id, body.favorited))
    return ChangeSkillFavorite(favorited=saved)


@router.get("/learning/agent/skill-library/{version_id}/rating", response_model=SkillVersionRating)
async def version_rating(
    version_id: UUID, service: Publications, principal: Reader
) -> SkillVersionRating:
    return await _action(service.rating(principal, version_id))


@router.put("/learning/agent/skill-library/{version_id}/rating", response_model=SkillVersionRating)
async def rate_version(
    version_id: UUID, body: ChangeSkillRating, service: Publications, principal: Writer
) -> SkillVersionRating:
    return await _action(service.set_rating(principal, version_id, body.score))


@router.delete(
    "/learning/agent/skill-library/{version_id}/rating", response_model=SkillVersionRating
)
async def retract_rating(
    version_id: UUID, service: Publications, principal: Writer
) -> SkillVersionRating:
    return await _action(service.set_rating(principal, version_id, None))


@router.get("/admin/skill-reviews", response_model=SkillVersionReviewPage)
async def review_queue(
    service: Publications, principal: Reader, state: PublicationState = "pending"
) -> SkillVersionReviewPage:
    return await _action(service.pending(principal, state))


@router.post("/admin/skill-reviews/{version_id}", response_model=SkillVersionReview)
async def decide_version(
    version_id: UUID, body: ReviewSkillVersion, service: Publications, principal: Writer
) -> SkillVersionReview:
    return await _action(service.decide(principal, version_id, body))


@router.post("/admin/skill-reviews/{version_id}/disable", response_model=SkillVersionReview)
async def disable_skill_version(
    version_id: UUID, body: DisableSkillVersion, service: Publications, principal: Writer
) -> SkillVersionReview:
    return await _action(service.disable(principal, version_id, body))


@router.get("/learning/agent/skill-library/{version_id}/reports", response_model=SkillReportPage)
async def own_skill_reports(
    version_id: UUID, service: Reports, principal: Reader
) -> SkillReportPage:
    return await _action(service.own(principal, version_id))


@router.post(
    "/learning/agent/skill-library/{version_id}/reports",
    response_model=SkillReportView,
    status_code=201,
)
async def submit_skill_report(
    version_id: UUID, body: SubmitSkillReport, service: Reports, principal: Writer
) -> SkillReportView:
    return await _action(service.submit(principal, version_id, body))


@router.get("/admin/skill-reports", response_model=SkillReportReviewPage)
async def skill_report_reviews(
    service: Reports, principal: Reader, state: SkillReportState = "pending"
) -> SkillReportReviewPage:
    return await _action(service.reviews(principal, state))


@router.post("/admin/skill-reports/{report_id}", response_model=SkillReportReviewView)
async def decide_skill_report(
    report_id: UUID, body: ReviewSkillReport, service: Reports, principal: Writer
) -> SkillReportReviewView:
    return await _action(service.decide(principal, report_id, body))
