//! One immutable submission, one compilation, bounded sequential test points.
//! Ports own clocks/cancellation/storage/execution; this module is pure domain.
use crate::{ExecutionKind, Observation};
use kf_contracts::{SchemaVersion, judging::*};
use serde::Serialize;
#[derive(Debug)]
pub struct StageFailure {
    pub code: FailureCode,
    pub cleanup_complete: bool,
}
pub struct Compiled {
    pub report: CompilationReport,
    pub cleanup_complete: bool,
    pub failure: Option<FailureCode>,
}
pub trait CompilationPort {
    fn compile(&mut self, manifest: &JobManifest, run_id: &str) -> Result<Compiled, StageFailure>;
}
pub trait RunPort {
    fn run(
        &mut self,
        program: &ArtifactReference,
        case: &TestCase,
        limits: &RunLimits,
        run_id: &str,
    ) -> Result<Observation, StageFailure>;
}
pub trait ExpectedPort {
    fn expected(&mut self, case: &TestCase) -> Result<Vec<u8>, StageFailure>;
}
pub trait ControlPort {
    fn elapsed_us(&self) -> u64;
    fn cancelled(&self) -> bool;
    fn stage(&mut self, stage: Stage, case_index: Option<u16>) -> Result<(), StageFailure>;
}
#[derive(Debug, Serialize)]
pub struct SkippedCase {
    pub case_id: Identifier,
    pub reason: SkipReason,
}
#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SkipReason {
    CompilationFailed,
    Cancelled,
    InfrastructureFailure,
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
fn classify(o: &Observation, expected: &[u8]) -> Verdict {
    match o.kind {
        ExecutionKind::Exited { code: 0 } => {
            if o.stdout == expected {
                Verdict::Accepted
            } else {
                Verdict::WrongAnswer
            }
        }
        ExecutionKind::Exited { .. } | ExecutionKind::Signaled | ExecutionKind::OtherLimit => {
            Verdict::RuntimeError
        }
        ExecutionKind::TimeLimit => Verdict::TimeLimitExceeded,
        ExecutionKind::MemoryLimit => Verdict::MemoryLimitExceeded,
        ExecutionKind::OutputLimit => Verdict::OutputLimitExceeded,
        ExecutionKind::Cancelled => Verdict::Cancelled,
        ExecutionKind::InfrastructureFailure => Verdict::SystemError,
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
pub fn judge_submission<P: CompilationPort + RunPort + ExpectedPort + ControlPort>(
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
            .and_then(|()| p.compile(m, &compile_run_id)),
    };
    let mut cleanup_complete;
    let mut failure_code;
    let compilation = match compilation {
        Ok(c) => {
            c.report.validate()?;
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
                    let verdict = if matches!(o.kind, ExecutionKind::Exited { code: 0 }) {
                        match gate(p, m) {
                            Err(e) => {
                                failure_code = Some(e.code);
                                Verdict::SystemError
                            }
                            Ok(true) => Verdict::Cancelled,
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
                    } else {
                        classify(&o, &[])
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
        observed: Vec<Observation>,
        calls: usize,
        compile_calls: usize,
        expected_calls: usize,
        cancel_after: usize,
        clock: u64,
        fail_compile: bool,
    }
    impl CompilationPort for Port {
        fn compile(&mut self, _: &JobManifest, _: &str) -> Result<Compiled, StageFailure> {
            self.compile_calls += 1;
            Ok(Compiled {
                report: CompilationReport {
                    status: if self.fail_compile {
                        CompilationStatus::CompilationError
                    } else {
                        CompilationStatus::Succeeded
                    },
                    program: (!self.fail_compile).then(|| ArtifactReference {
                        digest: "a".repeat(64).try_into().unwrap(),
                        bytes: 1,
                    }),
                    diagnostics: String::new().try_into().unwrap(),
                    diagnostics_truncated: false,
                    cpu_time_us: Some(5),
                    memory_peak_bytes: Some(10),
                },
                cleanup_complete: true,
                failure: None,
            })
        }
    }
    impl RunPort for Port {
        fn run(
            &mut self,
            _: &ArtifactReference,
            _: &TestCase,
            _: &RunLimits,
            _: &str,
        ) -> Result<Observation, StageFailure> {
            self.calls += 1;
            Ok(self.observed.remove(0))
        }
    }
    impl ExpectedPort for Port {
        fn expected(&mut self, c: &TestCase) -> Result<Vec<u8>, StageFailure> {
            self.expected_calls += 1;
            Ok(vec![0; c.expected.bytes as usize])
        }
    }
    impl ControlPort for Port {
        fn elapsed_us(&self) -> u64 {
            self.clock
        }
        fn cancelled(&self) -> bool {
            self.calls >= self.cancel_after
        }
        fn stage(&mut self, _: Stage, _: Option<u16>) -> Result<(), StageFailure> {
            Ok(())
        }
    }
    fn fixture() -> (JobManifest, LeaseBinding, Digest) {
        let m: JobManifest = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/contracts/judge-manifest.valid.json"
        ))
        .unwrap();
        let mut value: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/contracts/judge-result.valid.json"
        ))
        .unwrap();
        let b: LeaseBinding = serde_json::from_value(value["binding"].take()).unwrap();
        (m, b, "b".repeat(64).try_into().unwrap())
    }
    fn port(kinds: Vec<ExecutionKind>) -> Port {
        Port {
            observed: kinds
                .into_iter()
                .map(|kind| Observation {
                    kind,
                    stdout: vec![],
                    cpu_time_us: Some(3),
                    memory_peak_bytes: None,
                })
                .collect(),
            calls: 0,
            compile_calls: 0,
            expected_calls: 0,
            cancel_after: usize::MAX,
            clock: 0,
            fail_compile: false,
        }
    }
    #[test]
    fn compile_once_order_exact_bytes_and_measurements() {
        let (mut m, b, d) = fixture();
        for c in &mut m.cases {
            c.expected.bytes = 0;
        }
        let mut p = port(vec![ExecutionKind::Exited { code: 0 }; m.cases.len()]);
        let r = judge_submission(&mut p, &m, b, d).unwrap();
        assert_eq!(p.compile_calls, 1);
        assert_eq!(p.calls, m.cases.len());
        assert_eq!(r.report.verdict, Verdict::Accepted);
        assert_eq!(r.report.run_cpu_time_us, Some(3 * m.cases.len() as u64));
        assert_eq!(r.report.run_memory_peak_bytes, None);
        assert!(r.skipped.is_empty());
        assert_eq!(r.report.compilation.cpu_time_us, Some(5));
    }
    #[test]
    fn cancellation_and_ce_skip_without_faking_cases() {
        let (m, b, d) = fixture();
        let mut p = port(vec![]);
        p.fail_compile = true;
        let r = judge_submission(&mut p, &m, b.clone(), d.clone()).unwrap();
        assert_eq!(r.report.verdict, Verdict::CompilationError);
        assert_eq!(r.skipped.len(), m.cases.len());
        assert_eq!(p.calls, 0);
        p.fail_compile = false;
        p.cancel_after = 0;
        let r = judge_submission(&mut p, &m, b, d).unwrap();
        assert_eq!(r.report.verdict, Verdict::Cancelled);
        assert_eq!(p.compile_calls, 1);
    }
    #[test]
    fn nonzero_does_not_read_or_compare_expected() {
        let (m, b, d) = fixture();
        let mut p = port(vec![ExecutionKind::Exited { code: 7 }; m.cases.len()]);
        let r = judge_submission(&mut p, &m, b, d).unwrap();
        assert_eq!(r.report.verdict, Verdict::RuntimeError);
        assert_eq!(p.expected_calls, 0);
    }
    #[test]
    fn infrastructure_and_deadline_override_prior_wrong_answer() {
        let (m, b, d) = fixture();
        let mut p = port(vec![ExecutionKind::InfrastructureFailure]);
        let r = judge_submission(&mut p, &m, b.clone(), d.clone()).unwrap();
        assert_eq!(p.calls, 1);
        assert_eq!(r.report.failure_code, Some(FailureCode::SandboxUnavailable));
        p.clock = m.job_wall_time_us;
        let r = judge_submission(&mut p, &m, b, d).unwrap();
        assert_eq!(r.report.failure_code, Some(FailureCode::JobDeadline));
        assert!(r.report.cases.is_empty());
    }
    #[test]
    fn byte_boundaries_and_nonzero_precedence() {
        for (out, expected, v) in [
            (vec![], vec![], Verdict::Accepted),
            (vec![0, 255, 10], vec![0, 255, 10], Verdict::Accepted),
            (vec![0, 255, 10], vec![0, 255], Verdict::WrongAnswer),
            (vec![10], vec![], Verdict::WrongAnswer),
        ] {
            let o = Observation {
                kind: ExecutionKind::Exited { code: 0 },
                stdout: out,
                cpu_time_us: None,
                memory_peak_bytes: None,
            };
            assert_eq!(classify(&o, &expected), v);
        }
    }
    #[test]
    fn fatal_precedence_missing_metrics_and_midpoint_cancel() {
        let (mut m, b, d) = fixture();
        for c in &mut m.cases {
            c.expected.bytes = 0;
        }
        let mut p = port(vec![
            ExecutionKind::Exited { code: 0 },
            ExecutionKind::InfrastructureFailure,
        ]);
        p.observed[0].stdout = vec![1];
        let r = judge_submission(&mut p, &m, b.clone(), d.clone()).unwrap();
        assert_eq!(r.report.cases[0].verdict, Verdict::WrongAnswer);
        assert_eq!(r.report.verdict, Verdict::SystemError);
        assert_eq!(r.skipped.len(), 1);
        let mut p = port(vec![ExecutionKind::Exited { code: 0 }; 3]);
        p.cancel_after = 1;
        let r = judge_submission(&mut p, &m, b.clone(), d.clone()).unwrap();
        assert_eq!(p.calls, 1);
        assert_eq!(r.report.verdict, Verdict::Cancelled);
        assert_eq!(r.skipped.len(), 2);
        let mut p = port(vec![ExecutionKind::Exited { code: 0 }; 3]);
        p.observed[1].cpu_time_us = None;
        p.observed[0].memory_peak_bytes = Some(100);
        let r = judge_submission(&mut p, &m, b, d).unwrap();
        assert_eq!(r.report.verdict, Verdict::Accepted);
        assert_eq!(r.report.run_cpu_time_us, None);
        assert_eq!(r.report.run_memory_peak_bytes, None);
    }
    #[test]
    fn maximum_point_count_is_bounded_and_run_ids_are_unique() {
        let (mut m, b, d) = fixture();
        m.cases = (0..MAX_CASES)
            .map(|i| TestCase {
                case_id: format!("aaaaaaaa-aaaa-4aaa-8aaa-{:012}", 1000 + i)
                    .try_into()
                    .unwrap(),
                input: m.cases[0].input.clone(),
                expected: ArtifactReference {
                    digest: m.cases[0].expected.digest.clone(),
                    bytes: 0,
                },
            })
            .collect();
        m.job_wall_time_us = 900_000_000;
        let mut p = port(vec![ExecutionKind::Exited { code: 0 }; MAX_CASES]);
        let r = judge_submission(&mut p, &m, b.clone(), d.clone()).unwrap();
        assert_eq!(p.compile_calls, 1);
        assert_eq!(p.calls, MAX_CASES);
        assert_eq!(
            r.case_run_ids
                .iter()
                .collect::<std::collections::HashSet<_>>()
                .len(),
            MAX_CASES
        );
        m.cases.push(TestCase {
            case_id: "aaaaaaaa-aaaa-4aaa-8aaa-000000002000"
                .to_string()
                .try_into()
                .unwrap(),
            input: m.cases[0].input.clone(),
            expected: m.cases[0].expected.clone(),
        });
        assert!(judge_submission(&mut p, &m, b, d).is_err());
        assert_eq!(p.compile_calls, 1);
    }
}
