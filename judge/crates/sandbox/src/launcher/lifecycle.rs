//! Monotone lifecycle ordering for the future trusted supervisor.
//!
//! Callers must produce events from verified operations on the same task. These
//! in-memory types cannot authenticate OS observations and are deliberately not
//! deserializable. In particular, reaching FilterReady is no execution permit.

use crate::outcome::{ErrorCode, ExecutionError, ExecutionStage, LimitKind};
use crate::spec::ValidatedSpec;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Validated,
    Preparing,
    CgroupReady,
    NamespacesReady,
    RootReady,
    IdentityReady,
    FilterReady,
    ExecReleased,
    Waiting,
    Terminating,
    Accounting,
    Cleaning,
    Finished,
}

impl State {
    /// Operation currently outstanding; cleanup failures have their own record.
    pub fn stage(self) -> Option<ExecutionStage> {
        match self {
            Self::Validated => Some(ExecutionStage::Preparation),
            Self::Preparing => Some(ExecutionStage::Cgroup),
            Self::CgroupReady => Some(ExecutionStage::Namespaces),
            Self::NamespacesReady => Some(ExecutionStage::RootFilesystem),
            Self::RootReady => Some(ExecutionStage::Identity),
            Self::IdentityReady => Some(ExecutionStage::Seccomp),
            Self::FilterReady | Self::ExecReleased => Some(ExecutionStage::Exec),
            Self::Waiting | Self::Terminating => Some(ExecutionStage::Wait),
            Self::Accounting => Some(ExecutionStage::Accounting),
            Self::Cleaning => Some(ExecutionStage::Cleanup),
            Self::Finished => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Event {
    BeginPreparation,
    /// Child remains blocked; task enrollment and limits are verified.
    CgroupConfirmed,
    NamespacesConfirmed,
    RootConfirmed,
    /// UID/GID, supplementary groups, capabilities and no_new_privs checked.
    IdentityConfirmed,
    FilterConfirmed,
    /// Trusted control transport has released the exec gate, not exec success.
    ExecGateReleased,
    /// The exec error channel confirmed success, distinct from gate release.
    ExecConfirmed,
    /// Does not imply that descendants have exited or output reached EOF.
    MainExited,
    Cancel,
    LimitObserved(LimitKind),
    Failed {
        code: ErrorCode,
        errno: Option<u16>,
    },
    /// Entire task tree empty and launcher/init children reaped.
    TreeReaped,
    /// Final accounting and bounded output/artifact collection settled,
    /// including explicit unavailable/error records when collection failed.
    CollectionFinished,
    CleanupSucceeded,
    CleanupFailed {
        errno: Option<u16>,
    },
}

/// First reason to stop scheduling execution, not a judge/result classifier.
/// Later cancellation and faults remain independently observable.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StopReason {
    MainExited,
    Cancelled,
    Limit(LimitKind),
    Failure,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct TransitionError {
    pub state: State,
    pub event: Event,
}

impl std::fmt::Display for TransitionError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "invalid lifecycle event {:?} in {:?}",
            self.event, self.state
        )
    }
}

impl std::error::Error for TransitionError {}

/// Private fields and no Clone/Deserialize keep each attempt's transitions monotone.
/// Retains at most one primary fault and one independent cleanup fault.
#[derive(Debug)]
pub struct Lifecycle {
    run_id: String,
    state: State,
    stop_reason: Option<StopReason>,
    exec_released: bool,
    cancel_requested: bool,
    failure: Option<ExecutionError>,
    cleanup_failure: Option<ExecutionError>,
}

