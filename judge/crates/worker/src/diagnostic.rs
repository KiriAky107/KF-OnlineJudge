//! Dedicated public-sample execution, without formal judgement reporting.
//!
//! The trusted caller must resolve the published sample and authorize the user's
//! source before constructing this input. This module is not a wire/API boundary
//! and must never be fed a user-selected test artifact or a formal test manifest.
//! Publication/ownership checks and queue admission belong to that caller; every
//! actual execution still requires the privileged factory's independent policy.
use crate::{
    pipeline::{ArtifactPort, Attempt, StageEvidence, kind, limits, metric, verify},
    pipeline_v2::{artifact_error, failure, snapshot_reference, with_publication},
    runtime_specs::diagnostic_preparation_spec,
};
use kf_contracts::{
    diagnostic::{DiagnosticMode, UbsanCategory, UbsanFinding, UbsanObservation},
    judging as v1, judging_v2 as v2,
};
use kf_engine::{
    Observation,
    diagnostic::{
        OptimizationError, OptimizationLevel, OptimizationObservation, OptimizationPreparationPort,
        OptimizationRequest, observe_optimization, observe_ubsan, validate_public_sample,
    },
    submission::{ControlPort, StageFailure},
    submission_v2::{Compiled, RunPort},
};
use kf_sandbox::{
    artifacts::{SnapshotFile, StagePublication},
    outcome::ExecutionOutcome,
    spec::{ExecutionProfile, ExecutionSpec, InputArtifact},
};
use std::{
    io::{self, Read},
    path::PathBuf,
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};

/// Byte-verified, immutable inputs from a trusted public-sample resolver.
/// Deliberately not deserializable: hashes prove identity, not public visibility.
pub struct PublicSampleInput {
    configuration: v2::LanguageConfiguration,
    binding: v1::LeaseBinding,
    source: v1::ArtifactReference,
    sample: v1::TestCase,
    source_bytes: Vec<u8>,
    input_bytes: Vec<u8>,
    expected: Vec<u8>,
}

impl PublicSampleInput {
    pub fn from_resolved(
        request: &OptimizationRequest<'_>,
        source: Vec<u8>,
        input: Vec<u8>,
    ) -> Result<Self, OptimizationError> {
        Self::from_resolved_mode(request, source, input, DiagnosticMode::Optimization)
    }

    pub fn from_resolved_mode(
        request: &OptimizationRequest<'_>,
        source: Vec<u8>,
        input: Vec<u8>,
        mode: DiagnosticMode,
    ) -> Result<Self, OptimizationError> {
        validate_public_sample(request, mode).map_err(OptimizationError::InvalidRequest)?;
        for (reference, bytes) in [
            (request.source, source.as_slice()),
            (&request.public_sample.input, input.as_slice()),
            (&request.public_sample.expected, request.expected),
        ] {
            verify(reference, bytes).map_err(|_| {
                OptimizationError::InvalidRequest("diagnostic artifact differs from resolved input")
            })?;
        }
        Ok(Self {
            configuration: request.configuration.clone(),
            binding: request.binding.clone(),
            source: request.source.clone(),
            sample: request.public_sample.clone(),
            source_bytes: source,
            input_bytes: input,
            expected: request.expected.to_vec(),
        })
    }

    fn request(&self) -> OptimizationRequest<'_> {
        OptimizationRequest {
            configuration: &self.configuration,
            binding: &self.binding,
            source: &self.source,
            public_sample: &self.sample,
            expected: &self.expected,
        }
    }

    fn matches(&self, request: &OptimizationRequest<'_>) -> bool {
        request.configuration == &self.configuration
            && request.binding == &self.binding
            && request.source == &self.source
            && request.public_sample == &self.sample
            && request.expected == self.expected
    }

    fn validate_program(&self, program: &v2::CompiledProgram) -> v1::Validation {
        program.validate()?;
        if program.kind != v2::ProgramKind::NativeExecutable
            || program.binding != self.binding
            || program.source != self.source
            || program.toolchain != self.configuration.toolchain
            || program.bytes() > self.configuration.compile_limits.workdir_bytes
            || program
                .files
                .iter()
                .any(|f| f.artifact.bytes > self.configuration.compile_limits.file_bytes)
        {
            return Err("program differs from diagnostic source, runtime or budget");
        }
        Ok(())
    }

    fn run_spec(
        &self,
        program: &v2::CompiledProgram,
        run_id: &str,
    ) -> Result<ExecutionSpec, StageFailure> {
        self.validate_program(program)
            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, true))?;
        let spec = ExecutionSpec {
            schema_version: kf_contracts::SchemaVersion,
            runtime_identity: Some(self.configuration.toolchain.clone()),
            run_id: run_id.into(),
            profile: ExecutionProfile::Native,
            argv: vec!["/work/main".into()],
            env: Default::default(),
            cwd: ".".into(),
            inputs: vec![InputArtifact {
                artifact_id: "program".into(),
                path: "main".into(),
            }],
            stdin_artifact: Some("stdin".into()),
            outputs: vec![],
            limits: limits(&self.configuration.run_limits),
        };
        spec.clone()
            .validate_for_factory_client()
            .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, true))?;
        Ok(spec)
    }
}

