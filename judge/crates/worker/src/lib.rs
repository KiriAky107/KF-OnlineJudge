//! Ordinary Worker adapter. Privileged execution and artifact capabilities stay
//! in sandbox; comparison and the final summary stay in Engine.
#[cfg(target_os = "linux")]
pub mod native {
    use kf_engine::{Case, CaseRequest, ExecutionKind, ExecutionPort, Observation, PortError};
    use kf_sandbox::{
        artifacts::ArtifactRoot,
        outcome::{ExecutionOutcome, LimitKind, Measurement},
        spec::{ExecutionProfile, ExecutionSpec, InputArtifact, ResourceLimits},
    };
    use serde::Deserialize;
    use std::{
        io::{self, Read},
        path::{Path, PathBuf},
    };
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    pub struct Job {
        pub request: CaseRequest,
        pub expected_artifact: String,
    }
    pub struct SandboxPort {
        socket: PathBuf,
        artifacts: ArtifactRoot,
    }
    impl ExecutionPort for SandboxPort {
        fn execute(&mut self, request: &CaseRequest) -> Result<Observation, PortError> {
            let spec = ExecutionSpec {
                schema_version: kf_contracts::SchemaVersion,
                runtime_identity: None,
                run_id: request.run_id.clone(),
                profile: ExecutionProfile::Native,
                argv: vec!["/work/main".into()],
                env: std::collections::BTreeMap::new(),
                cwd: ".".into(),
                inputs: vec![InputArtifact {
                    artifact_id: request.program_artifact.clone(),
                    path: "main".into(),
                }],
                stdin_artifact: request.stdin_artifact.clone(),
                outputs: vec![],
                limits: ResourceLimits::for_profile(ExecutionProfile::Native),
            };
            let validated = spec
                .clone()
                .validate(&kf_sandbox::spec::SpecPolicy::default())
                .map_err(|_| PortError)?;
            let result = kf_sandbox::rpc::execute(&self.socket, spec, &self.artifacts)
                .map_err(|_| PortError)?;
            let outcome = result.report.classify(&validated).map_err(|_| PortError)?;
            let kind = match outcome {
                ExecutionOutcome::Exited { code } => ExecutionKind::Exited { code },
                ExecutionOutcome::Signaled { .. } => ExecutionKind::Signaled,
                ExecutionOutcome::LimitExceeded {
                    primary: LimitKind::Memory,
                } => ExecutionKind::MemoryLimit,
                ExecutionOutcome::LimitExceeded {
                    primary: LimitKind::CpuTime | LimitKind::WallTime,
                } => ExecutionKind::TimeLimit,
                ExecutionOutcome::LimitExceeded {
                    primary: LimitKind::Stdout | LimitKind::Stderr,
                } => ExecutionKind::OutputLimit,
                ExecutionOutcome::LimitExceeded { .. } => ExecutionKind::OtherLimit,
                ExecutionOutcome::Cancelled => ExecutionKind::Cancelled,
                ExecutionOutcome::InfrastructureFailure | ExecutionOutcome::NotStarted => {
                    ExecutionKind::InfrastructureFailure
                }
            };
            let mut stdout = Vec::new();
            result
                .stdout
                .reader()
                .map_err(|_| PortError)?
                .read_to_end(&mut stdout)
                .map_err(|_| PortError)?;
            let value = |m: Measurement<u64>| match m {
                Measurement::Measured { value } => Some(value),
                Measurement::Unavailable { .. } => None,
            };
            Ok(Observation {
                kind,
                stdout,
                cpu_time_us: value(result.report.usage.cpu_time_us),
                memory_peak_bytes: value(result.report.usage.memory_peak_bytes),
            })
        }
    }
    pub fn judge_once(
        socket: &Path,
        job_path: &Path,
        artifact_path: &Path,
    ) -> io::Result<kf_engine::Judgement> {
        let data =
            kf_sandbox::execution::read_bounded_file(job_path, kf_sandbox::spec::MAX_SPEC_BYTES)?;
        let job: Job =
            serde_json::from_slice(&data).map_err(|_| io::Error::other("invalid native job"))?;
        let artifacts = ArtifactRoot::open(artifact_path)?;
        let expected = artifacts.snapshot_artifact(
            &job.expected_artifact,
            ResourceLimits::for_profile(ExecutionProfile::Native).stdout_bytes,
        )?;
        let mut bytes = Vec::new();
        expected.reader()?.read_to_end(&mut bytes)?;
        let case = Case {
            request: job.request,
            expected: bytes,
        };
        let mut port = SandboxPort {
            socket: socket.to_owned(),
            artifacts,
        };
        kf_engine::judge(&mut port, &[case]).map_err(|_| io::Error::other("invalid judge request"))
    }
}

#[cfg(target_os = "linux")]
pub mod pipeline;

#[cfg(target_os = "linux")]
pub mod consumer;
