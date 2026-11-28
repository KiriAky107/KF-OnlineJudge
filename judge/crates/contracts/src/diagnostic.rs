//! Private public-sample diagnostic wire contract. Execution authorization lives at the broker.
use crate::{SchemaVersion, judging as v1, judging_v2 as v2};
use serde::{Deserialize, Serialize, de::Error};
use v1::{Validation, Verdict, validated_struct};

pub const CLAIM_SECONDS: u64 = 60;
pub const MAX_DIAGNOSTIC_WALL_US: u64 = 55_000_000;
pub const MAX_PREPARATION_WALL_US: u64 = 20_000_000;
pub const MAX_SAMPLE_WALL_US: u64 = 5_000_000;
pub const MAX_SAMPLE_BYTES: u64 = 64 * 1024;
pub const MAX_CONTROL_BYTES: usize = 16 * 1024;
pub const MAX_DIAGNOSTIC_BATCH_CASES: usize = 4;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum RunKind {
    #[serde(rename = "diagnostic.run")]
    Value,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ResultKind {
    #[serde(rename = "diagnostic.result")]
    Value,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiagnosticBinding {
    pub request_id: v1::Identifier,
    pub execution_id: v1::Identifier,
    pub worker_id: v1::Identifier,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DiagnosticMode {
    #[default]
    Optimization,
    Ubsan,
}

impl DiagnosticMode {
    pub fn passes(self) -> u64 {
        match self {
            Self::Optimization => 2,
            Self::Ubsan => 1,
        }
    }

    fn is_optimization(&self) -> bool {
        *self == Self::Optimization
    }
}

validated_struct!(DiagnosticRun {
    schema_version: SchemaVersion,
    kind: RunKind,
    binding: DiagnosticBinding,
    problem_version_id: v1::Identifier,
    configuration: v2::LanguageConfiguration,
    claim_expires_at_unix_ms: u64,
    source: v1::ArtifactReference,
    sample: v1::TestCase,
    #[serde(default, skip_serializing_if = "DiagnosticMode::is_optimization")]
    mode: DiagnosticMode,
});

impl DiagnosticRun {
    pub fn validate(&self) -> Validation {
        let config = &self.configuration;
        config.validate()?;
        self.source.validate()?;
        self.sample.validate()?;
        v1::bounded(self.claim_expires_at_unix_ms, 1, i64::MAX as u64)?;
        let compile_us = config.compile_limits.wall_time_us;
        let run_us = config.run_limits.wall_time_us;
        if !matches!(config.language, v2::Language::C17 | v2::Language::Cpp20)
            || compile_us > MAX_PREPARATION_WALL_US
            || run_us > MAX_SAMPLE_WALL_US
            || config.job_wall_time_us < self.mode.passes() * (compile_us + run_us)
            || config.job_wall_time_us > MAX_DIAGNOSTIC_WALL_US
            || !(1..=v1::MAX_SOURCE_BYTES).contains(&self.source.bytes)
            || self.sample.input.bytes.max(self.sample.expected.bytes) > MAX_SAMPLE_BYTES
            || self.sample.expected.bytes > config.run_limits.stdout_bytes
            || self.sample.case_id != self.binding.execution_id
        {
            return Err("diagnostic input or budget is outside policy");
        }
        Ok(())
    }
}

validated_struct!(DiagnosticObservation {
    o2: Verdict,
    #[serde(deserialize_with = "v1::required_option")]
    o0: Option<Verdict>,
});

impl DiagnosticObservation {
    pub fn validate(&self) -> Validation {
        let unavailable = |value| matches!(value, Verdict::Cancelled | Verdict::SystemError);
        if unavailable(self.o2)
            || self.o0.is_some_and(unavailable)
            || matches!(self.o2, Verdict::WrongAnswer | Verdict::RuntimeError) != self.o0.is_some()
        {
            return Err("invalid diagnostic observation");
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UbsanCategory {
    SignedIntegerOverflow,
    DivisionByZero,
    IndexOutOfBounds,
    InvalidShift,
}

validated_struct!(UbsanFinding {
    category: UbsanCategory,
    line: u64,
    column: u64,
});

impl UbsanFinding {
    pub fn validate(&self) -> Validation {
        v1::bounded(self.line, 1, v1::MAX_SOURCE_BYTES)?;
        v1::bounded(self.column, 1, v1::MAX_SOURCE_BYTES)
    }
}

validated_struct!(UbsanObservation {
    verdict: Verdict,
    #[serde(deserialize_with = "v1::required_option")]
    finding: Option<UbsanFinding>,
});

impl UbsanObservation {
    pub fn validate(&self) -> Validation {
        if matches!(self.verdict, Verdict::Cancelled | Verdict::SystemError) {
            return Err("invalid UBSan verdict");
        }
        if let Some(finding) = &self.finding {
            finding.validate()?;
            if self.verdict != Verdict::RuntimeError {
                return Err("invalid UBSan finding");
            }
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResultState {
    Observed,
    Failed,
    Cancelled,
    CleanupUnconfirmed,
}

validated_struct!(DiagnosticResult {
    schema_version: SchemaVersion,
    kind: ResultKind,
    binding: DiagnosticBinding,
    state: ResultState,
    #[serde(deserialize_with = "v1::required_option")]
    observation: Option<DiagnosticObservation>,
    #[serde(deserialize_with = "v1::required_option")]
    failure_code: Option<v1::FailureCode>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    ubsan: Option<UbsanObservation>,
});

impl DiagnosticResult {
    pub fn validate(&self) -> Validation {
        if let Some(value) = &self.observation {
            value.validate()?;
        }
        if let Some(value) = &self.ubsan {
            value.validate()?;
        }
        if (self.state == ResultState::Observed)
            != (self.observation.is_some() || self.ubsan.is_some())
            || (self.observation.is_some() && self.ubsan.is_some())
            || (self.state == ResultState::Failed) != self.failure_code.is_some()
            || self.failure_code == Some(v1::FailureCode::CleanupFailed)
        {
            return Err("invalid diagnostic result");
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BoundaryOutcome {
    Completed,
    RuntimeError,
    TimeLimit,
    MemoryLimit,
    OutputLimit,
    OtherLimit,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum BoundaryReportKind {
    #[serde(rename = "diagnostic.boundary-measurements")]
    Value,
}

validated_struct!(BoundaryMeasurement {
    case_id: v1::Identifier,
    outcome: BoundaryOutcome,
    cpu_time_us: u64,
    memory_peak_bytes: u64,
});

impl BoundaryMeasurement {
    pub fn validate(&self) -> Validation {
        v1::bounded(self.cpu_time_us, 0, i64::MAX as u64)?;
        v1::bounded(self.memory_peak_bytes, 0, i64::MAX as u64)
    }
}

validated_struct!(DiagnosticBoundaryReport {
    schema_version: SchemaVersion,
    kind: BoundaryReportKind,
    binding: v1::LeaseBinding,
    manifest_digest: v1::Digest,
    cases: Vec<BoundaryMeasurement>,
});

impl DiagnosticBoundaryReport {
    pub fn validate(&self) -> Validation {
        self.binding.validate()?;
        if !(2..=MAX_DIAGNOSTIC_BATCH_CASES).contains(&self.cases.len()) {
            return Err("boundary measurement count outside policy");
        }
        let mut seen = std::collections::HashSet::new();
        for case in &self.cases {
            case.validate()?;
            if !seen.insert(case.case_id) {
                return Err("boundary measurements must bind distinct cases");
            }
        }
        Ok(())
    }
}
