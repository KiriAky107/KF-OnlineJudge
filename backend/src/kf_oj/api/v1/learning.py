"""Authenticated personal learning and training endpoints."""

from collections.abc import Awaitable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.learning import (
    ConfirmTrainingPublication,
    CreateTraining,
    CreateTrainingFollowUp,
    LearningOverview,
    LearningParameters,
    LearningRecommendations,
    ProblemNoteView,
    TeamTrainingReview,
    TrainingDetail,
    TrainingPage,
    TrainingPublicationDetail,
    TrainingPublicationPage,
    UpdateTrainingFeedback,
    UpdateTrainingNote,
)
from kf_oj.contracts.learning_model import LearningHintCeiling
from kf_oj.modules.identity.public import AdministrationDenied, AuthenticationFailed, Principal
from kf_oj.modules.learning.public import (
    IdempotencyConflict,
    LearningUnavailable,
    NoCandidates,
    TrainingSourceUnavailable,
)
from kf_oj.modules.teams.public import TeamPermissionDenied

router = APIRouter(tags=["learning"])


def service(request: Request):
    value = getattr(request.app.state, "learning", None)
    if value is None:
        raise HTTPException(503, "learning service unavailable")
    return value


@router.get("/learning/overview", response_model=LearningOverview)
async def overview(request: Request, principal: Annotated[Principal, Depends(current_principal)]):
    return await service(request).overview(principal)


