"""Pure transition/fencing rules; callers still need a locked database transaction."""

from enum import StrEnum

from kf_oj.contracts.judging import LeaseBinding


class EvaluationState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    COMPILING = "compiling"
    RUNNING = "running"
    CANCELLING = "cancelling"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    FAILED = "failed"

    def allows(self, target: "EvaluationState") -> bool:
        transitions = {
            self.QUEUED: {self.CLAIMED, self.CANCELLED},
            self.CLAIMED: {self.COMPILING, self.CANCELLING, self.FAILED, self.QUEUED},
            self.COMPILING: {
                self.RUNNING,
                self.FINISHED,
                self.CANCELLING,
                self.FAILED,
                self.QUEUED,
            },
            self.RUNNING: {self.FINISHED, self.CANCELLING, self.FAILED, self.QUEUED},
            self.CANCELLING: {self.CANCELLED, self.FAILED, self.QUEUED},
        }
        return target in transitions.get(self, set())


def require_current_lease(
    expected: LeaseBinding,
    supplied: LeaseBinding,
    expires_at_unix_ms: int,
    now_unix_ms: int,
) -> None:
    """Identity/version equality and a strict deadline; never trust client time."""
    if expected != supplied or now_unix_ms < 0 or now_unix_ms >= expires_at_unix_ms:
        raise ValueError("stale or foreign execution authority")


def require_transition(current: EvaluationState, target: EvaluationState) -> None:
    if not current.allows(target):
        raise ValueError("invalid evaluation transition")
