//! Pure transition and lease fencing; repositories must apply these under lock.
use crate::judging::{LeaseBinding, Validation};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EvaluationState {
    Queued,
    Claimed,
    Compiling,
    Running,
    Cancelling,
    Finished,
    Cancelled,
    Failed,
}
impl EvaluationState {
    pub fn allows(self, target: Self) -> bool {
        use EvaluationState::*;
        matches!(
            (self, target),
            (Queued, Claimed | Cancelled)
                | (Claimed, Compiling | Cancelling | Failed | Queued)
                | (Compiling, Running | Finished | Cancelling | Failed | Queued)
                | (Running, Finished | Cancelling | Failed | Queued)
                | (Cancelling, Cancelled | Failed)
        )
    }
}
pub fn require_current_lease(
    expected: &LeaseBinding,
    supplied: &LeaseBinding,
    expires_at_unix_ms: u64,
    now_unix_ms: u64,
) -> Validation {
    expected.validate()?;
    supplied.validate()?;
    if expected != supplied || now_unix_ms >= expires_at_unix_ms {
        return Err("stale or foreign execution authority");
    }
    Ok(())
}
pub fn require_transition(current: EvaluationState, target: EvaluationState) -> Validation {
    if current.allows(target) {
        Ok(())
    } else {
        Err("invalid evaluation transition")
    }
}
