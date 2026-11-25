//! Optimization comparison for one public sample from an admitted diagnostic job.
//! The trusted caller resolves the public sample and owns sandbox admission.

use crate::ExecutionKind;
use crate::submission::{ControlPort, StageFailure, classify};
use crate::submission_v2::{Compiled, RunPort};
use kf_contracts::judging::{MAX_PACKAGE_BYTES, MAX_SOURCE_BYTES};
use kf_contracts::judging_v2::{
    ArtifactReference, CompilationStatus, FailureCode, Identifier, Language, LanguageConfiguration,
    LeaseBinding, TestCase, Verdict,
};
use serde::Serialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OptimizationLevel {
    O0,
    O2,
}

pub struct OptimizationRequest<'a> {
    pub configuration: &'a LanguageConfiguration,
    pub binding: &'a LeaseBinding,
    pub source: &'a ArtifactReference,
    /// Resolved by the caller from the published sample, outside the formal test package.
    pub public_sample: &'a TestCase,
    /// The caller verifies this artifact against the published sample before invocation.
    pub expected: &'a [u8],
}

pub trait OptimizationPreparationPort {
    fn prepare_optimization(
        &mut self,
        request: &OptimizationRequest<'_>,
        level: OptimizationLevel,
        run_id: &str,
    ) -> Result<Compiled, StageFailure>;
}