impl Lifecycle {
    pub fn new(spec: &ValidatedSpec) -> Self {
        Self {
            run_id: spec.spec().run_id.clone(),
            state: State::Validated,
            stop_reason: None,
            exec_released: false,
            cancel_requested: false,
            failure: None,
            cleanup_failure: None,
        }
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }
    pub fn state(&self) -> State {
        self.state
    }
    pub fn stop_reason(&self) -> Option<StopReason> {
        self.stop_reason
    }
    pub fn exec_released(&self) -> bool {
        self.exec_released
    }
    pub fn cancel_requested(&self) -> bool {
        self.cancel_requested
    }
    pub fn failure(&self) -> Option<&ExecutionError> {
        self.failure.as_ref()
    }
    pub fn cleanup_failure(&self) -> Option<&ExecutionError> {
        self.cleanup_failure.as_ref()
    }

    /// An invalid live event latches a protocol fault and requests termination;
    /// ignoring the returned error cannot subsequently advance to exec.
    /// This only records ordering: the caller must still perform OS termination.
    pub fn apply(&mut self, event: Event) -> Result<(), TransitionError> {
        let previous = self.state;
        if previous == State::Finished {
            return Err(TransitionError {
                state: previous,
                event,
            });
        }
        match event {
            Event::Cancel => {
                self.cancel_requested = true;
                self.request_stop(StopReason::Cancelled);
                return Ok(());
            }
            Event::LimitObserved(kind) => {
                self.request_stop(StopReason::Limit(kind));
                return Ok(());
            }
            Event::Failed { code, errno } if valid_errno(errno) => {
                self.record_failure(code, errno);
                return Ok(());
            }
            Event::CleanupFailed { errno } if previous == State::Cleaning && valid_errno(errno) => {
                self.record_failure(ErrorCode::CleanupIncomplete, errno);
                return Ok(());
            }
            _ => {}
        }
        let next = match (previous, event) {
            (State::Validated, Event::BeginPreparation) => Some(State::Preparing),
            (State::Preparing, Event::CgroupConfirmed) => Some(State::CgroupReady),
            (State::CgroupReady, Event::NamespacesConfirmed) => Some(State::NamespacesReady),
            (State::NamespacesReady, Event::RootConfirmed) => Some(State::RootReady),
            (State::RootReady, Event::IdentityConfirmed) => Some(State::IdentityReady),
            (State::IdentityReady, Event::FilterConfirmed) => Some(State::FilterReady),
            (State::FilterReady, Event::ExecGateReleased) => Some(State::ExecReleased),
            (State::ExecReleased, Event::ExecConfirmed) => Some(State::Waiting),
            (State::Waiting, Event::MainExited) => {
                self.request_stop(StopReason::MainExited);
                return Ok(());
            }
            (State::Terminating, Event::TreeReaped) => Some(State::Accounting),
            (State::Accounting, Event::CollectionFinished) => Some(State::Cleaning),
            (State::Cleaning, Event::CleanupSucceeded) => Some(State::Finished),
            _ => None,
        };
        if let Some(next) = next {
            self.state = next;
            if next == State::ExecReleased {
                self.exec_released = true;
            }
            Ok(())
        } else {
            self.record_failure(ErrorCode::ProtocolFailed, None);
            Err(TransitionError {
                state: previous,
                event,
            })
        }
    }

    fn request_stop(&mut self, reason: StopReason) {
        self.stop_reason.get_or_insert(reason);
        match self.state {
            State::Terminating | State::Accounting | State::Cleaning | State::Finished => {}
            _ => self.state = State::Terminating,
        }
    }

    fn record_failure(&mut self, code: ErrorCode, errno: Option<u16>) {
        if let Some(stage) = self.state.stage() {
            let error = ExecutionError { stage, code, errno };
            if self.state == State::Cleaning {
                self.cleanup_failure.get_or_insert(error);
            } else {
                self.failure.get_or_insert(error);
            }
            // Accounting failures do not skip remaining collection. Once the
            // tree is reaped, later faults must never restore a live-task phase.
            self.request_stop(StopReason::Failure);
        }
    }
}

fn valid_errno(errno: Option<u16>) -> bool {
    errno.is_none_or(|value| (1..=4095).contains(&value))
}
