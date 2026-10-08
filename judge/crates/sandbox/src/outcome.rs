//! Local supervisor observations, not a judge verdict or execution authority.
//! Only trusted collectors may supply these records; schema validation cannot
//! authenticate a cgroup, a counter delta, or an output artifact.

use crate::spec::{ResourceLimits, ValidatedSpec};
use kf_contracts::SchemaVersion;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
pub enum Measurement<T> {
    Measured { value: T },
    Unavailable { reason: MeasurementFailure },
}

impl<T> Measurement<T> {
    fn value(&self) -> Option<&T> {
        match self {
            Self::Measured { value } => Some(value),
            Self::Unavailable { .. } => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MeasurementFailure {
    NotStarted,
    ReadFailed,
    CounterReset,
    InvalidData,
}

/// Checked differences from the fresh task cgroup's enrollment baseline.
/// oom_kill alone includes global OOM and is deliberately not sufficient.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MemoryEvents {
    pub max: u64,
    pub oom: u64,
    pub oom_kill: u64,
    pub oom_group_kill: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResourceUsage {
    /// Entire task cgroup from enrollment, including sandbox initialization
    /// and descendants; never subtract estimated initialization overhead.
    pub cpu_time_us: Measurement<u64>,
    /// Monotonic time from opening the exec gate through confirming an empty
    /// task tree and taking its final accounting snapshot.
    pub wall_time_us: Measurement<u64>,
    pub memory_peak_bytes: Measurement<u64>,
    pub memory_events: Measurement<MemoryEvents>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Termination {
    NotStarted,
    Exited {
        code: u8,
    },
    Signaled {
        signal: u8,
        core_dumped: bool,
    },
    WaitFailed,
    /// Verified whole-cgroup stop, with no separately waitable main status.
    /// PID namespace shutdown may reap its children inside the kernel. This
    /// does not assert a user exit signal or infer a limit from SIGKILL.
    StoppedBySupervisor,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExecutionStage {
    Validation,
    Preparation,
    Cgroup,
    Namespaces,
    RootFilesystem,
    Identity,
    Seccomp,
    Exec,
    Wait,
    Accounting,
    Output,
    ArtifactExport,
    Cleanup,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    CapabilityUnavailable,
    OperationFailed,
    ProtocolFailed,
    MeasurementUnavailable,
    OutputReadFailed,
    ArtifactExportFailed,
    CleanupIncomplete,
}

/// Stable codes only; no host path, input content or unbounded error message.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionError {
    pub stage: ExecutionStage,
    pub code: ErrorCode,
    pub errno: Option<u16>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CleanupState {
    NotNeeded,
    Complete,
    Failed,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputCapture {
    pub artifact_id: Option<String>,
    /// Lower bound when collection stopped; not an invented final total.
    pub observed_bytes: Measurement<u64>,
    pub retained_bytes: u64,
    pub truncated: bool,
    pub reached_eof: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OutputArtifact {
    pub path: String,
    pub artifact_id: String,
    pub bytes: u64,
}

/// Declaration order is the tie-break for equal observation timestamps.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LimitKind {
    Memory,
    CpuTime,
    WallTime,
    Processes,
    Stdout,
    Stderr,
    FileSize,
    WorkdirSize,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum LimitEvidence {
    /// Requires task-local memory.max OOM and group-kill counter increments;
    /// the task must have verified memory.oom.group=1 and its configured cap.
    Memory,
    CpuTime {
        observed_us: u64,
    },
    WallTime {
        observed_us: u64,
    },
    Processes {
        max_events_delta: u64,
    },
    Stdout {
        observed_bytes: u64,
    },
    Stderr {
        observed_bytes: u64,
    },
    FileSize {
        observed_bytes: u64,
    },
    WorkdirSize {
        observed_bytes: u64,
    },
}

impl LimitEvidence {
    pub fn kind(&self) -> LimitKind {
        match self {
            Self::Memory => LimitKind::Memory,
            Self::CpuTime { .. } => LimitKind::CpuTime,
            Self::WallTime { .. } => LimitKind::WallTime,
            Self::Processes { .. } => LimitKind::Processes,
            Self::Stdout { .. } => LimitKind::Stdout,
            Self::Stderr { .. } => LimitKind::Stderr,
            Self::FileSize { .. } => LimitKind::FileSize,
            Self::WorkdirSize { .. } => LimitKind::WorkdirSize,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LimitObservation {
    /// First supervisor observation after exec-gate release, not estimated
    /// kernel event time; post-exit counter evidence uses its read timestamp.
    pub at_wall_time_us: u64,
    pub evidence: LimitEvidence,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionReport {
    pub schema_version: SchemaVersion,
    pub run_id: String,
    pub termination: Termination,
    pub usage: ResourceUsage,
    pub stdout: OutputCapture,
    pub stderr: OutputCapture,
    pub artifacts: Vec<OutputArtifact>,
    pub limits: Vec<LimitObservation>,
    pub cancelled_at_wall_time_us: Option<u64>,
    pub errors: Vec<ExecutionError>,
    pub cleanup: CleanupState,
}

/// Execution classification only. There is intentionally no Accepted variant.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ExecutionOutcome {
    NotStarted,
    InfrastructureFailure,
    Cancelled,
    LimitExceeded { primary: LimitKind },
    Exited { code: u8 },
    Signaled { signal: u8, core_dumped: bool },
}

#[derive(Debug, PartialEq, Eq)]
pub struct ReportError(pub &'static str);

impl std::fmt::Display for ReportError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for ReportError {}

impl ExecutionReport {
    /// Consistency checks on trusted observations. This does not verify OS
    /// isolation or supply an execution permit.
    pub fn classify(&self, spec: &ValidatedSpec) -> Result<ExecutionOutcome, ReportError> {
        let limits = &spec.spec().limits;
        if self.run_id != spec.spec().run_id || self.errors.len() > 16 || self.limits.len() > 8 {
            return Err(ReportError("report_identity_or_size"));
        }
        if self
            .errors
            .iter()
            .any(|error| error.errno.is_some_and(|errno| errno == 0 || errno > 4095))
        {
            return Err(ReportError("errno"));
        }
        if matches!(
            self.termination,
            Termination::Signaled {
                signal: 0 | 65..,
                ..
            }
        ) {
            return Err(ReportError("signal"));
        }
        if (self.cleanup == CleanupState::Failed)
            != self
                .errors
                .iter()
                .any(|error| error.stage == ExecutionStage::Cleanup)
        {
            return Err(ReportError("cleanup"));
        }
        check_capture(&self.stdout, limits.stdout_bytes)?;
        check_capture(&self.stderr, limits.stderr_bytes)?;
        let mut artifact_paths = std::collections::BTreeSet::new();
        let mut artifact_bytes = 0_u64;
        for artifact in &self.artifacts {
            artifact_bytes = artifact_bytes
                .checked_add(artifact.bytes)
                .ok_or(ReportError("artifacts"))?;
            if !spec.spec().outputs.contains(&artifact.path)
                || !artifact_paths.insert(artifact.path.as_str())
                || !artifact_id(&artifact.artifact_id)
                || artifact.bytes > limits.file_bytes
                || artifact_bytes > limits.workdir_bytes
            {
                return Err(ReportError("artifacts"));
            }
        }
        let mut observed_kinds = std::collections::BTreeSet::new();
        for observation in &self.limits {
            if !observed_kinds.insert(observation.evidence.kind())
                || !self.within_wall_time(observation.at_wall_time_us)
                || matches!(observation.evidence, LimitEvidence::WallTime { observed_us }
                    if observed_us > observation.at_wall_time_us)
                || !self.confirmed(&observation.evidence, limits)
            {
                return Err(ReportError("limit_evidence"));
            }
        }
        if self
            .cancelled_at_wall_time_us
            .is_some_and(|time| !self.within_wall_time(time))
        {
            return Err(ReportError("cancellation_time"));
        }
        if self.termination == Termination::NotStarted {
            if !self.limits.is_empty()
                || self.cancelled_at_wall_time_us.is_some()
                || !self.artifacts.is_empty()
                || self.stdout.retained_bytes != 0
                || self.stderr.retained_bytes != 0
                || self
                    .stdout
                    .observed_bytes
                    .value()
                    .is_some_and(|value| *value != 0)
                || self
                    .stderr
                    .observed_bytes
                    .value()
                    .is_some_and(|value| *value != 0)
            {
                return Err(ReportError("not_started"));
            }
            return if !self.errors.iter().any(|error| {
                matches!(
                    error.stage,
                    ExecutionStage::Validation
                        | ExecutionStage::Preparation
                        | ExecutionStage::Cgroup
                        | ExecutionStage::Namespaces
                        | ExecutionStage::RootFilesystem
                        | ExecutionStage::Identity
                        | ExecutionStage::Seccomp
                        | ExecutionStage::Exec
                )
            }) {
                Err(ReportError("missing_start_error"))
            } else if self.cleanup == CleanupState::Failed {
                Ok(ExecutionOutcome::InfrastructureFailure)
            } else {
                Ok(ExecutionOutcome::NotStarted)
            };
        }
        if self.cleanup == CleanupState::NotNeeded {
            return Err(ReportError("cleanup"));
        }
        // Collector/cleanup failure takes precedence, while the report retains
        // all observed limits, termination and outputs for diagnosis.
        if !self.errors.is_empty()
            || self.termination == Termination::WaitFailed
            || self.usage.cpu_time_us.value().is_none()
            || self.usage.wall_time_us.value().is_none()
            || self.usage.memory_peak_bytes.value().is_none()
            || self.usage.memory_events.value().is_none()
            || self.stdout.observed_bytes.value().is_none()
            || self.stderr.observed_bytes.value().is_none()
            || !self.stdout.reached_eof && !self.stdout.truncated
            || !self.stderr.reached_eof && !self.stderr.truncated
            || (self.stdout.truncated || self.stderr.truncated)
                && self.limits.is_empty()
                && self.cancelled_at_wall_time_us.is_none()
            || self
                .stdout
                .observed_bytes
                .value()
                .is_some_and(|bytes| *bytes > limits.stdout_bytes)
                && !observed_kinds.contains(&LimitKind::Stdout)
            || self
                .stderr
                .observed_bytes
                .value()
                .is_some_and(|bytes| *bytes > limits.stderr_bytes)
                && !observed_kinds.contains(&LimitKind::Stderr)
        {
            return Ok(ExecutionOutcome::InfrastructureFailure);
        }
        let primary = self
            .limits
            .iter()
            .min_by_key(|observation| (observation.at_wall_time_us, observation.evidence.kind()));
        // Cancellation wins an exact-time tie with a limit. No later cleanup
        // kill signal may replace the reason first observed by supervision.
        if self
            .cancelled_at_wall_time_us
            .is_some_and(|cancelled| primary.is_none_or(|limit| cancelled <= limit.at_wall_time_us))
        {
            return Ok(ExecutionOutcome::Cancelled);
        }
        if let Some(primary) = primary {
            return Ok(ExecutionOutcome::LimitExceeded {
                primary: primary.evidence.kind(),
            });
        }
        Ok(match self.termination {
            Termination::Exited { code } => ExecutionOutcome::Exited { code },
            Termination::Signaled {
                signal,
                core_dumped,
            } => ExecutionOutcome::Signaled {
                signal,
                core_dumped,
            },
            Termination::StoppedBySupervisor => ExecutionOutcome::InfrastructureFailure,
            Termination::NotStarted | Termination::WaitFailed => unreachable!(),
        })
    }

    fn within_wall_time(&self, time: u64) -> bool {
        self.usage
            .wall_time_us
            .value()
            .is_some_and(|wall| time <= *wall)
    }

    fn confirmed(&self, evidence: &LimitEvidence, limits: &ResourceLimits) -> bool {
        match *evidence {
            LimitEvidence::Memory => self.usage.memory_events.value().is_some_and(|events| {
                events.max > 0 && events.oom > 0 && events.oom_group_kill > 0 && events.oom_kill > 0
            }),
            LimitEvidence::CpuTime { observed_us } => {
                observed_us >= limits.cpu_time_us
                    && self
                        .usage
                        .cpu_time_us
                        .value()
                        .is_some_and(|total| observed_us <= *total)
            }
            LimitEvidence::WallTime { observed_us } => {
                observed_us >= limits.wall_time_us
                    && self
                        .usage
                        .wall_time_us
                        .value()
                        .is_some_and(|total| observed_us <= *total)
            }
            LimitEvidence::Processes { max_events_delta } => max_events_delta > 0,
            LimitEvidence::Stdout { observed_bytes } => {
                observed_bytes > limits.stdout_bytes
                    && self
                        .stdout
                        .observed_bytes
                        .value()
                        .is_some_and(|total| observed_bytes <= *total)
            }
            LimitEvidence::Stderr { observed_bytes } => {
                observed_bytes > limits.stderr_bytes
                    && self
                        .stderr
                        .observed_bytes
                        .value()
                        .is_some_and(|total| observed_bytes <= *total)
            }
            LimitEvidence::FileSize { observed_bytes } => observed_bytes > limits.file_bytes,
            LimitEvidence::WorkdirSize { observed_bytes } => observed_bytes > limits.workdir_bytes,
        }
    }
}

fn artifact_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn check_capture(capture: &OutputCapture, limit: u64) -> Result<(), ReportError> {
    if capture.retained_bytes > limit
        || capture
            .artifact_id
            .as_deref()
            .is_some_and(|id| !artifact_id(id))
        || capture.retained_bytes > 0 && capture.artifact_id.is_none()
        || capture.observed_bytes.value().is_some_and(|observed| {
            capture.retained_bytes > *observed
                || (capture.retained_bytes < *observed && !capture.truncated)
                || (*observed > limit && !capture.truncated)
        })
    {
        return Err(ReportError("output_capture"));
    }
    Ok(())
}
