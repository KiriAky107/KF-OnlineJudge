//! Deterministic comparison and aggregation. The injected port owns execution;
//! this crate has no OS, sandbox, privilege or storage dependency.
use serde::{Deserialize, Serialize};
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CaseRequest {
    pub run_id: String,
    pub program_artifact: String,
    pub stdin_artifact: Option<String>,
}
pub struct Case {
    pub request: CaseRequest,
    pub expected: Vec<u8>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ExecutionKind {
    Exited { code: u8 },
    Signaled,
    TimeLimit,
    MemoryLimit,
    OutputLimit,
    OtherLimit,
    Cancelled,
    InfrastructureFailure,
}
pub struct Observation {
    pub kind: ExecutionKind,
    pub stdout: Vec<u8>,
    pub cpu_time_us: Option<u64>,
    pub memory_peak_bytes: Option<u64>,
}
pub trait ExecutionPort {
    fn execute(&mut self, request: &CaseRequest) -> Result<Observation, PortError>;
}
#[derive(Debug)]
pub struct PortError;
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Accepted,
    WrongAnswer,
    RuntimeError,
    TimeLimitExceeded,
    MemoryLimitExceeded,
    OutputLimitExceeded,
    Cancelled,
    SystemError,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct CaseResult {
    pub run_id: String,
    pub verdict: Verdict,
    pub cpu_time_us: Option<u64>,
    pub memory_peak_bytes: Option<u64>,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Judgement {
    pub verdict: Verdict,
    pub cases: Vec<CaseResult>,
}
pub fn judge(port: &mut impl ExecutionPort, cases: &[Case]) -> Result<Judgement, PortError> {
    if cases.is_empty() || cases.len() > 256 {
        return Err(PortError);
    }
    let mut seen = std::collections::HashSet::new();
    if cases.iter().any(|c| !seen.insert(&c.request.run_id)) {
        return Err(PortError);
    }
    let mut results = Vec::with_capacity(cases.len());
    for c in cases {
        let result = match port.execute(&c.request) {
            Err(_) => CaseResult {
                run_id: c.request.run_id.clone(),
                verdict: Verdict::SystemError,
                cpu_time_us: None,
                memory_peak_bytes: None,
            },
            Ok(o) => {
                let verdict = match o.kind {
                    ExecutionKind::Exited { code: 0 } => {
                        if o.stdout == c.expected {
                            Verdict::Accepted
                        } else {
                            Verdict::WrongAnswer
                        }
                    }
                    ExecutionKind::Exited { .. }
                    | ExecutionKind::Signaled
                    | ExecutionKind::OtherLimit => Verdict::RuntimeError,
                    ExecutionKind::TimeLimit => Verdict::TimeLimitExceeded,
                    ExecutionKind::MemoryLimit => Verdict::MemoryLimitExceeded,
                    ExecutionKind::OutputLimit => Verdict::OutputLimitExceeded,
                    ExecutionKind::Cancelled => Verdict::Cancelled,
                    ExecutionKind::InfrastructureFailure => Verdict::SystemError,
                };
                CaseResult {
                    run_id: c.request.run_id.clone(),
                    verdict,
                    cpu_time_us: o.cpu_time_us,
                    memory_peak_bytes: o.memory_peak_bytes,
                }
            }
        };
        let stop = matches!(result.verdict, Verdict::SystemError | Verdict::Cancelled);
        results.push(result);
        if stop {
            break;
        }
    }
    let verdict = results
        .iter()
        .find(|r| matches!(r.verdict, Verdict::SystemError | Verdict::Cancelled))
        .or_else(|| results.iter().find(|r| r.verdict != Verdict::Accepted))
        .map_or(Verdict::Accepted, |r| r.verdict);
    Ok(Judgement {
        verdict,
        cases: results,
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    struct Port {
        kinds: std::collections::VecDeque<Result<ExecutionKind, PortError>>,
        calls: usize,
    }
    impl ExecutionPort for Port {
        fn execute(&mut self, _: &CaseRequest) -> Result<Observation, PortError> {
            self.calls += 1;
            Ok(Observation {
                kind: self.kinds.pop_front().unwrap()?,
                stdout: vec![0, 255, 10],
                cpu_time_us: Some(42),
                memory_peak_bytes: None,
            })
        }
    }
    fn case(id: &str, expected: Vec<u8>) -> Case {
        Case {
            request: CaseRequest {
                run_id: id.into(),
                program_artifact: "program".into(),
                stdin_artifact: None,
            },
            expected,
        }
    }
    #[test]
    fn exact_binary_comparison_and_nonzero_precedence() {
        let mut p = Port {
            kinds: [
                Ok(ExecutionKind::Exited { code: 0 }),
                Ok(ExecutionKind::Exited { code: 0 }),
                Ok(ExecutionKind::Exited { code: 7 }),
            ]
            .into(),
            calls: 0,
        };
        let j = judge(
            &mut p,
            &[
                case("a", vec![0, 255, 10]),
                case("b", vec![0, 255]),
                case("c", vec![0, 255, 10]),
            ],
        )
        .unwrap();
        assert_eq!(j.verdict, Verdict::WrongAnswer);
        assert_eq!(
            j.cases.iter().map(|c| c.verdict).collect::<Vec<_>>(),
            vec![
                Verdict::Accepted,
                Verdict::WrongAnswer,
                Verdict::RuntimeError
            ]
        );
        assert_eq!(j.cases[0].cpu_time_us, Some(42));
        assert_eq!(j.cases[0].memory_peak_bytes, None);
    }
    #[test]
    fn every_execution_failure_has_a_verdict() {
        for (k, v) in [
            (ExecutionKind::TimeLimit, Verdict::TimeLimitExceeded),
            (ExecutionKind::MemoryLimit, Verdict::MemoryLimitExceeded),
            (ExecutionKind::OutputLimit, Verdict::OutputLimitExceeded),
            (ExecutionKind::OtherLimit, Verdict::RuntimeError),
            (ExecutionKind::Signaled, Verdict::RuntimeError),
            (ExecutionKind::Cancelled, Verdict::Cancelled),
            (ExecutionKind::InfrastructureFailure, Verdict::SystemError),
        ] {
            let mut p = Port {
                kinds: [Ok(k)].into(),
                calls: 0,
            };
            assert_eq!(judge(&mut p, &[case("a", vec![])]).unwrap().verdict, v);
        }
    }
    #[test]
    fn infrastructure_failure_stops_and_overrides_prior_answer() {
        let mut p = Port {
            kinds: [
                Ok(ExecutionKind::Exited { code: 0 }),
                Err(PortError),
                Ok(ExecutionKind::Exited { code: 0 }),
            ]
            .into(),
            calls: 0,
        };
        let j = judge(
            &mut p,
            &[case("a", vec![]), case("b", vec![]), case("c", vec![])],
        )
        .unwrap();
        assert_eq!(j.verdict, Verdict::SystemError);
        assert_eq!(p.calls, 2);
        assert_eq!(j.cases[1].cpu_time_us, None);
    }
    #[test]
    fn empty_or_reused_run_ids_do_not_execute() {
        let mut p = Port {
            kinds: [].into(),
            calls: 0,
        };
        assert!(judge(&mut p, &[]).is_err());
        assert!(judge(&mut p, &[case("a", vec![]), case("a", vec![])]).is_err());
        assert_eq!(p.calls, 0);
    }
}

pub mod submission;
pub mod submission_v2;