/// Local execution evidence remains available even when a later stage fails.
/// Contains no source, expected output, compiler text or runtime output.
pub struct DiagnosticRun<T = OptimizationObservation> {
    pub result: Result<T, OptimizationError>,
    pub stages: Vec<StageEvidence>,
}

/// Executes one resolved sample within the caller's remaining claim deadline.
/// Does not recover shared staging, read arbitrary artifacts, or write an
/// evaluation. The caller owns exclusive queue/slot admission and crash recovery.
pub fn observe_public_sample(
    input: PublicSampleInput,
    socket: PathBuf,
    staging: PathBuf,
    claim_deadline: Instant,
    cancellation: &AtomicBool,
) -> io::Result<DiagnosticRun> {
    with_sample_attempt(
        input,
        socket,
        staging,
        claim_deadline,
        cancellation,
        DiagnosticMode::Optimization,
        |port, request| observe_optimization(port, request),
    )
}

pub fn observe_ubsan_sample(
    input: PublicSampleInput,
    socket: PathBuf,
    staging: PathBuf,
    claim_deadline: Instant,
    cancellation: &AtomicBool,
) -> io::Result<DiagnosticRun<UbsanObservation>> {
    with_sample_attempt(
        input,
        socket,
        staging,
        claim_deadline,
        cancellation,
        DiagnosticMode::Ubsan,
        |port, request| {
            let verdict = observe_ubsan(port, request)?;
            Ok(UbsanObservation {
                verdict,
                finding: port.ubsan_finding.take(),
            })
        },
    )
}

fn with_sample_attempt<T>(
    input: PublicSampleInput,
    socket: PathBuf,
    staging: PathBuf,
    claim_deadline: Instant,
    cancellation: &AtomicBool,
    mode: DiagnosticMode,
    observe: impl FnOnce(
        &mut PublicSampleAttempt<'_>,
        &OptimizationRequest<'_>,
    ) -> Result<T, OptimizationError>,
) -> io::Result<DiagnosticRun<T>> {
    if cancellation.load(Ordering::Acquire) {
        return Ok(DiagnosticRun {
            result: Err(OptimizationError::Cancelled),
            stages: vec![],
        });
    }
    let configuration = &input.configuration;
    let required = Duration::from_micros(
        mode.passes()
            * (configuration.compile_limits.wall_time_us + configuration.run_limits.wall_time_us),
    );
    if claim_deadline.saturating_duration_since(Instant::now()) < required {
        return Ok(DiagnosticRun {
            result: Err(OptimizationError::Stage(failure(
                v1::FailureCode::JobDeadline,
                true,
            ))),
            stages: vec![],
        });
    }
    let mut port = PublicSampleAttempt {
        input: &input,
        io: Attempt::new(
            NoArtifacts,
            socket,
            staging,
            input.configuration.job_wall_time_us,
            cancellation,
        )?
        .cap_deadline(claim_deadline),
        program: None,
        mode,
        ubsan_finding: None,
    };
    let result = observe(&mut port, &input.request());
    Ok(DiagnosticRun {
        result,
        stages: port.io.stages,
    })
}

struct NoArtifacts;
impl ArtifactPort for NoArtifacts {
    fn read(&mut self, _: &v1::ArtifactReference) -> io::Result<Vec<u8>> {
        Err(io::Error::other(
            "diagnostics cannot fetch arbitrary artifacts",
        ))
    }
}

