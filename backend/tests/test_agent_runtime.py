from itertools import pairwise
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kf_oj.contracts.agent_runtime import AgentRun, AgentSession, AgentStep, RunState
from kf_oj.runtime.state import check_run_transition


def test_agent_records_use_separate_runtime_identity_and_canonical_references():
    session = AgentSession(
        session_id=uuid4(),
        owner_id=uuid4(),
        purpose="focused_training",
        created_at="2026-11-20T00:00:00Z",
    )
    run = AgentRun(
        run_id=uuid4(),
        session_id=session.session_id,
        request_id=uuid4(),
        skill_version_id=uuid4(),
        input_digest="a" * 64,
        configuration_digest="b" * 64,
        created_at=session.created_at,
    )
    step = AgentStep(
        step_id=uuid4(),
        run_id=run.run_id,
        sequence=1,
        kind="tool",
        invocation_version="learning-overview-v1",
        arguments_digest="c" * 64,
        state="succeeded",
        result_digest="d" * 64,
    )
    assert run.state is RunState.QUEUED
    assert AgentStep.model_validate_json(step.model_dump_json()) == step
    with pytest.raises(ValidationError):
        AgentRun.model_validate(run.model_dump() | {"state": "accepted"})
    with pytest.raises(ValidationError):
        AgentStep.model_validate(step.model_dump() | {"state": "failed", "error_code": "timeout"})


def test_run_can_retry_but_cannot_succeed_after_cancellation_is_requested():
    states = (
        RunState.QUEUED,
        RunState.RUNNING,
        RunState.RETRY_WAIT,
        RunState.QUEUED,
        RunState.RUNNING,
        RunState.CANCEL_REQUESTED,
        RunState.CANCELLED,
    )
    for previous, following in pairwise(states):
        check_run_transition(previous, following)
    with pytest.raises(ValueError):
        check_run_transition(RunState.CANCEL_REQUESTED, RunState.SUCCEEDED)
    with pytest.raises(ValueError):
        check_run_transition(RunState.CANCELLED, RunState.QUEUED)
