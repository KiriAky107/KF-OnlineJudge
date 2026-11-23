"""Run lifecycle rules shared by scheduling and future atomic state writes."""

from kf_oj.contracts.agent_runtime import RunState

_TRANSITIONS = {
    RunState.QUEUED: frozenset((RunState.RUNNING, RunState.CANCELLED)),
    RunState.RUNNING: frozenset(
        (RunState.RETRY_WAIT, RunState.CANCEL_REQUESTED, RunState.SUCCEEDED, RunState.FAILED)
    ),
    RunState.RETRY_WAIT: frozenset((RunState.QUEUED, RunState.CANCELLED, RunState.FAILED)),
    RunState.CANCEL_REQUESTED: frozenset((RunState.CANCELLED,)),
}


def check_run_transition(previous: RunState, following: RunState) -> None:
    """Reject transitions outside the lifecycle, including any change after a terminal state."""
    if following not in _TRANSITIONS.get(previous, frozenset()):
        raise ValueError(f"invalid learning run transition: {previous.value} -> {following.value}")