#[derive(Debug)]
pub enum OptimizationError {
    InvalidRequest(&'static str),
    InvalidPreparation(&'static str),
    Cancelled,
    Stage(StageFailure),
}

#[derive(Debug, Serialize)]
pub struct OptimizationObservation {
    pub case_id: Identifier,
    pub o2: Verdict,
    pub o0: Option<Verdict>,
    pub optimization_sensitive: bool,
}

/// A failed baseline is checked under O0 only when it can yield an optimization
/// finding. Report a difference as an observation, not a diagnosis of UB.
pub fn observe_optimization<P: OptimizationPreparationPort + RunPort + ControlPort>(
    port: &mut P,
    request: &OptimizationRequest<'_>,
) -> Result<OptimizationObservation, OptimizationError> {
    validate(request).map_err(OptimizationError::InvalidRequest)?;
    let o2 = probe(port, request, OptimizationLevel::O2)?;
    let o0 = if matches!(o2, Verdict::WrongAnswer | Verdict::RuntimeError) {
        Some(probe(port, request, OptimizationLevel::O0)?)
    } else {
        None
    };
    Ok(OptimizationObservation {
        case_id: request.public_sample.case_id,
        o2,
        o0,
        optimization_sensitive: o0 == Some(Verdict::Accepted),
    })
}

fn validate(request: &OptimizationRequest<'_>) -> Result<(), &'static str> {
    let configuration = request.configuration;
    if !matches!(configuration.language, Language::C17 | Language::Cpp20) {
        return Err("optimization comparison supports only C17 and C++20");
    }
    configuration.compile_limits.validate()?;
    configuration.run_limits.validate()?;
    configuration.validate()?;
    request.binding.validate()?;
    request.source.validate()?;
    let sample = request.public_sample;
    sample.validate()?;
    if request.source.bytes == 0
        || request.source.bytes > MAX_SOURCE_BYTES
        || request.source.bytes + sample.input.bytes + sample.expected.bytes > MAX_PACKAGE_BYTES
        || sample.expected.bytes != request.expected.len() as u64
        || sample.expected.bytes > configuration.run_limits.stdout_bytes
    {
        return Err("invalid source or public sample budget");
    }
    let pair_wall_time =
        2 * (configuration.compile_limits.wall_time_us + configuration.run_limits.wall_time_us);
    if configuration.job_wall_time_us < pair_wall_time {
        return Err("diagnostic deadline cannot cover both profiles");
    }
    Ok(())
}

fn gate(
    port: &impl ControlPort,
    configuration: &LanguageConfiguration,
) -> Result<(), OptimizationError> {
    if port.cancelled() {
        return Err(OptimizationError::Cancelled);
    }
    if port.elapsed_us() >= configuration.job_wall_time_us {
        return Err(stage_error(FailureCode::JobDeadline));
    }
    Ok(())
}

fn stage_error(code: FailureCode) -> OptimizationError {
    OptimizationError::Stage(StageFailure {
        code,
        cleanup_complete: code != FailureCode::CleanupFailed,
    })
}

fn normalize_failure(failure: StageFailure) -> OptimizationError {
    if failure.cleanup_complete && failure.code != FailureCode::CleanupFailed {
        OptimizationError::Stage(failure)
    } else {
        stage_error(FailureCode::CleanupFailed)
    }
}

fn probe<P: OptimizationPreparationPort + RunPort + ControlPort>(
    port: &mut P,
    request: &OptimizationRequest<'_>,
    level: OptimizationLevel,
) -> Result<Verdict, OptimizationError> {
    let configuration = request.configuration;
    gate(port, configuration)?;
    let name = match level {
        OptimizationLevel::O0 => "do0",
        OptimizationLevel::O2 => "do2",
    };
    let attempt = request.binding.attempt_id.as_uuid();
    let prepared = port
        .prepare_optimization(request, level, &format!("{name}-c-{attempt}"))
        .map_err(normalize_failure)?;
    if !prepared.cleanup_complete {
        return Err(stage_error(FailureCode::CleanupFailed));
    }
    if let Some(failure) = prepared.failure {
        return Err(stage_error(failure));
    }
    gate(port, configuration)?;
    prepared
        .report
        .validate()
        .map_err(OptimizationError::InvalidPreparation)?;
    let Some(program) = prepared.report.program else {
        return match prepared.report.status {
            CompilationStatus::CompilationError | CompilationStatus::CompilationLimit => {
                Ok(Verdict::CompilationError)
            }
            CompilationStatus::Cancelled => Err(OptimizationError::Cancelled),
            _ => Err(stage_error(FailureCode::SandboxUnavailable)),
        };
    };
    if program.binding != *request.binding
        || program.source != *request.source
        || program.toolchain != configuration.toolchain
        || program.kind != configuration.language.contract().program_kind
        || program
            .files
            .iter()
            .any(|f| f.artifact.bytes > configuration.compile_limits.file_bytes)
        || program.bytes() > configuration.compile_limits.workdir_bytes
    {
        return Err(OptimizationError::InvalidPreparation(
            "prepared program differs from diagnostic authority",
        ));
    }
    gate(port, configuration)?;
    let observed = port
        .run(
            &program,
            request.public_sample,
            &configuration.run_limits,
            &format!("{name}-r-{attempt}"),
        )
        .map_err(normalize_failure)?;
    if observed.kind == ExecutionKind::InfrastructureFailure {
        return Err(stage_error(FailureCode::SandboxUnavailable));
    }
    gate(port, configuration)?;
    if observed.kind == ExecutionKind::Cancelled {
        return Err(OptimizationError::Cancelled);
    }
    if observed.kind == (ExecutionKind::Exited { code: 0 }) && observed.stdout_truncated {
        return Ok(Verdict::OutputLimitExceeded);
    }
    Ok(classify(&observed, request.expected))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Observation;
    use kf_contracts::judging::decode_wire;
    use kf_contracts::judging_v2::{
        CompilationReport, JobManifest, JudgementReport, RunLimits, Stage,
    };
    use std::collections::VecDeque;

    struct Port {
        program: CompilationReport,
        observations: VecDeque<Observation>,
        calls: Vec<(OptimizationLevel, String)>,
        run_ids: Vec<String>,
        sample_id: Identifier,
        run_budget: RunLimits,
        cleanup_complete: bool,
        cancel_after_run: bool,
        deadline_after_run: Option<u64>,
    }

    impl ControlPort for Port {
        fn elapsed_us(&self) -> u64 {
            if self.run_ids.is_empty() {
                0
            } else {
                self.deadline_after_run.unwrap_or(0)
            }
        }

        fn cancelled(&self) -> bool {
            self.cancel_after_run && !self.run_ids.is_empty()
        }

        fn stage(&mut self, _: Stage, _: Option<u16>) -> Result<(), StageFailure> {
            panic!("a diagnostic must not publish formal judgement stages");
        }
    }

    impl OptimizationPreparationPort for Port {
        fn prepare_optimization(
            &mut self,
            _: &OptimizationRequest<'_>,
            level: OptimizationLevel,
            run_id: &str,
        ) -> Result<Compiled, StageFailure> {
            self.calls.push((level, run_id.to_owned()));
            Ok(Compiled {
                report: self.program.clone(),
                cleanup_complete: self.cleanup_complete,
                failure: None,
            })
        }
    }

    impl RunPort for Port {
        fn run(
            &mut self,
            program: &kf_contracts::judging_v2::CompiledProgram,
            case: &TestCase,
            budget: &RunLimits,
            run_id: &str,
        ) -> Result<Observation, StageFailure> {
            assert_eq!(Some(program), self.program.program.as_ref());
            assert_eq!(case.case_id, self.sample_id);
            assert_eq!(*budget, self.run_budget);
            self.run_ids.push(run_id.to_owned());
            Ok(self.observations.pop_front().unwrap())
        }
    }

    fn signal(kind: ExecutionKind, stdout: &[u8]) -> Observation {
        Observation {
            stderr: "private runtime output".into(),
            stdout_truncated: false,
            stderr_truncated: false,
            kind,
            stdout: stdout.to_vec(),
            cpu_time_us: Some(1),
            memory_peak_bytes: Some(2),
        }
    }

    fn fixture(
        language: &str,
        observations: impl IntoIterator<Item = Observation>,
    ) -> (JobManifest, LanguageConfiguration, Port) {
        let fixtures = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/contracts-v2");
        let manifest: JobManifest = decode_wire(
            &std::fs::read(fixtures.join(format!("judge-manifest.valid-{language}.json"))).unwrap(),
        )
        .unwrap();
        let judgement: JudgementReport = decode_wire(
            &std::fs::read(fixtures.join(format!("judge-result.valid-{language}.json"))).unwrap(),
        )
        .unwrap();
        let configuration = LanguageConfiguration {
            language: manifest.language,
            toolchain: manifest.toolchain.clone(),
            compile_limits: manifest.compile_limits.clone(),
            run_limits: manifest.run_limits.clone(),
            job_wall_time_us: 2
                * (manifest.compile_limits.wall_time_us + manifest.run_limits.wall_time_us),
        };
        let port = Port {
            program: judgement.compilation,
            observations: observations.into_iter().collect(),
            calls: vec![],
            run_ids: vec![],
            sample_id: manifest.cases[1].case_id,
            run_budget: configuration.run_limits.clone(),
            cleanup_complete: true,
            cancel_after_run: false,
            deadline_after_run: None,
        };
        (manifest, configuration, port)
    }

    fn request<'a>(
        manifest: &'a JobManifest,
        configuration: &'a LanguageConfiguration,
        binding: &'a LeaseBinding,
    ) -> OptimizationRequest<'a> {
        OptimizationRequest {
            configuration,
            binding,
            source: &manifest.source,
            public_sample: &manifest.cases[1],
            expected: b"0",
        }
    }

    #[test]
    fn checks_wrong_answer_and_runtime_failure_against_the_same_public_input() {
        for (language, failure) in [
            ("c17", ExecutionKind::Exited { code: 0 }),
            ("cpp20", ExecutionKind::Exited { code: 1 }),
        ] {
            let (manifest, configuration, mut port) = fixture(
                language,
                [
                    signal(failure, b"1"),
                    signal(ExecutionKind::Exited { code: 0 }, b"0"),
                ],
            );
            let binding = port.program.program.as_ref().unwrap().binding.clone();
            let result =
                observe_optimization(&mut port, &request(&manifest, &configuration, &binding))
                    .unwrap();
            assert_eq!(
                port.calls
                    .iter()
                    .map(|(level, _)| *level)
                    .collect::<Vec<_>>(),
                [OptimizationLevel::O2, OptimizationLevel::O0]
            );
            assert_ne!(port.calls[0].1, port.calls[1].1);
            assert_ne!(port.run_ids[0], port.run_ids[1]);
            assert_eq!(result.o0, Some(Verdict::Accepted));
            assert!(result.optimization_sensitive);
            assert!(
                !serde_json::to_string(&result)
                    .unwrap()
                    .contains("private runtime output")
            );
        }
    }

    #[test]
    fn skips_a_second_profile_when_the_baseline_cannot_yield_a_finding() {
        let mut truncated = signal(ExecutionKind::Exited { code: 0 }, b"0");
        truncated.stdout_truncated = true;
        for (observation, verdict) in [
            (
                signal(ExecutionKind::Exited { code: 0 }, b"0"),
                Verdict::Accepted,
            ),
            (
                signal(ExecutionKind::TimeLimit, b""),
                Verdict::TimeLimitExceeded,
            ),
            (truncated, Verdict::OutputLimitExceeded),
        ] {
            let (manifest, configuration, mut port) = fixture("c17", [observation]);
            let binding = port.program.program.as_ref().unwrap().binding.clone();
            let result =
                observe_optimization(&mut port, &request(&manifest, &configuration, &binding))
                    .unwrap();
            assert_eq!(result.o2, verdict);
            assert_eq!(result.o0, None);
            assert!(!result.optimization_sensitive);
            assert_eq!(port.calls.len(), 1);
        }
    }

    #[test]
    fn a_second_failure_is_an_observation_without_an_optimization_finding() {
        let (manifest, configuration, mut port) = fixture(
            "cpp20",
            [
                signal(ExecutionKind::Exited { code: 0 }, b"1"),
                signal(ExecutionKind::Signaled, b""),
            ],
        );
        let binding = port.program.program.as_ref().unwrap().binding.clone();
        let result =
            observe_optimization(&mut port, &request(&manifest, &configuration, &binding)).unwrap();
        assert_eq!(result.o2, Verdict::WrongAnswer);
        assert_eq!(result.o0, Some(Verdict::RuntimeError));
        assert!(!result.optimization_sensitive);
    }

    #[test]
    fn stops_on_cancellation_deadline_or_failed_cleanup() {
        for condition in ["cancel", "deadline", "cleanup"] {
            let (manifest, configuration, mut port) =
                fixture("c17", [signal(ExecutionKind::Exited { code: 0 }, b"wrong")]);
            let binding = port.program.program.as_ref().unwrap().binding.clone();
            port.cancel_after_run = condition == "cancel";
            port.deadline_after_run =
                (condition == "deadline").then_some(configuration.job_wall_time_us);
            port.cleanup_complete = condition != "cleanup";
            let error =
                observe_optimization(&mut port, &request(&manifest, &configuration, &binding))
                    .unwrap_err();
            match (condition, error) {
                ("cancel", OptimizationError::Cancelled)
                | (
                    "deadline",
                    OptimizationError::Stage(StageFailure {
                        code: FailureCode::JobDeadline,
                        cleanup_complete: true,
                    }),
                )
                | (
                    "cleanup",
                    OptimizationError::Stage(StageFailure {
                        code: FailureCode::CleanupFailed,
                        cleanup_complete: false,
                    }),
                ) => {}
                (_, error) => panic!("unexpected diagnostic error: {error:?}"),
            }
            assert_eq!(port.calls.len(), 1);
            assert_eq!(port.run_ids.is_empty(), condition == "cleanup");
        }
    }

    #[test]
    fn refuses_invalid_samples_and_program_bindings_before_running() {
        let (manifest, configuration, mut port) = fixture("c17", []);
        let binding = port.program.program.as_ref().unwrap().binding.clone();
        let mut request = request(&manifest, &configuration, &binding);
        request.expected = b"too long";
        assert!(matches!(
            observe_optimization(&mut port, &request),
            Err(OptimizationError::InvalidRequest(_))
        ));
        assert!(port.calls.is_empty());

        request.expected = b"0";
        port.program.program.as_mut().unwrap().source.bytes += 1;
        assert!(matches!(
            observe_optimization(&mut port, &request),
            Err(OptimizationError::InvalidPreparation(_))
        ));
        assert!(port.run_ids.is_empty());
    }
}
