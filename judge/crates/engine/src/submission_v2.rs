//! Explicit v2 preparation and typed program execution; legacy wire stays separate.
//! Ports own clocks/cancellation/storage/execution; this module is pure domain.
use super::submission::{
    ControlPort, ExpectedPort, SkipReason, SkippedCase, StageFailure, classify,
};
use crate::{ExecutionKind, Observation};
use kf_contracts::judging_v2::*;
use serde::Serialize;
pub struct Compiled {
    pub report: CompilationReport,
    pub cleanup_complete: bool,
    pub failure: Option<FailureCode>,
}
pub trait PreparationPort {
    fn prepare(
        &mut self,
        manifest: &JobManifest,
        binding: &LeaseBinding,
        run_id: &str,
    ) -> Result<Compiled, StageFailure>;
}
pub trait RunPort {
    fn run(
        &mut self,
        program: &CompiledProgram,
        case: &TestCase,
        limits: &RunLimits,
        run_id: &str,
    ) -> Result<Observation, StageFailure>;
}
#[derive(Debug, Serialize)]
pub struct SubmissionResult {
    pub report: JudgementReport,
    pub compile_run_id: String,
    pub case_run_ids: Vec<String>,
    pub skipped: Vec<SkippedCase>,
}
fn empty_compile(status: CompilationStatus) -> CompilationReport {
    CompilationReport {
        status,
        program: None,
        diagnostics: String::new().try_into().expect("empty diagnostics"),
        diagnostics_truncated: false,
        cpu_time_us: None,
        memory_peak_bytes: None,
    }
}
fn failure(code: FailureCode) -> StageFailure {
    StageFailure {
        code,
        cleanup_complete: true,
    }
}
fn gate(p: &impl ControlPort, m: &JobManifest) -> Result<bool, StageFailure> {
    if p.elapsed_us() >= m.job_wall_time_us {
        return Err(failure(FailureCode::JobDeadline));
    }
    Ok(p.cancelled())
}
pub fn judge_submission<P: PreparationPort + RunPort + ExpectedPort + ControlPort>(
    p: &mut P,
    m: &JobManifest,
    binding: LeaseBinding,
    digest: Digest,
) -> Result<SubmissionResult, &'static str> {
    m.validate()?;
    binding.validate()?;
    if binding.evaluation_id != m.evaluation_id {
        return Err("submission binding differs");
    }
    let compile_run_id = format!("c-{}", binding.attempt_id.as_uuid());
    let compilation = match gate(p, m) {
        Ok(true) => Ok(Compiled {
            report: empty_compile(CompilationStatus::Cancelled),
            cleanup_complete: true,
            failure: None,
        }),
        Err(e) => Err(e),
        Ok(false) => p
            .stage(Stage::Compiling, None)
            .and_then(|()| p.prepare(m, &binding, &compile_run_id)),
    };
    let mut cleanup_complete;
    let mut failure_code;
    let mut compilation = match compilation {
        Ok(c) => {
            c.report.validate()?;
            if c.report.program.as_ref().is_some_and(|program| {
                program.binding != binding
                    || program.source != m.source
                    || program.toolchain != m.toolchain
                    || program.kind != m.language.contract().program_kind
                    || (program.kind != ProgramKind::PythonSource
                        && (program
                            .files
                            .iter()
                            .any(|f| f.artifact.bytes > m.compile_limits.file_bytes)
                            || program.bytes() > m.compile_limits.workdir_bytes))
            }) {
                return Err("preparation program differs from authorized manifest or lease");
            }
            cleanup_complete = c.cleanup_complete;
            failure_code = c.failure;
            if !cleanup_complete {
                failure_code = Some(FailureCode::CleanupFailed);
            }
            if failure_code.is_some() {
                empty_compile(CompilationStatus::SystemError)
            } else {
                c.report
            }
        }
        Err(e) => {
            cleanup_complete = e.cleanup_complete;
            failure_code = Some(if cleanup_complete {
                e.code
            } else {
                FailureCode::CleanupFailed
            });
            empty_compile(CompilationStatus::SystemError)
        }
    };
    if failure_code.is_none() && p.elapsed_us() >= m.job_wall_time_us {
        failure_code = Some(FailureCode::JobDeadline);
        if compilation.status != CompilationStatus::Succeeded {
            compilation.status = CompilationStatus::SystemError;
            compilation.program = None;
        }
    }
    let mut cases = Vec::with_capacity(m.cases.len());
    let mut run_ids = Vec::with_capacity(m.cases.len());
    if let Some(program) = &compilation.program {
        for (index, c) in m.cases.iter().enumerate() {
            match gate(p, m) {
                Ok(true) => break,
                Err(e) => {
                    failure_code = Some(e.code);
                    break;
                }
                Ok(false) => {}
            }
            let run_id = format!("r-{}-{index}", binding.attempt_id.as_uuid());
            let observation = p
                .stage(Stage::Running, Some(index as u16))
                .and_then(|()| p.run(program, c, &m.run_limits, &run_id));
            run_ids.push(run_id);
            let result = match observation {
                Err(e) => {
                    cleanup_complete = e.cleanup_complete;
                    failure_code = Some(if cleanup_complete {
                        e.code
                    } else {
                        FailureCode::CleanupFailed
                    });
                    CaseReport {
                        case_id: c.case_id,
                        verdict: Verdict::SystemError,
                        cpu_time_us: None,
                        memory_peak_bytes: None,
                    }
                }
                Ok(o) => {
                    let verdict = if o.kind == ExecutionKind::InfrastructureFailure {
                        Verdict::SystemError
                    } else {
                        match gate(p, m) {
                            Err(e) => {
                                failure_code = Some(e.code);
                                Verdict::SystemError
                            }
                            Ok(true) => Verdict::Cancelled,
                            Ok(false) if !matches!(o.kind, ExecutionKind::Exited { code: 0 }) => {
                                classify(&o, &[])
                            }
                            Ok(false) => match p.expected(c) {
                                Ok(bytes)
                                    if bytes.len() as u64 == c.expected.bytes
                                        && bytes.len() as u64 <= m.run_limits.stdout_bytes =>
                                {
                                    classify(&o, &bytes)
                                }
                                Ok(_) => {
                                    failure_code = Some(FailureCode::ArtifactUnavailable);
                                    Verdict::SystemError
                                }
                                Err(e) => {
                                    failure_code = Some(e.code);
                                    Verdict::SystemError
                                }
                            },
                        }
                    };
                    if verdict == Verdict::SystemError && failure_code.is_none() {
                        failure_code = Some(FailureCode::SandboxUnavailable);
                    }
                    CaseReport {
                        case_id: c.case_id,
                        verdict,
                        cpu_time_us: o.cpu_time_us,
                        memory_peak_bytes: o.memory_peak_bytes,
                    }
                }
            };
            let stop = matches!(result.verdict, Verdict::SystemError | Verdict::Cancelled);
            cases.push(result);
            if stop {
                break;
            }
        }
    }
    let verdict = if failure_code.is_some() {
        Verdict::SystemError
    } else {
        match compilation.status {
            CompilationStatus::CompilationError | CompilationStatus::CompilationLimit => {
                Verdict::CompilationError
            }
            CompilationStatus::Cancelled => Verdict::Cancelled,
            CompilationStatus::SystemError => {
                failure_code = Some(FailureCode::SandboxUnavailable);
                Verdict::SystemError
            }
            CompilationStatus::Succeeded => {
                if cases.len() != m.cases.len()
                    || cases.iter().any(|c| c.verdict == Verdict::Cancelled)
                {
                    Verdict::Cancelled
                } else {
                    cases
                        .iter()
                        .find(|c| c.verdict != Verdict::Accepted)
                        .map_or(Verdict::Accepted, |c| c.verdict)
                }
            }
        }
    };
    let cpu = if !cases.is_empty() && cases.iter().all(|c| c.cpu_time_us.is_some()) {
        Some(
            cases
                .iter()
                .try_fold(0_u64, |n, c| n.checked_add(c.cpu_time_us.unwrap_or(0)))
                .ok_or("CPU sum overflow")?,
        )
    } else {
        None
    };
    let memory = if !cases.is_empty() && cases.iter().all(|c| c.memory_peak_bytes.is_some()) {
        cases.iter().filter_map(|c| c.memory_peak_bytes).max()
    } else {
        None
    };
    let reason = if verdict == Verdict::Cancelled {
        SkipReason::Cancelled
    } else if verdict == Verdict::CompilationError {
        SkipReason::CompilationFailed
    } else {
        SkipReason::InfrastructureFailure
    };
    let skipped = m.cases[cases.len()..]
        .iter()
        .map(|c| SkippedCase {
            case_id: c.case_id,
            reason,
        })
        .collect();
    let report = JudgementReport {
        schema_version: SchemaVersion,
        kind: ResultKind::Value,
        binding,
        manifest_digest: digest,
        compilation,
        cases,
        verdict,
        cleanup_complete,
        failure_code,
        run_cpu_time_us: cpu,
        run_memory_peak_bytes: memory,
    };
    report.validate_for_manifest(m, &report.manifest_digest)?;
    Ok(SubmissionResult {
        report,
        compile_run_id,
        case_run_ids: run_ids,
        skipped,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Port {
        prepared: CompiledProgram,
        observations: Vec<Observation>,
        prepare_status: CompilationStatus,
        prepares: usize,
        runs: usize,
        expected_reads: usize,
        cancel_after: usize,
        clock: u64,
        advance_us: u64,
        fail_cleanup: bool,
    }
    impl PreparationPort for Port {
        fn prepare(
            &mut self,
            _: &JobManifest,
            _: &LeaseBinding,
            _: &str,
        ) -> Result<Compiled, StageFailure> {
            self.prepares += 1;
            Ok(Compiled {
                report: CompilationReport {
                    status: self.prepare_status,
                    program: (self.prepare_status == CompilationStatus::Succeeded)
                        .then(|| self.prepared.clone()),
                    diagnostics: String::new().try_into().unwrap(),
                    diagnostics_truncated: false,
                    cpu_time_us: None,
                    memory_peak_bytes: Some(4),
                },
                cleanup_complete: !self.fail_cleanup,
                failure: None,
            })
        }
    }
    impl RunPort for Port {
        fn run(
            &mut self,
            program: &CompiledProgram,
            _: &TestCase,
            _: &RunLimits,
            _: &str,
        ) -> Result<Observation, StageFailure> {
            assert_eq!(program, &self.prepared);
            self.runs += 1;
            Ok(self.observations.remove(0))
        }
    }
    impl ExpectedPort for Port {
        fn expected(&mut self, case: &TestCase) -> Result<Vec<u8>, StageFailure> {
            self.expected_reads += 1;
            Ok(vec![0; case.expected.bytes as usize])
        }
    }
    impl ControlPort for Port {
        fn elapsed_us(&self) -> u64 {
            self.clock
                .saturating_add(self.runs as u64 * self.advance_us)
        }
        fn cancelled(&self) -> bool {
            self.runs >= self.cancel_after
        }
        fn stage(&mut self, _: Stage, _: Option<u16>) -> Result<(), StageFailure> {
            Ok(())
        }
    }
    fn fixture(language: &str) -> (JobManifest, LeaseBinding, Digest, Port) {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/contracts-v2");
        let m: JobManifest = kf_contracts::judging::decode_wire(
            &std::fs::read(root.join(format!("judge-manifest.valid-{language}.json"))).unwrap(),
        )
        .unwrap();
        let r: JudgementReport = kf_contracts::judging::decode_wire(
            &std::fs::read(root.join(format!("judge-result.valid-{language}.json"))).unwrap(),
        )
        .unwrap();
        let p = Port {
            prepared: r.compilation.program.unwrap(),
            observations: m
                .cases
                .iter()
                .map(|case| Observation {
                    kind: ExecutionKind::Exited { code: 0 },
                    stdout: vec![0; case.expected.bytes as usize],
                    cpu_time_us: Some(3),
                    memory_peak_bytes: None,
                })
                .collect(),
            prepare_status: CompilationStatus::Succeeded,
            prepares: 0,
            runs: 0,
            expected_reads: 0,
            cancel_after: usize::MAX,
            clock: 0,
            advance_us: 0,
            fail_cleanup: false,
        };
        (m, r.binding, r.manifest_digest, p)
    }
    #[test]
    fn typed_prepare_once_same_program_every_point_and_unknown_measurements() {
        for language in ["c17", "cpp20", "python313", "java21"] {
            let (m, b, d, mut p) = fixture(language);
            let r = judge_submission(&mut p, &m, b, d).unwrap();
            assert_eq!(p.prepares, 1);
            assert_eq!(p.runs, m.cases.len());
            assert_eq!(r.report.verdict, Verdict::Accepted);
            assert_eq!(r.report.run_memory_peak_bytes, None);
            assert_eq!(r.report.compilation.cpu_time_us, None);
            assert_eq!(r.report.run_cpu_time_us, Some(3 * m.cases.len() as u64));
            assert!(r.skipped.is_empty());
        }
    }
    #[test]
    fn bad_program_is_refused_before_any_execution_or_expected_read() {
        let (m, b, d, mut p) = fixture("java21");
        p.prepared.source.digest = "f".repeat(64).try_into().unwrap();
        assert!(judge_submission(&mut p, &m, b, d).is_err());
        assert_eq!(p.runs, 0);
        assert_eq!(p.expected_reads, 0);
        let (m, b, d, mut p) = fixture("python313");
        p.prepared.binding.fencing_version += 1;
        assert!(judge_submission(&mut p, &m, b, d).is_err());
        assert_eq!(p.runs, 0);
    }
    #[test]
    fn preparation_failure_and_cleanup_precedence_never_fake_points() {
        for status in [
            CompilationStatus::CompilationError,
            CompilationStatus::CompilationLimit,
            CompilationStatus::Cancelled,
        ] {
            let (m, b, d, mut p) = fixture("java21");
            p.prepare_status = status;
            let r = judge_submission(&mut p, &m, b, d).unwrap();
            assert_eq!(p.runs, 0);
            assert_eq!(r.skipped.len(), m.cases.len());
            assert_eq!(
                r.report.verdict,
                if status == CompilationStatus::Cancelled {
                    Verdict::Cancelled
                } else {
                    Verdict::CompilationError
                }
            );
        }
        let (m, b, d, mut p) = fixture("python313");
        p.fail_cleanup = true;
        let r = judge_submission(&mut p, &m, b, d).unwrap();
        assert_eq!(r.report.failure_code, Some(FailureCode::CleanupFailed));
        assert!(!r.report.cleanup_complete);
        assert_eq!(p.runs, 0);
    }
    #[test]
    fn trusted_outcome_precedes_comparison_and_text_cannot_fake_memory_limit() {
        for (kind, verdict) in [
            (ExecutionKind::Exited { code: 1 }, Verdict::RuntimeError),
            (ExecutionKind::MemoryLimit, Verdict::MemoryLimitExceeded),
            (ExecutionKind::TimeLimit, Verdict::TimeLimitExceeded),
            (ExecutionKind::OutputLimit, Verdict::OutputLimitExceeded),
        ] {
            let (m, b, d, mut p) = fixture("java21");
            for o in &mut p.observations {
                o.kind = kind;
                o.stdout = b"OutOfMemoryError".to_vec();
            }
            let r = judge_submission(&mut p, &m, b, d).unwrap();
            assert_eq!(r.report.verdict, verdict);
            assert_eq!(p.expected_reads, 0);
        }
    }
    #[test]
    fn job_deadline_and_cancel_skip_tail_without_fabricated_grades() {
        let (m, b, d, mut p) = fixture("python313");
        p.advance_us = m.job_wall_time_us;
        let r = judge_submission(&mut p, &m, b, d).unwrap();
        assert_eq!(r.report.failure_code, Some(FailureCode::JobDeadline));
        assert_eq!(p.runs, 1);
        assert_eq!(p.expected_reads, 0);
        let (m, b, d, mut p) = fixture("java21");
        p.cancel_after = 1;
        let r = judge_submission(&mut p, &m, b, d).unwrap();
        assert_eq!(r.report.verdict, Verdict::Cancelled);
        assert_eq!(r.skipped.len(), m.cases.len() - 1);
        assert_eq!(p.runs, 1);
    }
}
