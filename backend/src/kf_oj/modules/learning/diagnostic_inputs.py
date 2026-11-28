from dataclasses import dataclass
from uuid import UUID

from kf_oj.modules.contests.public import require_agent_access
from kf_oj.modules.identity.public import ActiveSessionPort, Principal
from kf_oj.modules.problems.public import (
    ProblemUnavailable,
    PublicDiagnosticSample,
    public_diagnostic_sample,
)
from kf_oj.modules.submissions.public import (
    DiagnosticSourceReference,
    SubmissionUnavailable,
    owned_diagnostic_source,
)


class DiagnosticInputsUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublicSampleDiagnosticInputs:
    account_id: UUID
    source: DiagnosticSourceReference
    sample: PublicDiagnosticSample


async def resolve_public_sample_diagnostic_inputs(
    session,
    identity: ActiveSessionPort,
    principal: Principal,
    *,
    submission_id: UUID,
    problem_version_id: UUID,
    sample_ordinal: int,
    source_execution_confirmed: bool,
) -> PublicSampleDiagnosticInputs:
    """Bind one explicitly confirmed source and sample in the caller's transaction."""
    source = await resolve_confirmed_diagnostic_source(
        session,
        identity,
        principal,
        submission_id=submission_id,
        problem_version_id=problem_version_id,
        source_execution_confirmed=source_execution_confirmed,
    )
    try:
        sample = await public_diagnostic_sample(session, problem_version_id, sample_ordinal)
    except ProblemUnavailable:
        raise DiagnosticInputsUnavailable("diagnostic input unavailable") from None
    return PublicSampleDiagnosticInputs(principal.account_id, source, sample)


async def resolve_confirmed_diagnostic_source(
    session,
    identity: ActiveSessionPort,
    principal: Principal,
    *,
    submission_id: UUID,
    problem_version_id: UUID,
    source_execution_confirmed: bool,
) -> DiagnosticSourceReference:
    """Check current authorization before selecting any source artifact."""
    if source_execution_confirmed is not True:
        raise DiagnosticInputsUnavailable("source execution confirmation required")
    return await resolve_owned_diagnostic_source(
        session, identity, principal, submission_id, problem_version_id
    )


async def resolve_owned_diagnostic_source(
    session,
    identity: ActiveSessionPort,
    principal: Principal,
    submission_id: UUID,
    problem_version_id: UUID,
) -> DiagnosticSourceReference:
    """Read the owner's sealed metadata without releasing source contents."""
    await identity.require_active_session(session, principal)
    await require_agent_access(session, principal.account_id)
    try:
        return await owned_diagnostic_source(
            session, principal.account_id, submission_id, problem_version_id
        )
    except SubmissionUnavailable:
        raise DiagnosticInputsUnavailable("diagnostic input unavailable") from None
