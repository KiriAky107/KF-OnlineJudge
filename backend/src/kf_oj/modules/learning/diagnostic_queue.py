"""Shared queue for explicitly authorized public-sample and registered batch diagnostics."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import Field
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.agent_api import DiagnosticMistakeEvidence
from kf_oj.contracts.diagnostic import (
    CLAIM_SECONDS,
    DiagnosticMode,
    DiagnosticObservation,
    UbsanObservation,
)
from kf_oj.contracts.diagnostic_batch import (
    BatchDiagnosticOption,
    BatchDiagnosticSummary,
    BatchPurpose,
    ConfirmBatchDiagnostic,
    RegisteredBatchOffer,
)
from kf_oj.contracts.judging import Digest, FailureCode, Verdict, WireModel
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactError, ArtifactStore
from kf_oj.modules.contests.public import AgentUnavailableDuringContest, require_agent_access
from kf_oj.modules.identity.public import (
    AcceptedSession,
    ActiveSessionPort,
    AuthenticationFailed,
    Principal,
    accepted_session_access,
)
from kf_oj.modules.problems.public import (
    ProblemPort,
    ProblemUnavailable,
    diagnostic_version_available,
)
from kf_oj.modules.submissions.public import DiagnosticSourceReference

from .agent_models import AgentRunRow, AgentSessionRow, AgentWorkflowRow
from .diagnostic_batch import DiagnosticBatch
from .diagnostic_inputs import (
    DiagnosticInputsUnavailable,
    PublicSampleDiagnosticInputs,
    resolve_confirmed_diagnostic_source,
    resolve_owned_diagnostic_source,
    resolve_public_sample_diagnostic_inputs,
)
from .diagnostic_models import DiagnosticRequestRow
from .diagnostic_runtime import diagnostic_configuration

_QUEUE_DURATION = timedelta(minutes=2)
_EXECUTION_DURATION = timedelta(seconds=CLAIM_SECONDS)
_MAX_QUEUED = 8
_MAX_ACCOUNT_PER_HOUR = 3
_MAX_SERVICE_PER_HOUR = 24
_IN_FLIGHT = ("running", "cancel_pending")
type RequestSnapshot = dict[str, str | int | UUID | dict | None]


class DiagnosticBusy(ValueError):
    pass


class DiagnosticRequestConflict(ValueError):
    pass


class DiagnosticExecutorSettings(WireModel):
    factory_config_sha256: Digest
    admission_sha256: Digest


class DiagnosticExecutionProfile(DiagnosticExecutorSettings):
    tool: Literal["public-sample-v1", "registered-batch-v1"]
    configuration_sha256: Digest
    sandbox_wall_budget_us: Annotated[int, Field(strict=True, gt=0)]


class DiagnosticReviewContext(WireModel):
    run_id: UUID
    skill_version_id: UUID
    configuration_sha256: Digest


@dataclass(frozen=True, slots=True)
class DiagnosticRequest:
    request_id: UUID
    state: str
    queue_deadline: datetime
    mode: DiagnosticMode
    sample_ordinal: int
    observation: DiagnosticObservation | None = None
    ubsan: UbsanObservation | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticClaim:
    request_id: UUID
    execution_id: UUID
    execution_deadline: datetime
    inputs: PublicSampleDiagnosticInputs
    mode: DiagnosticMode = "optimization"


@dataclass(frozen=True, slots=True)
class DiagnosticExecutionInputs:
    inputs: PublicSampleDiagnosticInputs
    configuration: v2.LanguageConfiguration
    source: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class BatchDiagnosticRequest:
    request_id: UUID
    state: str
    queue_deadline: datetime
    summary: BatchDiagnosticSummary | None = None


@dataclass(frozen=True, slots=True)
class BatchDiagnosticClaim:
    request_id: UUID
    execution_id: UUID
    execution_deadline: datetime
    account_id: UUID
    source: DiagnosticSourceReference
    manifest_sha256: str
    purpose: BatchPurpose


@dataclass(frozen=True, slots=True)
class BatchDiagnosticExecutionInputs:
    batch: DiagnosticBatch
    source: bytes = field(repr=False)


def _record_execution_profile(
    row: DiagnosticRequestRow,
    settings: DiagnosticExecutorSettings | None,
    configuration: v2.LanguageConfiguration,
    checker: v2.LanguageConfiguration | None = None,
) -> None:
    if settings is None:
        return
    payload = {
        "runtime": configuration.model_dump(mode="json"),
        "input_checker": checker.model_dump(mode="json") if checker else None,
    }
    profile = DiagnosticExecutionProfile(
        **settings.model_dump(),
        tool="registered-batch-v1" if row.mode == "batch" else "public-sample-v1",
        configuration_sha256=sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        sandbox_wall_budget_us=configuration.job_wall_time_us,
    ).model_dump(mode="json")
    if row.execution_profile is not None and row.execution_profile != profile:
        raise DiagnosticInputsUnavailable("diagnostic execution configuration changed")
    row.execution_profile = profile


def _source_snapshot(
    source: DiagnosticSourceReference, proof: AcceptedSession
) -> dict[str, str | int | UUID]:
    return {
        "submission_id": source.submission_id,
        "problem_version_id": source.problem_version_id,
        "language": source.language,
        "source_digest": source.artifact.digest,
        "source_bytes": source.artifact.bytes,
        "authorization_version": proof.authorization_version,
        "session_proof": proof.session_proof,
    }


def _snapshot(
    inputs: PublicSampleDiagnosticInputs, proof: AcceptedSession
) -> dict[str, str | int | UUID]:
    sample = inputs.sample
    data = ArtifactStore.reference(sample.input_bytes)
    expected = ArtifactStore.reference(sample.expected_bytes)
    return _source_snapshot(inputs.source, proof) | {
        "sample_ordinal": sample.ordinal,
        "input_digest": data.digest,
        "input_bytes": data.bytes,
        "expected_digest": expected.digest,
        "expected_bytes": expected.bytes,
    }


def _matches(row: DiagnosticRequestRow, snapshot: RequestSnapshot) -> bool:
    return all(getattr(row, key) == value for key, value in snapshot.items())


async def _review_context(
    db: AsyncSession, owner_id: UUID, submission_id: UUID, run_id: UUID | None
) -> dict | None:
    if run_id is None:
        return None
    context = (
        await db.execute(
            select(AgentRunRow.skill_version_id, AgentRunRow.configuration_digest)
            .join(AgentSessionRow, AgentSessionRow.id == AgentRunRow.session_id)
            .join(AgentWorkflowRow, AgentWorkflowRow.run_id == AgentRunRow.id)
            .where(
                AgentRunRow.id == run_id,
                AgentRunRow.state == "succeeded",
                AgentSessionRow.owner_id == owner_id,
                AgentSessionRow.purpose == "submission_review",
                AgentWorkflowRow.result["anchor_submission_id"].astext == str(submission_id),
            )
        )
    ).one_or_none()
    if context is None:
        raise DiagnosticInputsUnavailable("diagnostic review unavailable")
    return DiagnosticReviewContext(
        run_id=run_id,
        skill_version_id=context.skill_version_id,
        configuration_sha256=context.configuration_digest,
    ).model_dump(mode="json")


def _scoped_execution_id(scope: UUID, owner: UUID, request: UUID) -> UUID:
    # A request is claimed at most once. Its execution ID binds that claim to the broker scope.
    return uuid5(scope, f"{owner}/{request}")


def _request(row: DiagnosticRequestRow, state: str | None = None) -> DiagnosticRequest:
    observation = None
    if row.state == "observed" and row.mode == "optimization":
        observation = DiagnosticObservation(
            o2=Verdict(row.o2), o0=Verdict(row.o0) if row.o0 is not None else None
        )
    return DiagnosticRequest(
        row.id,
        state or row.state,
        row.queue_deadline,
        row.mode,
        row.sample_ordinal,
        observation,
        UbsanObservation.model_validate(row.ubsan) if row.ubsan is not None else None,
    )


def _batch_request(row: DiagnosticRequestRow, state: str | None = None) -> BatchDiagnosticRequest:
    summary = None
    if row.state == "observed":
        summary = BatchDiagnosticSummary(
            schema_version=1,
            submission_id=row.submission_id,
            problem_version_id=row.problem_version_id,
            purpose=row.batch_purpose,
            outcome=row.batch_outcome,
        )
    return BatchDiagnosticRequest(row.id, state or row.state, row.queue_deadline, summary)


async def _lock(db: AsyncSession) -> None:
    # Shared application lock namespace, independent of learning tool and model budgets.
    await db.execute(text("SELECT pg_advisory_xact_lock(1262907471, 2)"))


async def _expire_queued(db: AsyncSession, now: datetime) -> None:
    await db.execute(
        update(DiagnosticRequestRow)
        .where(
            DiagnosticRequestRow.state == "queued",
            DiagnosticRequestRow.queue_deadline <= now,
        )
        .values(state="expired", finished_at=now)
    )


async def _authorized_inputs(
    db: AsyncSession, row: DiagnosticRequestRow
) -> PublicSampleDiagnosticInputs | None:
    proof = AcceptedSession(row.authorization_version, row.session_proof)
    actor = Principal(row.owner_id, "", authorization_version=row.authorization_version)
    try:
        inputs = await resolve_public_sample_diagnostic_inputs(
            db,
            accepted_session_access(row.owner_id, proof),
            actor,
            submission_id=row.submission_id,
            problem_version_id=row.problem_version_id,
            sample_ordinal=row.sample_ordinal,
            source_execution_confirmed=True,
        )
    except (AuthenticationFailed, AgentUnavailableDuringContest, DiagnosticInputsUnavailable):
        return None
    return inputs if _matches(row, _snapshot(inputs, proof)) else None


async def _authorized_batch_source(
    db: AsyncSession, row: DiagnosticRequestRow
) -> DiagnosticSourceReference | None:
    proof = AcceptedSession(row.authorization_version, row.session_proof)
    actor = Principal(row.owner_id, "", authorization_version=row.authorization_version)
    try:
        source = await resolve_confirmed_diagnostic_source(
            db,
            accepted_session_access(row.owner_id, proof),
            actor,
            submission_id=row.submission_id,
            problem_version_id=row.problem_version_id,
            source_execution_confirmed=True,
        )
    except (AuthenticationFailed, AgentUnavailableDuringContest, DiagnosticInputsUnavailable):
        return None
    return source if _matches(row, _source_snapshot(source, proof)) else None


class DiagnosticQueue:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        identity: ActiveSessionPort,
        *,
        ubsan_enabled: bool = False,
        batch_offers: tuple[RegisteredBatchOffer, ...] | None = None,
        load_batch: Callable[[AsyncSession, str], Awaitable[DiagnosticBatch]] | None = None,
    ) -> None:
        self._sessions = sessions
        self._identity = identity
        self.ubsan_enabled = ubsan_enabled
        self._batch_offers = (
            {offer.manifest_sha256: offer for offer in batch_offers}
            if batch_offers is not None
            else None
        )
        self._load_batch = load_batch

    async def executor_has_access(self, db: AsyncSession) -> bool:
        """Check the broker's queue grants without claiming or changing any request."""
        table = "public." + DiagnosticRequestRow.__tablename__
        fields = (
            DiagnosticRequestRow.state,
            DiagnosticRequestRow.execution_id,
            DiagnosticRequestRow.execution_started_at,
            DiagnosticRequestRow.execution_deadline,
            DiagnosticRequestRow.execution_profile,
            DiagnosticRequestRow.finished_at,
            DiagnosticRequestRow.failure_code,
            DiagnosticRequestRow.o2,
            DiagnosticRequestRow.o0,
        )
        if self.ubsan_enabled:
            fields += (DiagnosticRequestRow.ubsan,)
        if self._load_batch is not None:
            fields += (DiagnosticRequestRow.batch_outcome,)
        await db.execute(select(DiagnosticRequestRow).limit(0))
        return (
            await db.scalar(
                select(
                    and_(
                        *(func.has_column_privilege(table, field.key, "UPDATE") for field in fields)
                    )
                )
            )
            is True
        )

    @property
    def has_registered_batches(self) -> bool:
        return bool(self._batch_offers)

    async def enqueue(
        self,
        principal: Principal,
        *,
        request_id: UUID,
        submission_id: UUID,
        problem_version_id: UUID,
        sample_ordinal: int,
        source_execution_confirmed: bool,
        mode: DiagnosticMode = "optimization",
        review_run_id: UUID | None = None,
    ) -> DiagnosticRequest:
        async with self._sessions.begin() as db:
            await _lock(db)
            inputs = await resolve_public_sample_diagnostic_inputs(
                db,
                self._identity,
                principal,
                submission_id=submission_id,
                problem_version_id=problem_version_id,
                sample_ordinal=sample_ordinal,
                source_execution_confirmed=source_execution_confirmed,
            )
            row = await self._admit(
                db,
                principal.account_id,
                request_id,
                mode,
                _snapshot(inputs, AcceptedSession.from_principal(principal))
                | {
                    "review_context": await _review_context(
                        db, principal.account_id, submission_id, review_run_id
                    )
                },
            )
            return _request(row)

    async def enqueue_batch(
        self, principal: Principal, *, request_id: UUID, request: ConfirmBatchDiagnostic
    ) -> BatchDiagnosticRequest:
        async with self._sessions.begin() as db:
            await _lock(db)
            source = await resolve_confirmed_diagnostic_source(
                db,
                self._identity,
                principal,
                submission_id=request.submission_id,
                problem_version_id=request.problem_version_id,
                source_execution_confirmed=request.source_execution_confirmed,
            )
            purpose = await self._batch_purpose(db, source, request.manifest_sha256)
            row = await self._admit(
                db,
                principal.account_id,
                request_id,
                "batch",
                _source_snapshot(source, AcceptedSession.from_principal(principal))
                | {
                    "batch_manifest_digest": request.manifest_sha256,
                    "batch_purpose": purpose,
                    "review_context": await _review_context(
                        db, principal.account_id, request.submission_id, request.review_run_id
                    ),
                },
            )
            return _batch_request(row)

    async def offered_batches(
        self, principal: Principal, submission_id: UUID, problem_version_id: UUID
    ) -> tuple[BatchDiagnosticOption, ...]:
        async with self._sessions.begin() as db:
            source = await resolve_owned_diagnostic_source(
                db, self._identity, principal, submission_id, problem_version_id
            )
            if not await diagnostic_version_available(db, source.problem_version_id):
                raise DiagnosticInputsUnavailable("diagnostic input unavailable")
            return tuple(
                BatchDiagnosticOption(
                    manifest_sha256=offer.manifest_sha256, title=offer.title, purpose=offer.purpose
                )
                for offer in (self._batch_offers or {}).values()
                if offer.problem_version_id == source.problem_version_id
                and offer.language == source.language
            )

    def _batch_offer(
        self, source: DiagnosticSourceReference, digest: str
    ) -> RegisteredBatchOffer | None:
        offer = (self._batch_offers or {}).get(digest)
        if (
            offer is None
            or offer.problem_version_id != source.problem_version_id
            or offer.language != source.language
        ):
            return None
        return offer

    async def _batch_purpose(
        self, db: AsyncSession, source: DiagnosticSourceReference, digest: str
    ) -> BatchPurpose:
        if self._batch_offers is None and self._load_batch is not None:
            batch = await self._registered_batch(db, source, digest)
            return batch.manifest.purpose
        offer = self._batch_offer(source, digest)
        if offer is None or not await diagnostic_version_available(db, source.problem_version_id):
            raise DiagnosticInputsUnavailable("registered diagnostic batch unavailable")
        return offer.purpose

    async def _admit(
        self,
        db: AsyncSession,
        account_id: UUID,
        request_id: UUID,
        mode: DiagnosticMode | Literal["batch"],
        snapshot: RequestSnapshot,
    ) -> DiagnosticRequestRow:
        now = await db.scalar(select(func.clock_timestamp()))
        await _expire_queued(db, now)
        existing = await db.get(DiagnosticRequestRow, (account_id, request_id))
        if existing is not None:
            if existing.mode != mode or not _matches(existing, snapshot):
                raise DiagnosticRequestConflict("diagnostic request already has different inputs")
            return existing
        if mode == "ubsan" and not self.ubsan_enabled:
            raise DiagnosticInputsUnavailable("UBSan diagnostics are unavailable")
        owner = DiagnosticRequestRow.owner_id == account_id
        queued = DiagnosticRequestRow.state == "queued"
        active = or_(queued, DiagnosticRequestRow.state.in_(_IN_FLIGHT))
        hour = DiagnosticRequestRow.created_at > now - timedelta(hours=1)
        queued_count, owner_active, owner_recent, recent = (
            await db.execute(
                select(
                    func.count().filter(queued),
                    func.count().filter(owner, active),
                    func.count().filter(owner, hour),
                    func.count().filter(hour),
                )
                .select_from(DiagnosticRequestRow)
                .where(or_(active, hour))
            )
        ).one()
        if (
            queued_count >= _MAX_QUEUED
            or owner_active
            or owner_recent >= _MAX_ACCOUNT_PER_HOUR
            or recent >= _MAX_SERVICE_PER_HOUR
        ):
            raise DiagnosticBusy("diagnostic request limit reached")
        row = DiagnosticRequestRow(
            id=request_id,
            owner_id=account_id,
            state="queued",
            created_at=now,
            queue_deadline=now + _QUEUE_DURATION,
            mode=mode,
            **snapshot,
        )
        db.add(row)
        return row

    async def _registered_batch(
        self, db: AsyncSession, source: DiagnosticSourceReference, digest: str
    ) -> DiagnosticBatch:
        offer = self._batch_offer(source, digest)
        if self._load_batch is None or (self._batch_offers is not None and offer is None):
            raise DiagnosticInputsUnavailable("registered diagnostic batch unavailable")
        try:
            batch = await self._load_batch(db, digest)
        except (OSError, ValueError):
            raise DiagnosticInputsUnavailable("registered diagnostic batch unavailable") from None
        if (
            batch.manifest_sha256 != digest
            or batch.manifest.problem_version_id != source.problem_version_id
            or batch.manifest.language != source.language
            or (offer is not None and batch.manifest.purpose != offer.purpose)
        ):
            raise DiagnosticInputsUnavailable("registered diagnostic batch unavailable")
        return batch

    async def _owned_request(
        self, db: AsyncSession, principal: Principal, request_id: UUID, *, batch: bool = False
    ) -> DiagnosticRequestRow:
        await self._identity.require_active_session(db, principal)
        await require_agent_access(db, principal.account_id)
        row = await db.get(DiagnosticRequestRow, (principal.account_id, request_id))
        proof = AcceptedSession.from_principal(principal)
        if (
            row is None
            or (row.mode == "batch") != batch
            or (row.authorization_version, row.session_proof)
            != (proof.authorization_version, proof.session_proof)
        ):
            raise DiagnosticInputsUnavailable("diagnostic request unavailable")
        return row

    async def read(self, principal: Principal, request_id: UUID) -> DiagnosticRequest:
        async with self._sessions.begin() as db:
            row = await self._owned_request(db, principal, request_id)
            if await _authorized_inputs(db, row) is None:
                raise DiagnosticInputsUnavailable("diagnostic request unavailable")
            now = await db.scalar(select(func.clock_timestamp()))
            if row.state == "queued" and row.queue_deadline <= now:
                return _request(row, "expired")
            return _request(row)

    async def read_batch(self, principal: Principal, request_id: UUID) -> BatchDiagnosticRequest:
        async with self._sessions.begin() as db:
            row = await self._owned_request(db, principal, request_id, batch=True)
            source = await _authorized_batch_source(db, row)
            if source is None or not await diagnostic_version_available(db, row.problem_version_id):
                raise DiagnosticInputsUnavailable("diagnostic request unavailable")
            if self._batch_offers is not None:
                offer = self._batch_offer(source, row.batch_manifest_digest)
                if row.state == "observed" and (
                    offer is None or offer.purpose != row.batch_purpose
                ):
                    return BatchDiagnosticRequest(row.id, "unavailable", row.queue_deadline)
            elif row.batch_purpose != await self._batch_purpose(
                db, source, row.batch_manifest_digest
            ):
                raise DiagnosticInputsUnavailable("diagnostic request unavailable")
            now = await db.scalar(select(func.clock_timestamp()))
            state = "expired" if row.state == "queued" and row.queue_deadline <= now else row.state
            return _batch_request(row, state)

    async def mistake_evidence(
        self, db: AsyncSession, principal: Principal, request_id: UUID
    ) -> DiagnosticMistakeEvidence:
        row = await self._owned_request(db, principal, request_id)
        if row.state != "observed" or row.mode != "ubsan":
            raise DiagnosticInputsUnavailable("diagnostic finding unavailable")
        observation = UbsanObservation.model_validate(row.ubsan)
        if observation.finding is None or await _authorized_inputs(db, row) is None:
            raise DiagnosticInputsUnavailable("diagnostic finding unavailable")
        return DiagnosticMistakeEvidence(
            request_id=row.id,
            submission_id=row.submission_id,
            problem_version_id=row.problem_version_id,
            sample_ordinal=row.sample_ordinal,
            finding=observation.finding,
        )

    async def cancel(self, principal: Principal, request_id: UUID) -> DiagnosticRequest:
        return await self._cancel(principal, request_id, batch=False, view=_request)

    async def cancel_batch(self, principal: Principal, request_id: UUID) -> BatchDiagnosticRequest:
        return await self._cancel(principal, request_id, batch=True, view=_batch_request)

    async def _cancel[T](
        self,
        principal: Principal,
        request_id: UUID,
        *,
        batch: bool,
        view: Callable[[DiagnosticRequestRow], T],
    ) -> T:
        async with self._sessions.begin() as db:
            await _lock(db)
            row = await self._owned_request(db, principal, request_id, batch=batch)
            if row.state == "queued":
                now = await db.scalar(select(func.clock_timestamp()))
                row.state = "expired" if row.queue_deadline <= now else "cancelled"
                row.finished_at = now
            elif row.state == "running":
                row.state = "cancel_pending"
            return view(row)

    async def _verified_batch(
        self, db: AsyncSession, row: DiagnosticRequestRow
    ) -> tuple[DiagnosticSourceReference, DiagnosticBatch] | None:
        source = await _authorized_batch_source(db, row)
        if source is None:
            return None
        try:
            batch = await self._registered_batch(db, source, row.batch_manifest_digest)
        except DiagnosticInputsUnavailable:
            return None
        return (source, batch) if row.batch_purpose == batch.manifest.purpose else None

    async def claim(
        self, scope: UUID | None = None
    ) -> DiagnosticClaim | BatchDiagnosticClaim | None:
        """Reserve a single attempt for a trusted executor after rechecking the original consent."""
        async with self._sessions.begin() as db:
            await _lock(db)
            now = await db.scalar(select(func.clock_timestamp()))
            await _expire_queued(db, now)
            # An overdue or interrupted runner holds the slot until trusted cleanup is confirmed.
            if (
                await db.scalar(
                    select(DiagnosticRequestRow.id).where(
                        DiagnosticRequestRow.state.in_(_IN_FLIGHT)
                    )
                )
                is not None
            ):
                return None
            modes = (
                ("optimization", "ubsan", "batch")
                if self._load_batch is not None or self._batch_offers is not None
                else (
                    "optimization",
                    "ubsan",
                )
            )
            pending = await db.scalars(
                select(DiagnosticRequestRow)
                .where(DiagnosticRequestRow.state == "queued", DiagnosticRequestRow.mode.in_(modes))
                .order_by(
                    DiagnosticRequestRow.created_at,
                    DiagnosticRequestRow.owner_id,
                    DiagnosticRequestRow.id,
                )
                .limit(_MAX_QUEUED)
            )
            for row in pending:
                if row.mode == "ubsan" and not self.ubsan_enabled:
                    row.state = "unavailable"
                    row.finished_at = now
                    continue
                if row.mode == "batch":
                    verified = await self._verified_batch(db, row)
                    inputs = verified[0] if verified is not None else None
                else:
                    inputs = await _authorized_inputs(db, row)
                now = await db.scalar(select(func.clock_timestamp()))
                if inputs is None or row.queue_deadline <= now:
                    row.state = "unavailable" if inputs is None else "expired"
                    row.finished_at = now
                    continue
                row.state = "running"
                row.execution_id = (
                    _scoped_execution_id(scope, row.owner_id, row.id) if scope else uuid4()
                )
                row.execution_started_at = now
                row.execution_deadline = now + _EXECUTION_DURATION
                if isinstance(inputs, DiagnosticSourceReference):
                    return BatchDiagnosticClaim(
                        row.id,
                        row.execution_id,
                        row.execution_deadline,
                        row.owner_id,
                        inputs,
                        row.batch_manifest_digest,
                        row.batch_purpose,
                    )
                return DiagnosticClaim(
                    row.id, row.execution_id, row.execution_deadline, inputs, row.mode
                )
            return None

    async def recover_expired(
        self, scope: UUID, confirm_cleanup: Callable[[], Awaitable[bool]]
    ) -> DiagnosticRequest | BatchDiagnosticRequest | None:
        """The broker holds its inherited execution lock for the entire recovery."""
        async with self._sessions() as db:
            candidate = (
                await db.execute(
                    select(
                        DiagnosticRequestRow.owner_id,
                        DiagnosticRequestRow.id,
                        DiagnosticRequestRow.execution_id,
                    ).where(
                        DiagnosticRequestRow.state.in_(_IN_FLIGHT),
                        DiagnosticRequestRow.execution_deadline <= func.clock_timestamp(),
                    )
                )
            ).one_or_none()
        if candidate is None:
            return None

        owner, request, execution_id = candidate
        if execution_id != _scoped_execution_id(scope, owner, request):
            return None
        if not await confirm_cleanup():
            return None

        async with self._sessions.begin() as db:
            await _lock(db)
            row = await db.get(DiagnosticRequestRow, (owner, request))
            now = await db.scalar(select(func.clock_timestamp()))
            if (
                row is None
                or row.execution_id != execution_id
                or row.state not in _IN_FLIGHT
                or row.execution_deadline > now
            ):
                return None
            row.state = "cancelled" if row.state == "cancel_pending" else "expired"
            row.finished_at = now
            return _batch_request(row) if row.mode == "batch" else _request(row)

    async def claim_current(self, claim: DiagnosticClaim | BatchDiagnosticClaim) -> bool:
        """Check the current claim before starting the next isolated stage."""
        async with self._sessions.begin() as db:
            await _lock(db)
            if isinstance(claim, BatchDiagnosticClaim):
                row = await self._batch_claim_row(db, claim)
                if row is None or row.state != "running":
                    return False
                source = await _authorized_batch_source(db, row)
                if source != claim.source or not await diagnostic_version_available(
                    db, row.problem_version_id
                ):
                    return False
                if self._batch_offers is not None:
                    offer = self._batch_offer(claim.source, claim.manifest_sha256)
                    if offer is None or offer.purpose != claim.purpose:
                        return False
                now = await db.scalar(select(func.clock_timestamp()))
                return row.execution_deadline > now
            return await self._live_claim(db, claim) is not None

    @staticmethod
    async def _batch_claim_row(
        db: AsyncSession, claim: BatchDiagnosticClaim
    ) -> DiagnosticRequestRow | None:
        row = await db.get(DiagnosticRequestRow, (claim.account_id, claim.request_id))
        if (
            row is None
            or row.execution_id != claim.execution_id
            or row.mode != "batch"
            or row.execution_deadline != claim.execution_deadline
            or row.batch_manifest_digest != claim.manifest_sha256
            or row.batch_purpose != claim.purpose
        ):
            return None
        return row

    async def batch_execution_inputs(
        self,
        claim: BatchDiagnosticClaim,
        artifacts: ArtifactRepository,
        *,
        executor: DiagnosticExecutorSettings | None = None,
    ) -> BatchDiagnosticExecutionInputs:
        """Release exact registered bytes and the confirmed owner's sealed source."""
        async with self._sessions.begin() as db:
            await _lock(db)
            row = await self._batch_claim_row(db, claim)
            if row is None or row.state != "running":
                raise DiagnosticInputsUnavailable("diagnostic claim unavailable")
            verified = await self._verified_batch(db, row)
            if verified is None or verified[0] != claim.source:
                raise DiagnosticInputsUnavailable("diagnostic input unavailable")
            source, batch = verified
            try:
                contents = (
                    b""
                    if batch.receipt()["state"] == "reference_pending"
                    else await artifacts.read(db, source.artifact)
                )
                await require_agent_access(db, row.owner_id)
            except (ArtifactError, AgentUnavailableDuringContest):
                raise DiagnosticInputsUnavailable("diagnostic input unavailable") from None
            if row.execution_deadline <= await db.scalar(select(func.clock_timestamp())):
                raise DiagnosticInputsUnavailable("diagnostic claim expired")
            if contents:
                _record_execution_profile(
                    row,
                    executor,
                    batch.configuration,
                    batch.input_check.configuration if batch.input_check else None,
                )
            return BatchDiagnosticExecutionInputs(batch, contents)

    async def execution_inputs(
        self,
        claim: DiagnosticClaim,
        artifacts: ArtifactRepository,
        problems: ProblemPort,
        admission: v2.NodeAdmission | None,
        *,
        executor: DiagnosticExecutorSettings | None = None,
    ) -> DiagnosticExecutionInputs:
        """Release one sealed source and one public sample to the trusted executor."""
        async with self._sessions.begin() as db:
            await _lock(db)
            current = await self._live_claim(db, claim)
            if current is None:
                raise DiagnosticInputsUnavailable("diagnostic claim unavailable")
            row, inputs = current
            try:
                version = await problems.for_self_test(db, row.problem_version_id)
                configuration = diagnostic_configuration(
                    version,
                    inputs.source.language,
                    admission,
                    package_bytes=row.source_bytes + row.input_bytes + row.expected_bytes,
                    expected_bytes=row.expected_bytes,
                    mode=row.mode,
                )
                source = await artifacts.read(db, inputs.source.artifact)
                await require_agent_access(db, row.owner_id)
            except (ArtifactError, AgentUnavailableDuringContest, ProblemUnavailable):
                raise DiagnosticInputsUnavailable("diagnostic input unavailable") from None
            now = await db.scalar(select(func.clock_timestamp()))
            if row.execution_deadline <= now:
                raise DiagnosticInputsUnavailable("diagnostic claim expired")
            _record_execution_profile(row, executor, configuration)
            return DiagnosticExecutionInputs(inputs, configuration, source)

    @staticmethod
    async def _live_claim(
        db: AsyncSession, claim: DiagnosticClaim
    ) -> tuple[DiagnosticRequestRow, PublicSampleDiagnosticInputs] | None:
        row = await db.get(DiagnosticRequestRow, (claim.inputs.account_id, claim.request_id))
        if (
            row is None
            or row.execution_id != claim.execution_id
            or row.state != "running"
            or row.mode != claim.mode
        ):
            return None
        inputs = await _authorized_inputs(db, row)
        now = await db.scalar(select(func.clock_timestamp()))
        if inputs is None or row.execution_deadline <= now:
            return None
        return row, inputs

    async def acknowledge_stopped(
        self,
        claim: DiagnosticClaim,
        *,
        observation: DiagnosticObservation | UbsanObservation | None = None,
        failure_code: FailureCode | None = None,
    ) -> DiagnosticRequest:
        """A trusted executor calls this only after its isolated attempt is confirmed stopped."""
        if observation is not None and (
            not isinstance(
                observation, UbsanObservation if claim.mode == "ubsan" else DiagnosticObservation
            )
            or failure_code is not None
        ):
            raise ValueError("a diagnostic attempt has one validated outcome")
        self._check_failure(failure_code)
        async with self._sessions.begin() as db:
            await _lock(db)
            row = await db.get(DiagnosticRequestRow, (claim.inputs.account_id, claim.request_id))
            if row is None or row.execution_id != claim.execution_id or row.mode != claim.mode:
                raise DiagnosticRequestConflict("diagnostic execution claim changed")
            if row.state not in _IN_FLIGHT:
                return _request(row)

            authorized = row.state == "running" and await _authorized_inputs(db, row) is not None
            now = await db.scalar(select(func.clock_timestamp()))
            self._finish(row, now, authorized, observation is not None, failure_code)
            if row.state == "observed":
                if isinstance(observation, UbsanObservation):
                    row.ubsan = observation.model_dump(mode="json")
                else:
                    row.o2 = observation.o2.value
                    row.o0 = observation.o0.value if observation.o0 is not None else None
            return _request(row)

    async def acknowledge_batch_stopped(
        self,
        claim: BatchDiagnosticClaim,
        *,
        summary: BatchDiagnosticSummary | None = None,
        failure_code: FailureCode | None = None,
    ) -> BatchDiagnosticRequest:
        """Record only a bound, coarse outcome after the trusted executor confirms cleanup."""
        if summary is not None and (
            not isinstance(summary, BatchDiagnosticSummary)
            or failure_code is not None
            or summary.submission_id != claim.source.submission_id
            or summary.problem_version_id != claim.source.problem_version_id
            or summary.purpose != claim.purpose
        ):
            raise ValueError("diagnostic batch outcome does not match its claim")
        self._check_failure(failure_code)
        async with self._sessions.begin() as db:
            await _lock(db)
            row = await self._batch_claim_row(db, claim)
            if row is None:
                raise DiagnosticRequestConflict("diagnostic execution claim changed")
            if row.state not in _IN_FLIGHT:
                return _batch_request(row)

            verified = await self._verified_batch(db, row) if row.state == "running" else None
            authorized = verified is not None and verified[0] == claim.source
            now = await db.scalar(select(func.clock_timestamp()))
            self._finish(row, now, authorized, summary is not None, failure_code)
            if row.state == "observed":
                row.batch_outcome = summary.outcome
            return _batch_request(row)

    @staticmethod
    def _check_failure(failure: FailureCode | None) -> None:
        if failure is not None and (
            not isinstance(failure, FailureCode) or failure == FailureCode.CLEANUP_FAILED
        ):
            raise ValueError("cleanup is required before releasing a diagnostic attempt")

    @staticmethod
    def _finish(
        row: DiagnosticRequestRow,
        now: datetime,
        authorized: bool,
        observed: bool,
        failure: FailureCode | None,
    ) -> None:
        if row.state == "cancel_pending":
            row.state = "cancelled"
        elif not authorized:
            row.state = "unavailable"
        elif row.execution_deadline <= now:
            row.state = "expired"
        elif failure is not None:
            row.state = "failed"
            row.failure_code = failure.value
        elif observed:
            row.state = "observed"
        else:
            raise ValueError("diagnostic result is required")
        row.finished_at = now
