//! Ordinary adapter: fixed GNU command profiles, immutable digest verification,
//! bounded point-at-a-time data and injected Engine ports. No database or sudo.
use kf_contracts::judging::*;
use kf_engine::{ExecutionKind, Observation, submission::*};
use kf_sandbox::{
    artifacts::{ArtifactRoot, SnapshotFile, StagePublication},
    factory::FactoryClient,
    outcome::{ExecutionOutcome, LimitKind, Measurement},
    spec::{ExecutionProfile, ExecutionSpec, InputArtifact, ResourceLimits},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};
use std::{
    io::{self, Read},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::Instant,
};
pub(crate) fn read_compile_diagnostics(
    reader: impl Read,
    sandbox_truncated: bool,
) -> io::Result<(String, bool)> {
    let mut raw = Vec::new();
    reader.take(65537).read_to_end(&mut raw)?;
    let mut text = String::from_utf8_lossy(&raw).replace('\0', "�");
    let mut truncated = sandbox_truncated || text.len() > 65536;
    if text.len() > 65536 {
        let mut end = 65536;
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        text.truncate(end);
        truncated = true;
    }
    Ok((text, truncated))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Job {
    pub manifest: JobManifest,
    pub binding: LeaseBinding,
    pub manifest_digest: Digest,
}
#[derive(Serialize)]
pub struct StageEvidence {
    pub run_id: String,
    pub helper_pid: u32,
    pub report: kf_sandbox::outcome::ExecutionReport,
}
#[derive(Serialize)]
pub struct Judged {
    pub result: SubmissionResult,
    pub stages: Vec<StageEvidence>,
}
pub trait ArtifactPort {
    fn read(&mut self, reference: &ArtifactReference) -> io::Result<Vec<u8>>;
}
pub struct LocalArtifacts {
    root: ArtifactRoot,
}
impl LocalArtifacts {
    pub fn open(path: &Path) -> io::Result<Self> {
        Ok(Self {
            root: ArtifactRoot::open(path)?,
        })
    }
}
impl ArtifactPort for LocalArtifacts {
    fn read(&mut self, r: &ArtifactReference) -> io::Result<Vec<u8>> {
        let s = self
            .root
            .snapshot_artifact(r.digest.as_str(), r.bytes.max(1))?;
        if s.bytes() != r.bytes {
            return Err(io::Error::other("artifact byte count"));
        }
        let mut data = Vec::new();
        s.reader()?.take(r.bytes + 1).read_to_end(&mut data)?;
        verify(r, &data)?;
        Ok(data)
    }
}
pub(crate) fn digest(data: &[u8]) -> Digest {
    format!("{:x}", Sha256::digest(data))
        .try_into()
        .expect("SHA256 representation")
}
pub(crate) fn verify(r: &ArtifactReference, data: &[u8]) -> io::Result<()> {
    if data.len() as u64 != r.bytes || digest(data) != r.digest {
        return Err(io::Error::other("artifact digest mismatch"));
    }
    Ok(())
}
fn stage_error(code: FailureCode, cleanup_complete: bool) -> StageFailure {
    StageFailure {
        code,
        cleanup_complete,
    }
}
pub(crate) fn limits(r: &RunLimits) -> ResourceLimits {
    ResourceLimits {
        cpu_time_us: r.cpu_time_us,
        wall_time_us: r.wall_time_us,
        memory_bytes: r.memory_bytes,
        stack_bytes: r.stack_bytes,
        processes: r.processes,
        open_files: r.open_files,
        stdout_bytes: r.stdout_bytes,
        stderr_bytes: r.stderr_bytes,
        file_bytes: r.file_bytes,
        workdir_bytes: r.workdir_bytes,
    }
}
pub(crate) fn metric(m: &Measurement<u64>) -> Option<u64> {
    match m {
        Measurement::Measured { value } => Some(*value),
        Measurement::Unavailable { .. } => None,
    }
}
pub(crate) fn kind(o: ExecutionOutcome) -> ExecutionKind {
    match o {
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
    }
}
pub fn compiler_spec(
    language: Language,
    source: String,
    run_id: String,
    budget: &RunLimits,
) -> ExecutionSpec {
    let profile = match language {
        Language::C17 => ExecutionProfile::CompileC,
        Language::Cpp20 => ExecutionProfile::CompileCpp,
    };
    gnu_compiler_spec(profile, source, run_id, budget).expect("GNU compilation profile")
}

pub(crate) fn gnu_compiler_spec(
    profile: ExecutionProfile,
    source: String,
    run_id: String,
    budget: &RunLimits,
) -> Option<ExecutionSpec> {
    let (argv, name) = kf_sandbox::profiles::gnu_compile_command(profile)?;
    Some(ExecutionSpec {
        schema_version: kf_contracts::SchemaVersion,
        runtime_identity: None,
        run_id,
        profile,
        argv: argv.into_iter().map(String::from).collect(),
        env: Default::default(),
        cwd: ".".into(),
        inputs: vec![InputArtifact {
            artifact_id: source,
            path: name.into(),
        }],
        stdin_artifact: None,
        outputs: vec!["main".into()],
        limits: limits(budget),
    })
}
type StageControl<'a> = Box<dyn FnMut(Stage, Option<u16>) -> Result<(), StageFailure> + 'a>;
pub struct Attempt<'a, A: ArtifactPort> {
    self_test: bool,
    pub artifacts: A,
    pub stages: Vec<StageEvidence>,
    pub cancellation: &'a AtomicBool,
    factory: FactoryClient,
    staging: PathBuf,
    started: Instant,
    deadline: Instant,
    program: Option<(ArtifactReference, SnapshotFile)>,
    control: Option<StageControl<'a>>,
}
impl<'a, A: ArtifactPort> Attempt<'a, A> {
    pub fn new(
        artifacts: A,
        factory_socket: PathBuf,
        staging: PathBuf,
        wall_us: u64,
        cancellation: &'a AtomicBool,
    ) -> io::Result<Self> {
        if wall_us == 0 || wall_us > MAX_JOB_WALL_US {
            return Err(io::Error::other("job deadline"));
        }
        let started = Instant::now();
        let deadline = started
            .checked_add(std::time::Duration::from_micros(wall_us))
            .ok_or_else(|| io::Error::other("deadline overflow"))?;
        Ok(Self {
            self_test: false,
            artifacts,
            stages: vec![],
            cancellation,
            factory: FactoryClient::new(factory_socket),
            staging,
            started,
            deadline,
            program: None,
            control: None,
        })
    }
    pub fn with_control(
        mut self,
        control: impl FnMut(Stage, Option<u16>) -> Result<(), StageFailure> + 'a,
    ) -> Self {
        self.control = Some(Box::new(control));
        self
    }
    pub(crate) fn staging_path(&self) -> &Path {
        &self.staging
    }
    pub(crate) fn execute(
        &mut self,
        spec: ExecutionSpec,
        publication: &mut StagePublication,
    ) -> Result<kf_sandbox::rpc::Executed, StageFailure> {
        let id = spec.run_id.clone();
        let validated = spec
            .clone()
            .validate_for_factory_client()
            .map_err(|_| stage_error(FailureCode::SandboxUnavailable, true))?;
        let result = self.factory.execute_controlled(
            spec,
            publication.root(),
            Some(self.deadline),
            Some(self.cancellation),
        );
        let cleanup = publication.cleanup().is_ok();
        let (result, pid) = result.map_err(|error| {
            if let Some(rejected) = error.get_ref().and_then(|e| e.downcast_ref::<kf_sandbox::rpc::Rejected>()) {
                eprintln!("{}", serde_json::json!({"operation":"sandbox_rejected", "failure":rejected.failure, "cleanup_failure":rejected.cleanup_failure}));
            } else {
                eprintln!("{}", serde_json::json!({"operation":"sandbox_rpc", "kind":format!("{:?}",error.kind())}));
            }
            stage_error(FailureCode::SandboxUnavailable, false)
        })?;
        if !cleanup {
            eprintln!("sandbox stage publication cleanup refused");
        }
        if !cleanup {
            return Err(stage_error(FailureCode::CleanupFailed, false));
        }
        result
            .report
            .classify(&validated)
            .map_err(|_| stage_error(FailureCode::SandboxUnavailable, false))?;
        self.stages.push(StageEvidence {
            run_id: id,
            helper_pid: pid,
            report: result.report.clone(),
        });
        Ok(result)
    }
}
impl<A: ArtifactPort> CompilationPort for Attempt<'_, A> {
    fn compile(&mut self, m: &JobManifest, run_id: &str) -> Result<Compiled, StageFailure> {
        self.self_test = m.self_test;
        if m.toolchain.image_revision != 1 || m.toolchain.toolchain_id.as_str() != "gnu-cxx-v1" {
            return Err(stage_error(FailureCode::SandboxUnavailable, true));
        }
        let source = self
            .artifacts
            .read(&m.source)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        verify(&m.source, &source)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        let mut publication = StagePublication::create(&self.staging, run_id)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        publication
            .put("source", &mut source.as_slice(), source.len() as u64)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, false))?;
        drop(source);
        let spec = compiler_spec(
            m.language,
            "source".into(),
            run_id.into(),
            &m.compile_limits,
        );
        let validated = spec
            .clone()
            .validate(&kf_sandbox::spec::SpecPolicy::default())
            .map_err(|_| stage_error(FailureCode::SandboxUnavailable, true))?;
        let result = self.execute(spec, &mut publication)?;
        let status = match result
            .report
            .classify(&validated)
            .map_err(|_| stage_error(FailureCode::SandboxUnavailable, false))?
        {
            ExecutionOutcome::Exited { code: 0 } => CompilationStatus::Succeeded,
            ExecutionOutcome::Exited { .. } | ExecutionOutcome::Signaled { .. } => {
                CompilationStatus::CompilationError
            }
            ExecutionOutcome::LimitExceeded { .. } => CompilationStatus::CompilationLimit,
            ExecutionOutcome::Cancelled => CompilationStatus::Cancelled,
            _ => CompilationStatus::SystemError,
        };
        let (text, truncated) = read_compile_diagnostics(
            result
                .stderr
                .reader()
                .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?,
            result.report.stderr.truncated,
        )
        .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        let program = if status == CompilationStatus::Succeeded {
            if result.outputs.len() != 1
                || result.outputs[0].path != "main"
                || result.outputs[0].file.bytes() == 0
            {
                return Err(stage_error(FailureCode::ArtifactUnavailable, true));
            }
            let output = result.outputs.into_iter().next().expect("one output").file;
            let mut h = Sha256::new();
            let mut reader = output
                .reader()
                .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
            let mut buffer = [0u8; 16384];
            loop {
                let n = reader
                    .read(&mut buffer)
                    .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
                if n == 0 {
                    break;
                }
                h.update(&buffer[..n]);
            }
            let r = ArtifactReference {
                digest: format!("{:x}", h.finalize()).try_into().expect("SHA256"),
                bytes: output.bytes(),
            };
            self.program = Some((r.clone(), output));
            Some(r)
        } else {
            None
        };
        Ok(Compiled {
            report: CompilationReport {
                status,
                program,
                diagnostics: text.try_into().expect("bounded diagnostics"),
                diagnostics_truncated: truncated,
                cpu_time_us: metric(&result.report.usage.cpu_time_us),
                memory_peak_bytes: metric(&result.report.usage.memory_peak_bytes),
            },
            cleanup_complete: true,
            failure: (status == CompilationStatus::SystemError)
                .then_some(FailureCode::SandboxUnavailable),
        })
    }
}
impl<A: ArtifactPort> RunPort for Attempt<'_, A> {
    fn run(
        &mut self,
        p: &ArtifactReference,
        c: &TestCase,
        b: &RunLimits,
        run_id: &str,
    ) -> Result<Observation, StageFailure> {
        let input = self
            .artifacts
            .read(&c.input)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        verify(&c.input, &input)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        let Some((reference, program)) = &self.program else {
            return Err(stage_error(FailureCode::ArtifactUnavailable, true));
        };
        if reference != p {
            return Err(stage_error(FailureCode::ArtifactUnavailable, true));
        }
        let mut publication = StagePublication::create(&self.staging, run_id)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        publication
            .put(
                "program",
                &mut program
                    .reader()
                    .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?,
                p.bytes,
            )
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, false))?;
        publication
            .put("stdin", &mut input.as_slice(), input.len() as u64)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, false))?;
        drop(input);
        let spec = ExecutionSpec {
            schema_version: kf_contracts::SchemaVersion,
            runtime_identity: None,
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
            limits: limits(b),
        };
        let validated = spec
            .clone()
            .validate(&kf_sandbox::spec::SpecPolicy::default())
            .map_err(|_| stage_error(FailureCode::SandboxUnavailable, true))?;
        let result = self.execute(spec, &mut publication)?;
        let outcome = result
            .report
            .classify(&validated)
            .map_err(|_| stage_error(FailureCode::SandboxUnavailable, false))?;
        let mut stdout = Vec::new();
        result
            .stdout
            .reader()
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?
            .take(b.stdout_bytes + 1)
            .read_to_end(&mut stdout)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        if stdout.len() as u64 > b.stdout_bytes {
            return Err(stage_error(FailureCode::ArtifactUnavailable, true));
        }
        let mut stderr = Vec::new();
        if self.self_test {
            result
                .stderr
                .reader()
                .map_err(|_e| stage_error(FailureCode::ArtifactUnavailable, true))?
                .take(16385)
                .read_to_end(&mut stderr)
                .map_err(|_e| stage_error(FailureCode::ArtifactUnavailable, true))?;
        }
        Ok(Observation {
            stderr,
            stdout_truncated: self.self_test && result.report.stdout.truncated,
            stderr_truncated: self.self_test && result.report.stderr.truncated,
            kind: kind(outcome),
            stdout,
            cpu_time_us: metric(&result.report.usage.cpu_time_us),
            memory_peak_bytes: metric(&result.report.usage.memory_peak_bytes),
        })
    }
}
impl<A: ArtifactPort> ExpectedPort for Attempt<'_, A> {
    fn expected(&mut self, c: &TestCase) -> Result<Vec<u8>, StageFailure> {
        let data = self
            .artifacts
            .read(&c.expected)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        verify(&c.expected, &data)
            .map_err(|_| stage_error(FailureCode::ArtifactUnavailable, true))?;
        Ok(data)
    }
}
impl<A: ArtifactPort> ControlPort for Attempt<'_, A> {
    fn elapsed_us(&self) -> u64 {
        self.started
            .elapsed()
            .as_micros()
            .try_into()
            .unwrap_or(u64::MAX)
    }
    fn cancelled(&self) -> bool {
        self.cancellation.load(Ordering::Acquire)
    }
    fn stage(&mut self, stage: Stage, index: Option<u16>) -> Result<(), StageFailure> {
        if let Some(control) = self.control.as_mut() {
            control(stage, index)
        } else {
            Ok(())
        }
    }
}
pub fn judge_local(
    socket: &Path,
    job: &Path,
    artifacts: &Path,
    staging: &Path,
) -> io::Result<Judged> {
    let job: Job = serde_json::from_slice(&kf_sandbox::execution::read_bounded_file(job, 262144)?)
        .map_err(|_| io::Error::other("invalid submission"))?;
    let canonical =
        manifest_bytes(&job.manifest).map_err(|_| io::Error::other("invalid manifest"))?;
    if digest(&canonical) != job.manifest_digest {
        return Err(io::Error::other("manifest digest mismatch"));
    }
    StagePublication::recover(staging)?;
    let cancel = AtomicBool::new(false);
    let mut adapter = Attempt::new(
        LocalArtifacts::open(artifacts)?,
        socket.into(),
        staging.into(),
        job.manifest.job_wall_time_us,
        &cancel,
    )?;
    let result = judge_submission(
        &mut adapter,
        &job.manifest,
        job.binding,
        job.manifest_digest,
    )
    .map_err(io::Error::other)?;
    Ok(Judged {
        result,
        stages: adapter.stages,
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn compile_diagnostics_preserve_limit_and_report_truncation() {
        for size in [16385, 20000, 65536, 65537, 70000] {
            let raw = vec![b'x'; size];
            let (text, truncated) = read_compile_diagnostics(raw.as_slice(), false).unwrap();
            assert_eq!(text.len(), size.min(65536));
            assert_eq!(truncated, size > 65536);
        }
        assert!(
            read_compile_diagnostics(b"short".as_slice(), true)
                .unwrap()
                .1
        );
        let raw = "界".repeat(22000);
        let (text, truncated) = read_compile_diagnostics(raw.as_bytes(), false).unwrap();
        assert!(truncated);
        assert_eq!(text.len(), 65535);
        assert_eq!(
            read_compile_diagnostics(b"error\0".as_slice(), false).unwrap(),
            ("error�".to_owned(), false)
        );
    }

    #[test]
    fn constructing_parallel_attempt_does_not_scan_shared_staging() {
        struct NoArtifacts;
        impl ArtifactPort for NoArtifacts {
            fn read(&mut self, _: &ArtifactReference) -> io::Result<Vec<u8>> {
                unreachable!("construction must not read artifacts")
            }
        }
        let staging = std::env::temp_dir().join(format!("kf-staging-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&staging).unwrap();
        // Another slot may currently be creating its publication, before .owner exists.
        std::fs::create_dir(staging.join("other-slot")).unwrap();
        let cancel = AtomicBool::new(false);
        assert!(
            Attempt::new(
                NoArtifacts,
                "/unused.sock".into(),
                staging.clone(),
                1000,
                &cancel
            )
            .is_ok()
        );
        std::fs::remove_dir_all(staging).unwrap();
    }
    #[test]
    fn fixed_commands_and_source_digest() {
        let m: JobManifest = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/contracts/judge-manifest.valid.json"
        ))
        .unwrap();
        for (lang, tool, std) in [
            (Language::C17, "/usr/bin/gcc", "-std=c17"),
            (Language::Cpp20, "/usr/bin/g++", "-std=c++20"),
        ] {
            let s = compiler_spec(lang, "source".into(), "compile".into(), &m.compile_limits);
            assert_eq!(s.argv[0], tool);
            assert_eq!(s.argv[1], std);
            assert_eq!(s.outputs, ["main"]);
            assert!(s.validate(&kf_sandbox::spec::SpecPolicy::default()).is_ok());
        }
        let r = ArtifactReference {
            digest: digest(b"a"),
            bytes: 1,
        };
        assert!(verify(&r, b"b").is_err());
        assert!(verify(&r, b"a").is_ok());
    }
}