@router.post("/learning/recommendations", response_model=LearningRecommendations)
async def recommendations(
    body: LearningParameters,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    return await service(request).recommend(principal, body)


@router.get("/learning/problem-notes/{problem_version_id}", response_model=ProblemNoteView)
async def problem_note(
    problem_version_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    try:
        return await service(request).problem_note(principal, problem_version_id)
    except LearningUnavailable:
        raise HTTPException(404, "problem note unavailable") from None


@router.put("/learning/problem-notes/{problem_version_id}", response_model=ProblemNoteView)
async def save_problem_note(
    problem_version_id: UUID,
    body: UpdateTrainingNote,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await service(request).save_problem_note(principal, problem_version_id, body.note)
    except LearningUnavailable:
        raise HTTPException(404, "problem note unavailable") from None


@router.get("/trainings", response_model=TrainingPage)
async def trainings(request: Request, principal: Annotated[Principal, Depends(current_principal)]):
    return await service(request).page(principal)


@router.post("/trainings", response_model=TrainingDetail, status_code=201)
async def create_training(
    body: CreateTraining,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await service(request).create(principal, body)
    except IdempotencyConflict:
        raise HTTPException(409, "idempotency request conflict") from None
    except LearningUnavailable:
        raise HTTPException(409, "recommendation changed; regenerate") from None
    except NoCandidates:
        raise HTTPException(422, "no available matching problems") from None


@router.get("/trainings/{training_id}", response_model=TrainingDetail)
async def training_detail(
    training_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    try:
        return await service(request).detail(principal, training_id)
    except LearningUnavailable:
        raise HTTPException(404, "training unavailable") from None


@router.post(
    "/trainings/{training_id}/publish-official",
    response_model=TrainingPublicationDetail,
    status_code=201,
)
async def publish_official_training(
    training_id: UUID,
    body: ConfirmTrainingPublication,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await service(request).publish_official(
            principal, training_id, request.app.state.identity
        )
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except AdministrationDenied:
        raise HTTPException(403, "administrator required") from None
    except TrainingSourceUnavailable:
        raise HTTPException(404, "training unavailable") from None
    except LearningUnavailable:
        raise HTTPException(409, "official training includes unavailable problems") from None


@router.get("/official-trainings", response_model=TrainingPublicationPage)
async def official_trainings(request: Request):
    return await service(request).official_page()


@router.get("/official-trainings/{training_id}", response_model=TrainingPublicationDetail)
async def official_training(training_id: UUID, request: Request):
    try:
        return await service(request).official_detail(training_id)
    except TrainingSourceUnavailable:
        raise HTTPException(404, "official training unavailable") from None


@router.post(
    "/official-trainings/{training_id}/start", response_model=TrainingDetail, status_code=201
)
async def start_official_training(
    training_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await service(request).start_official(
            principal, training_id, request.app.state.identity
        )
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except TrainingSourceUnavailable:
        raise HTTPException(404, "official training unavailable") from None
    except (IdempotencyConflict, LearningUnavailable):
        raise HTTPException(409, "official training unavailable") from None


async def _team_training[Result](operation: Awaitable[Result]) -> Result:
    try:
        return await operation
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except TeamPermissionDenied:
        raise HTTPException(403, "team role required") from None
    except TrainingSourceUnavailable:
        raise HTTPException(404, "team training unavailable") from None
    except (IdempotencyConflict, LearningUnavailable):
        raise HTTPException(409, "team training unavailable") from None


@router.post(
    "/trainings/{training_id}/publish-team/{team_id}",
    response_model=TrainingPublicationDetail,
    status_code=201,
)
async def publish_team_training(
    training_id: UUID,
    team_id: UUID,
    body: ConfirmTrainingPublication,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    return await _team_training(
        service(request).publish_team(principal, team_id, training_id, request.app.state.identity)
    )


@router.get("/teams/{team_id}/trainings", response_model=TrainingPublicationPage)
async def team_trainings(
    team_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    return await _team_training(
        service(request).team_training_page(principal, team_id, request.app.state.identity)
    )


@router.get("/teams/{team_id}/trainings/{training_id}", response_model=TrainingPublicationDetail)
async def team_training_detail(
    team_id: UUID,
    training_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    return await _team_training(
        service(request).team_training_detail(
            principal, team_id, training_id, request.app.state.identity
        )
    )


@router.get("/teams/{team_id}/trainings/{training_id}/review", response_model=TeamTrainingReview)
async def team_training_review(
    team_id: UUID,
    training_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
):
    return await _team_training(
        service(request).team_review(principal, team_id, training_id, request.app.state.identity)
    )


@router.post(
    "/teams/{team_id}/trainings/{training_id}/start", response_model=TrainingDetail, status_code=201
)
async def start_team_training(
    team_id: UUID,
    training_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    return await _team_training(
        service(request).start_team_training(
            principal, team_id, training_id, request.app.state.identity
        )
    )


@router.post("/trainings/{training_id}/follow-ups", response_model=TrainingDetail, status_code=201)
async def create_follow_up(
    training_id: UUID,
    body: CreateTrainingFollowUp,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await service(request).create(principal, body, source_plan_id=training_id)
    except TrainingSourceUnavailable:
        raise HTTPException(404, "training unavailable") from None
    except IdempotencyConflict:
        raise HTTPException(409, "idempotency request conflict") from None
    except LearningUnavailable:
        raise HTTPException(409, "recommendation changed; regenerate") from None
    except NoCandidates:
        raise HTTPException(422, "no available matching problems") from None


@router.put("/trainings/{training_id}/hint-ceiling", response_model=LearningHintCeiling)
async def set_training_hint_ceiling(
    training_id: UUID,
    body: LearningHintCeiling,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await service(request).set_hint_ceiling(
            principal, training_id, body, request.app.state.identity
        )
    except AuthenticationFailed:
        raise HTTPException(401, "authentication required") from None
    except LearningUnavailable:
        raise HTTPException(404, "training unavailable") from None


@router.patch("/trainings/{training_id}/items/{item_id}", response_model=TrainingDetail)
async def update_note(
    training_id: UUID,
    item_id: UUID,
    body: UpdateTrainingNote,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await service(request).note(principal, training_id, item_id, body)
    except LearningUnavailable:
        raise HTTPException(404, "training unavailable") from None


@router.put("/trainings/{training_id}/items/{item_id}/feedback", response_model=TrainingDetail)
async def update_feedback(
    training_id: UUID,
    item_id: UUID,
    body: UpdateTrainingFeedback,
    request: Request,
    principal: Annotated[Principal, Depends(unsafe_principal)],
):
    try:
        return await service(request).rate(principal, training_id, item_id, body)
    except LearningUnavailable:
        raise HTTPException(404, "training unavailable") from None
