"""Authenticated personal learning and training endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.learning import (
    CreateTraining,
    CreateTrainingFollowUp,
    LearningOverview,
    LearningParameters,
    LearningRecommendations,
    TrainingDetail,
    TrainingPage,
    UpdateTrainingFeedback,
    UpdateTrainingNote,
)
from kf_oj.modules.identity.public import Principal
from kf_oj.modules.learning.public import (
    IdempotencyConflict,
    LearningUnavailable,
    NoCandidates,
    TrainingSourceUnavailable,
)

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
