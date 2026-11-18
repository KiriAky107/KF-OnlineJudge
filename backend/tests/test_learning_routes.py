"""Small HTTP boundary checks, separate from real PostgreSQL domain acceptance."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from kf_oj.api.app import create_app
from kf_oj.api.dependencies import current_principal, unsafe_principal
from kf_oj.contracts.learning import CreateTraining, LearningParameters, UpdateTrainingNote
from kf_oj.core.settings import Settings
from kf_oj.modules.identity.public import AuthenticationFailed, Principal
from kf_oj.modules.learning.public import IdempotencyConflict, LearningUnavailable, NoCandidates


def test_learning_inputs_are_small_and_explicit():
    assert LearningParameters().count == 5
    for data in ({"count": 11}, {"count": 0}, {"difficulty": "guessed"}, {"tags": [""]}):
        with pytest.raises(ValidationError):
            LearningParameters(**data)
    with pytest.raises(ValidationError):
        UpdateTrainingNote(note="x" * 1001)
    with pytest.raises(ValidationError):
        CreateTraining(title="x")


def test_learning_http_auth_privacy_and_owner_errors():
    async def run():
        app = create_app(Settings(_env_file=None))
        principal = Principal(uuid4(), "learning-user")

        class Identity:
            async def authenticate(self, token):
                raise AuthenticationFailed

        app.state.identity = Identity()

        class Service:
            async def detail(self, who, training_id):
                assert who == principal
                raise LearningUnavailable

            async def note(self, who, training_id, item_id, body):
                assert who == principal
                raise LearningUnavailable

            async def create(self, who, body):
                assert who == principal
                if body.title == "conflict":
                    raise IdempotencyConflict
                raise NoCandidates

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            # With no authenticated principal the domain is never consulted.
            response = await client.get("/api/v1/trainings")
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
            app.dependency_overrides[current_principal] = lambda: principal
            app.dependency_overrides[unsafe_principal] = lambda: principal
            assert (await client.get("/api/v1/learning/overview")).status_code == 503
            app.state.learning = Service()
            url = f"/api/v1/trainings/{uuid4()}"
            assert (await client.get(url)).status_code == 404
            assert (
                await client.patch(url + f"/items/{uuid4()}", json={"note": "private"})
            ).status_code == 404
            for title, status in (("conflict", 409), ("empty", 422)):
                response = await client.post(
                    "/api/v1/trainings", json={"title": title, "idempotency_key": str(uuid4())}
                )
                assert response.status_code == status
                assert response.headers["cache-control"] == "no-store"

    asyncio.run(run())