struct SealedProgram {
    metadata: v2::CompiledProgram,
    file: SnapshotFile,
}

struct PublicSampleAttempt<'a> {
    input: &'a PublicSampleInput,
    io: Attempt<'a, NoArtifacts>,
    program: Option<SealedProgram>,
    mode: DiagnosticMode,
    ubsan_finding: Option<UbsanFinding>,
}

impl OptimizationPreparationPort for PublicSampleAttempt<'_> {
    fn prepare_optimization(
        &mut self,
        request: &OptimizationRequest<'_>,
        level: OptimizationLevel,
        run_id: &str,
    ) -> Result<Compiled, StageFailure> {
        // A new optimization profile must never reuse the previous executable.
        self.program = None;
        if !self.input.matches(request) {
            return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
        }
        let spec =
            diagnostic_preparation_spec(request, level, run_id.into(), &self.input.source_bytes)
                .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, true))?;
        let validated = spec
            .clone()
            .validate_for_factory_client()
            .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, true))?;
        let publication = StagePublication::create(self.io.staging_path(), run_id)
            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, false))?;
        with_publication(publication, |publication| {
            publication
                .put(
                    "source",
                    &mut self.input.source_bytes.as_slice(),
                    self.input.source.bytes,
                )
                .map_err(artifact_error)?;
            let result = self.io.execute(spec, publication)?;
            let status = match result
                .report
                .classify(&validated)
                .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, false))?
            {
                ExecutionOutcome::Exited { code: 0 } => v1::CompilationStatus::Succeeded,
                ExecutionOutcome::Exited { .. } | ExecutionOutcome::Signaled { .. } => {
                    v1::CompilationStatus::CompilationError
                }
                ExecutionOutcome::LimitExceeded { .. } => v1::CompilationStatus::CompilationLimit,
                ExecutionOutcome::Cancelled => v1::CompilationStatus::Cancelled,
                _ => v1::CompilationStatus::SystemError,
            };
            let program = if status == v1::CompilationStatus::Succeeded {
                if result.outputs.len() != 1 || result.outputs[0].path != "main" {
                    return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
                }
                let file = result
                    .outputs
                    .into_iter()
                    .next()
                    .expect("one executable")
                    .file;
                let metadata = v2::CompiledProgram {
                    kind: v2::ProgramKind::NativeExecutable,
                    binding: self.input.binding.clone(),
                    source: self.input.source.clone(),
                    toolchain: self.input.configuration.toolchain.clone(),
                    files: vec![v2::ProgramFile {
                        path: "main".into(),
                        artifact: snapshot_reference(&file)?,
                    }],
                };
                self.input
                    .validate_program(&metadata)
                    .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, true))?;
                self.program = Some(SealedProgram {
                    metadata: metadata.clone(),
                    file,
                });
                Some(metadata)
            } else {
                None
            };
            Ok(Compiled {
                report: v2::CompilationReport {
                    status,
                    program,
                    diagnostics: String::new().try_into().expect("empty diagnostic text"),
                    diagnostics_truncated: false,
                    cpu_time_us: metric(&result.report.usage.cpu_time_us),
                    memory_peak_bytes: metric(&result.report.usage.memory_peak_bytes),
                },
                cleanup_complete: true,
                failure: (status == v1::CompilationStatus::SystemError)
                    .then_some(v1::FailureCode::SandboxUnavailable),
            })
        })
    }
}

const MAX_UBSAN_REPORT_BYTES: u64 = 16 * 1024;

fn ubsan_finding(stderr: &[u8], input: &PublicSampleInput) -> Option<UbsanFinding> {
    let text = std::str::from_utf8(stderr).ok()?;
    if !text.ends_with('\n') || text.contains('\0') {
        return None;
    }
    let prefix = match input.configuration.language {
        v2::Language::C17 => "/work/main.c:",
        v2::Language::Cpp20 => "/work/main.cpp:",
        _ => return None,
    };
    let mut candidates = text.lines().filter_map(|line| {
        let (location, message) = line.strip_prefix(prefix)?.split_once(": runtime error: ")?;
        let (line, column) = location.split_once(':')?;
        let line = line.parse::<u64>().ok()?;
        let column = column.parse::<u64>().ok()?;
        let category = if message.starts_with("signed integer overflow: ") {
            UbsanCategory::SignedIntegerOverflow
        } else if message == "division by zero" {
            UbsanCategory::DivisionByZero
        } else if message.starts_with("index ") && message.contains(" out of bounds for type ") {
            UbsanCategory::IndexOutOfBounds
        } else if message.starts_with("shift exponent ") || message.starts_with("left shift of ") {
            UbsanCategory::InvalidShift
        } else {
            return None;
        };
        let finding = UbsanFinding {
            category,
            line,
            column,
        };
        finding.validate().ok()?;
        (line
            <= input
                .source_bytes
                .split_inclusive(|byte| *byte == b'\n')
                .count() as u64)
            .then_some(finding)
    });
    let first = candidates.next()?;
    candidates.next().is_none().then_some(first)
}

impl RunPort for PublicSampleAttempt<'_> {
    fn run(
        &mut self,
        program: &v2::CompiledProgram,
        case: &v1::TestCase,
        budget: &v1::RunLimits,
        run_id: &str,
    ) -> Result<Observation, StageFailure> {
        if case != &self.input.sample || budget != &self.input.configuration.run_limits {
            return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
        }
        let spec = self.input.run_spec(program, run_id)?;
        let sealed = self
            .program
            .take()
            .filter(|p| &p.metadata == program)
            .ok_or_else(|| failure(v1::FailureCode::ArtifactUnavailable, true))?;
        if snapshot_reference(&sealed.file)? != program.files[0].artifact {
            return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
        }
        let validated = spec
            .clone()
            .validate_for_factory_client()
            .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, true))?;
        let publication = StagePublication::create(self.io.staging_path(), run_id)
            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, false))?;
        with_publication(publication, |publication| {
            publication
                .put(
                    "program",
                    &mut sealed.file.reader().map_err(artifact_error)?,
                    program.files[0].artifact.bytes,
                )
                .map_err(artifact_error)?;
            publication
                .put(
                    "stdin",
                    &mut self.input.input_bytes.as_slice(),
                    case.input.bytes,
                )
                .map_err(artifact_error)?;
            let result = self.io.execute(spec, publication)?;
            let outcome = result
                .report
                .classify(&validated)
                .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, false))?;
            if self.mode == DiagnosticMode::Ubsan
                && outcome == (ExecutionOutcome::Exited { code: 1 })
                && !result.report.stderr.truncated
                && result.report.stderr.retained_bytes <= MAX_UBSAN_REPORT_BYTES
            {
                let mut stderr = Vec::new();
                result
                    .stderr
                    .reader()
                    .map_err(artifact_error)?
                    .take(MAX_UBSAN_REPORT_BYTES + 1)
                    .read_to_end(&mut stderr)
                    .map_err(artifact_error)?;
                if stderr.len() as u64 <= MAX_UBSAN_REPORT_BYTES {
                    self.ubsan_finding = ubsan_finding(&stderr, self.input);
                }
            }
            let mut stdout = Vec::new();
            result
                .stdout
                .reader()
                .map_err(artifact_error)?
                .take(budget.stdout_bytes + 1)
                .read_to_end(&mut stdout)
                .map_err(artifact_error)?;
            if stdout.len() as u64 > budget.stdout_bytes {
                return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
            }
            Ok(Observation {
                kind: kind(outcome),
                stdout,
                stdout_truncated: result.report.stdout.truncated,
                stderr: vec![],
                stderr_truncated: false,
                cpu_time_us: metric(&result.report.usage.cpu_time_us),
                memory_peak_bytes: metric(&result.report.usage.memory_peak_bytes),
            })
        })
    }
}

impl ControlPort for PublicSampleAttempt<'_> {
    fn elapsed_us(&self) -> u64 {
        self.io.elapsed_us()
    }
    fn cancelled(&self) -> bool {
        self.io.cancelled()
    }
    fn stage(&mut self, _: v1::Stage, _: Option<u16>) -> Result<(), StageFailure> {
        // This adapter is never allowed to send formal evaluation stage updates.
        Err(failure(v1::FailureCode::SandboxUnavailable, true))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipeline::digest;
    use std::{fs, os::unix::fs::PermissionsExt};

    fn reference(bytes: &[u8]) -> v1::ArtifactReference {
        v1::ArtifactReference {
            digest: digest(bytes),
            bytes: bytes.len() as u64,
        }
    }

    fn input() -> PublicSampleInput {
        let m: v2::JobManifest = v1::decode_wire(include_bytes!(
            "../../../../tests/fixtures/contracts-v2/judge-manifest.valid-c17.json"
        ))
        .unwrap();
        let report: v2::JudgementReport = v1::decode_wire(include_bytes!(
            "../../../../tests/fixtures/contracts-v2/judge-result.valid-c17.json"
        ))
        .unwrap();
        let config = v2::LanguageConfiguration {
            language: m.language,
            toolchain: m.toolchain,
            job_wall_time_us: 2 * (m.compile_limits.wall_time_us + m.run_limits.wall_time_us),
            compile_limits: m.compile_limits,
            run_limits: m.run_limits,
        };
        // Pure adapter tests: neither these bytes nor a compiler ever execute.
        let source = b"synthetic diagnostic source";
        let stdin = b"public input";
        let expected = b"public answer";
        let sample = v1::TestCase {
            case_id: m.cases[0].case_id,
            input: reference(stdin),
            expected: reference(expected),
        };
        PublicSampleInput::from_resolved(
            &OptimizationRequest {
                configuration: &config,
                binding: &report.binding,
                source: &reference(source),
                public_sample: &sample,
                expected,
            },
            source.to_vec(),
            stdin.to_vec(),
        )
        .unwrap()
    }

    #[test]
    fn ubsan_uses_only_bounded_reports_pointing_to_the_bound_source() {
        let source = input();
        let finding = ubsan_finding(
            b"/work/main.c:1:1: runtime error: division by zero\n",
            &source,
        );
        assert_eq!(
            finding.map(|value| value.category),
            Some(UbsanCategory::DivisionByZero)
        );
        for raw in [
            b"/work/main.c:99:1: runtime error: division by zero\n".as_slice(),
            b"/other.c:1:1: runtime error: division by zero\n",
            b"/work/main.c:1:1: runtime error: arbitrary message\n",
            b"/work/main.c:1:1: runtime error: division by zero",
            b"/work/main.c:1:1: runtime error: division by zero\n/work/main.c:1:1: runtime error: division by zero\n",
        ] {
            assert!(ubsan_finding(raw, &source).is_none());
        }
    }

    #[test]
    fn verifies_all_three_artifacts_before_accepting_a_resolved_sample() {
        let input = input();
        for target in ["source", "input", "expected"] {
            let mut source = input.source_bytes.clone();
            let mut stdin = input.input_bytes.clone();
            let mut expected = input.expected.clone();
            match target {
                "source" => source[0] ^= 1,
                "input" => stdin[0] ^= 1,
                _ => expected[0] ^= 1,
            }
            let request = OptimizationRequest {
                expected: &expected,
                ..input.request()
            };
            assert!(matches!(
                PublicSampleInput::from_resolved(&request, source, stdin),
                Err(OptimizationError::InvalidRequest(_))
            ));
        }
        let mut config = input.configuration.clone();
        config.job_wall_time_us /= 2;
        let request = OptimizationRequest {
            configuration: &config,
            ..input.request()
        };
        assert!(matches!(
            PublicSampleInput::from_resolved(
                &request,
                input.source_bytes.clone(),
                input.input_bytes.clone()
            ),
            Err(OptimizationError::InvalidRequest(_))
        ));
    }

    #[test]
    fn native_request_is_pinned_and_changed_scope_cannot_reach_staging() {
        let input = input();
        let program = v2::CompiledProgram {
            kind: v2::ProgramKind::NativeExecutable,
            binding: input.binding.clone(),
            source: input.source.clone(),
            toolchain: input.configuration.toolchain.clone(),
            files: vec![v2::ProgramFile {
                path: "main".into(),
                artifact: reference(b"not executable"),
            }],
        };
        let spec = input.run_spec(&program, "diagnostic-run").unwrap();
        assert_eq!(
            spec.runtime_identity.as_ref(),
            Some(&input.configuration.toolchain)
        );
        assert_eq!(spec.argv, ["/work/main"]);
        assert_eq!(spec.limits, limits(&input.configuration.run_limits));
        assert_eq!(spec.inputs.len(), 1);
        assert_eq!(spec.inputs[0].artifact_id, "program");
        assert_eq!(spec.stdin_artifact.as_deref(), Some("stdin"));
        assert!(spec.outputs.is_empty() && spec.env.is_empty());
        for target in ["source", "attempt", "runtime", "budget"] {
            let mut changed = program.clone();
            match target {
                "source" => changed.source.digest = digest(b"other source"),
                "attempt" => {
                    changed.binding.attempt_id =
                        uuid::Uuid::new_v4().to_string().try_into().unwrap()
                }
                "runtime" => changed.toolchain.image_digest = digest(b"other runtime"),
                _ => {
                    changed.files[0].artifact.bytes =
                        input.configuration.compile_limits.file_bytes + 1
                }
            }
            assert!(input.run_spec(&changed, "diagnostic-run").is_err());
        }
        let cancel = AtomicBool::new(false);
        let mut port = PublicSampleAttempt {
            input: &input,
            io: Attempt::new(
                NoArtifacts,
                "/unused.sock".into(),
                "/unused-staging".into(),
                input.configuration.job_wall_time_us,
                &cancel,
            )
            .unwrap(),
            program: None,
            mode: DiagnosticMode::Optimization,
            ubsan_finding: None,
        };
        let mut sample = input.sample.clone();
        sample.case_id = uuid::Uuid::new_v4().to_string().try_into().unwrap();
        let changed = OptimizationRequest {
            public_sample: &sample,
            ..input.request()
        };
        assert!(
            port.prepare_optimization(&changed, OptimizationLevel::O2, "changed")
                .is_err()
        );
        assert!(
            port.run(
                &program,
                &sample,
                &input.configuration.run_limits,
                "changed"
            )
            .is_err()
        );
        let mut budget = input.configuration.run_limits.clone();
        budget.cpu_time_us += 1;
        assert!(
            port.run(&program, &input.sample, &budget, "changed")
                .is_err()
        );
        assert!(
            port.run(
                &program,
                &input.sample,
                &input.configuration.run_limits,
                "unsealed"
            )
            .is_err()
        );
        assert!(port.stage(v1::Stage::Running, Some(0)).is_err());
        assert!(port.io.stages.is_empty());
    }

    #[test]
    fn cancelled_or_missing_factory_never_yields_a_diagnostic_and_cleans_staging() {
        let parent =
            std::env::temp_dir().join(format!("kf-public-diagnostic-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&parent).unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o700)).unwrap();
        let socket = parent.join("missing.sock");
        let cancelled = AtomicBool::new(true);
        let live_deadline = Instant::now()
            + Duration::from_micros(input().configuration.job_wall_time_us)
            + Duration::from_secs(1);
        let run = observe_public_sample(
            input(),
            socket.clone(),
            parent.clone(),
            live_deadline,
            &cancelled,
        )
        .unwrap();
        assert!(matches!(run.result, Err(OptimizationError::Cancelled)));
        assert!(run.stages.is_empty());
        assert_eq!(fs::read_dir(&parent).unwrap().count(), 0);
        let live = AtomicBool::new(false);
        let expired = observe_public_sample(
            input(),
            socket.clone(),
            parent.clone(),
            Instant::now(),
            &live,
        )
        .unwrap();
        assert!(matches!(
            expired.result,
            Err(OptimizationError::Stage(StageFailure {
                code: v1::FailureCode::JobDeadline,
                cleanup_complete: true,
            }))
        ));
        assert!(expired.stages.is_empty());
        assert_eq!(fs::read_dir(&parent).unwrap().count(), 0);
        let run =
            observe_public_sample(input(), socket, parent.clone(), live_deadline, &live).unwrap();
        assert!(matches!(
            run.result,
            Err(OptimizationError::Stage(StageFailure {
                code: v1::FailureCode::CleanupFailed,
                cleanup_complete: false,
            }))
        ));
        assert!(run.stages.is_empty());
        assert_eq!(fs::read_dir(&parent).unwrap().count(), 0);
        fs::remove_dir(parent).unwrap();
    }
}
