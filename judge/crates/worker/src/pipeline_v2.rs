//! Ordinary multi-runtime adapter. User source is only checked/compiled/run by
//! isolated Root helpers; Worker handles bounded bytes and sealed capabilities.
use crate::{
    pipeline::{ArtifactPort, Attempt, LocalArtifacts, StageEvidence, kind, metric, verify},
    runtime_specs::AuthorizedJob,
};
use kf_contracts::{judging as v1, judging_v2 as v2, judging_versions::NodeAdmission};
use kf_engine::{Observation, submission as old, submission_v2 as engine};
use kf_sandbox::{
    artifacts::{SnapshotFile, StagePublication},
    outcome::ExecutionOutcome,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest as _, Sha256};
use std::{
    io::{self, Read},
    path::{Path, PathBuf},
    sync::atomic::AtomicBool,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Job {
    pub manifest: v2::JobManifest,
    pub binding: v1::LeaseBinding,
    pub manifest_digest: v1::Digest,
    pub attempt_number: u32,
}
#[derive(Serialize)]
pub struct Judged {
    pub result: engine::SubmissionResult,
    pub stages: Vec<StageEvidence>,
}
struct SealedProgram {
    metadata: v2::CompiledProgram,
    files: Vec<SnapshotFile>,
}
pub struct RuntimeAttempt<'a, A: ArtifactPort> {
    authorized: AuthorizedJob,
    io: Attempt<'a, A>,
    program: Option<SealedProgram>,
}
pub(crate) fn failure(code: v1::FailureCode, cleanup_complete: bool) -> old::StageFailure {
    old::StageFailure {
        code,
        cleanup_complete,
    }
}
pub(crate) fn artifact_error(_: io::Error) -> old::StageFailure {
    failure(v1::FailureCode::ArtifactUnavailable, true)
}
pub(crate) fn with_publication<T>(
    mut publication: StagePublication,
    operation: impl FnOnce(&mut StagePublication) -> Result<T, old::StageFailure>,
) -> Result<T, old::StageFailure> {
    let result = operation(&mut publication);
    // Early errors also need a positive staging cleanup proof. Drop remains a
    // fallback, never evidence that permits cleanup_complete=true in a report.
    if publication.cleanup().is_err() {
        return Err(failure(v1::FailureCode::CleanupFailed, false));
    }
    result
}
pub(crate) fn snapshot_reference(
    file: &SnapshotFile,
) -> Result<v1::ArtifactReference, old::StageFailure> {
    let mut h = Sha256::new();
    let mut reader = file.reader().map_err(artifact_error)?;
    let mut buffer = [0u8; 16384];
    let mut total = 0u64;
    loop {
        let n = reader.read(&mut buffer).map_err(artifact_error)?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > file.bytes() {
            return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
        }
        h.update(&buffer[..n]);
    }
    if total != file.bytes() {
        return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
    }
    Ok(v1::ArtifactReference {
        digest: format!("{:x}", h.finalize()).try_into().expect("SHA256"),
        bytes: total,
    })
}
fn diagnostics(result: &kf_sandbox::rpc::Executed) -> Result<(String, bool), old::StageFailure> {
    crate::pipeline::read_compile_diagnostics(
        result.stderr.reader().map_err(artifact_error)?,
        result.report.stderr.truncated,
    )
    .map_err(artifact_error)
}
impl<'a, A: ArtifactPort> RuntimeAttempt<'a, A> {
    pub fn new(
        authorized: AuthorizedJob,
        artifacts: A,
        socket: PathBuf,
        staging: PathBuf,
        cancellation: &'a AtomicBool,
    ) -> io::Result<Self> {
        let io = Attempt::new(
            artifacts,
            socket,
            staging,
            authorized.manifest().job_wall_time_us,
            cancellation,
        )?;
        Ok(Self {
            authorized,
            io,
            program: None,
        })
    }
    pub fn with_control(
        mut self,
        control: impl FnMut(v1::Stage, Option<u16>) -> Result<(), old::StageFailure> + 'a,
    ) -> Self {
        self.io = self.io.with_control(control);
        self
    }
    pub fn into_stages(self) -> Vec<StageEvidence> {
        self.io.stages
    }
}
impl<A: ArtifactPort> engine::PreparationPort for RuntimeAttempt<'_, A> {
    fn prepare(
        &mut self,
        m: &v2::JobManifest,
        binding: &v1::LeaseBinding,
        run_id: &str,
    ) -> Result<engine::Compiled, old::StageFailure> {
        if m != self.authorized.manifest()
            || binding != self.authorized.binding()
            || self.program.is_some()
        {
            return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
        }
        let source = self.io.artifacts.read(&m.source).map_err(artifact_error)?;
        verify(&m.source, &source).map_err(artifact_error)?;
        let publication = StagePublication::create(self.io.staging_path(), run_id)
            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, false))?;
        with_publication(publication, |publication| {
            publication
                .put("source", &mut source.as_slice(), m.source.bytes)
                .map_err(artifact_error)?;
            let checked_source = if m.language == v2::Language::Python313 {
                let file = publication
                    .root()
                    .snapshot_artifact("source", m.source.bytes)
                    .map_err(artifact_error)?;
                if snapshot_reference(&file)? != m.source {
                    return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
                }
                Some(file)
            } else {
                None
            };
            drop(source);
            let spec = self
                .authorized
                .preparation_spec(run_id.into())
                .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, true))?;
            let validated = spec
                .clone()
                .validate(&kf_sandbox::spec::SpecPolicy::default())
                .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, true))?;
            let result = self.io.execute(spec, publication)?;
            let mut status = match result
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
            let (mut text, truncated) = diagnostics(&result)?;
            let cpu_time_us = metric(&result.report.usage.cpu_time_us);
            let memory_peak_bytes = metric(&result.report.usage.memory_peak_bytes);
            let mut files = Vec::new();
            let mut sealed = Vec::new();
            if status == v1::CompilationStatus::Succeeded {
                if let Some(source) = checked_source {
                    if !result.outputs.is_empty() {
                        return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
                    }
                    files.push(v2::ProgramFile {
                        path: "main.py".into(),
                        artifact: m.source.clone(),
                    });
                    sealed.push(source);
                } else {
                    for output in result.outputs {
                        files.push(v2::ProgramFile {
                            path: output.path,
                            artifact: snapshot_reference(&output.file)?,
                        });
                        sealed.push(output.file);
                    }
                }
                if m.language == v2::Language::Java21 {
                    // No reflective loading or initialization outside the JVM. A
                    // successfully compiled unsupported entry is a user CE.
                    let mut metadata = Vec::with_capacity(files.len());
                    for (info, file) in files.iter().zip(&sealed) {
                        let mut bytes = Vec::new();
                        file.reader()
                            .map_err(artifact_error)?
                            .take(info.artifact.bytes + 1)
                            .read_to_end(&mut bytes)
                            .map_err(artifact_error)?;
                        verify(&info.artifact, &bytes).map_err(artifact_error)?;
                        metadata.push((info.path.clone(), bytes));
                    }
                    let has_main = if metadata.is_empty() {
                        false
                    } else {
                        crate::java_entry::has_main(&metadata)
                            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, true))?
                    };
                    if !has_main {
                        status = v1::CompilationStatus::CompilationError;
                        text="Java submission requires default-package Main with public static void main(String[]) and at most 256 flat ASCII class files with paths up to 128 bytes.".into();
                    }
                }
            }
            let program = if status == v1::CompilationStatus::Succeeded {
                let metadata = v2::CompiledProgram {
                    kind: m.language.contract().program_kind,
                    binding: binding.clone(),
                    source: m.source.clone(),
                    toolchain: m.toolchain.clone(),
                    files,
                };
                self.authorized
                    .validate_program(&metadata)
                    .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, true))?;
                self.program = Some(SealedProgram {
                    metadata: metadata.clone(),
                    files: sealed,
                });
                Some(metadata)
            } else {
                None
            };
            Ok(engine::Compiled {
                report: v2::CompilationReport {
                    status,
                    program,
                    diagnostics: text.try_into().expect("bounded diagnostics"),
                    diagnostics_truncated: truncated,
                    cpu_time_us,
                    memory_peak_bytes,
                },
                cleanup_complete: true,
                failure: (status == v1::CompilationStatus::SystemError)
                    .then_some(v1::FailureCode::SandboxUnavailable),
            })
        })
    }
}
impl<A: ArtifactPort> engine::RunPort for RuntimeAttempt<'_, A> {
    fn run(
        &mut self,
        program: &v2::CompiledProgram,
        case: &v1::TestCase,
        budget: &v1::RunLimits,
        run_id: &str,
    ) -> Result<Observation, old::StageFailure> {
        let index = self
            .authorized
            .manifest()
            .cases
            .iter()
            .position(|c| c == case)
            .ok_or_else(|| failure(v1::FailureCode::ArtifactUnavailable, true))?;
        if budget != &self.authorized.manifest().run_limits {
            return Err(failure(v1::FailureCode::SandboxUnavailable, true));
        }
        let spec = self
            .authorized
            .run_spec(program, index, run_id.into())
            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, true))?;
        let stored = self
            .program
            .as_ref()
            .filter(|s| &s.metadata == program)
            .ok_or_else(|| failure(v1::FailureCode::ArtifactUnavailable, true))?;
        if stored.files.len() != program.files.len() {
            return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
        }
        let input = self
            .io
            .artifacts
            .read(&case.input)
            .map_err(artifact_error)?;
        verify(&case.input, &input).map_err(artifact_error)?;
        let publication = StagePublication::create(self.io.staging_path(), run_id)
            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, false))?;
        with_publication(publication, |publication| {
            for ((info, file), artifact) in
                program.files.iter().zip(&stored.files).zip(&spec.inputs)
            {
                if snapshot_reference(file)? != info.artifact {
                    return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
                }
                publication
                    .put(
                        &artifact.artifact_id,
                        &mut file.reader().map_err(artifact_error)?,
                        info.artifact.bytes,
                    )
                    .map_err(artifact_error)?;
            }
            publication
                .put("stdin", &mut input.as_slice(), case.input.bytes)
                .map_err(artifact_error)?;
            drop(input);
            let validated = spec
                .clone()
                .validate(&kf_sandbox::spec::SpecPolicy::default())
                .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, true))?;
            let result = self.io.execute(spec, publication)?;
            let outcome = result
                .report
                .classify(&validated)
                .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, false))?;
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
            let mut stderr = Vec::new();
            if self.authorized.manifest().self_test {
                result
                    .stderr
                    .reader()
                    .map_err(artifact_error)?
                    .take(16385)
                    .read_to_end(&mut stderr)
                    .map_err(artifact_error)?;
            }
            Ok(Observation {
                stderr,
                stdout_truncated: self.authorized.manifest().self_test
                    && result.report.stdout.truncated,
                stderr_truncated: self.authorized.manifest().self_test
                    && result.report.stderr.truncated,
                kind: kind(outcome),
                stdout,
                cpu_time_us: metric(&result.report.usage.cpu_time_us),
                memory_peak_bytes: metric(&result.report.usage.memory_peak_bytes),
            })
        })
    }
}
impl<A: ArtifactPort> old::ExpectedPort for RuntimeAttempt<'_, A> {
    fn expected(&mut self, case: &v1::TestCase) -> Result<Vec<u8>, old::StageFailure> {
        if !self.authorized.manifest().cases.contains(case) {
            return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
        }
        let bytes = self
            .io
            .artifacts
            .read(&case.expected)
            .map_err(artifact_error)?;
        verify(&case.expected, &bytes).map_err(artifact_error)?;
        Ok(bytes)
    }
}
impl<A: ArtifactPort> old::ControlPort for RuntimeAttempt<'_, A> {
    fn elapsed_us(&self) -> u64 {
        old::ControlPort::elapsed_us(&self.io)
    }
    fn cancelled(&self) -> bool {
        old::ControlPort::cancelled(&self.io)
    }
    fn stage(&mut self, stage: v1::Stage, index: Option<u16>) -> Result<(), old::StageFailure> {
        old::ControlPort::stage(&mut self.io, stage, index)
    }
}
pub fn judge_local(
    socket: &Path,
    job: &Path,
    artifacts: &Path,
    staging: &Path,
    admission: &NodeAdmission,
) -> io::Result<Judged> {
    let job = read_local_job(job)?;
    let authorized = admit_local_job(&job, admission)?;
    StagePublication::recover(staging)?;
    let cancellation = AtomicBool::new(false);
    execute_local_job(job, authorized, socket, artifacts, staging, &cancellation)
}

fn read_local_job(job: &Path) -> io::Result<Job> {
    v1::decode_wire(&kf_sandbox::execution::read_bounded_file(
        job,
        v1::MAX_WIRE_BYTES as u64,
    )?)
    .map_err(io::Error::other)
}

fn admit_local_job(job: &Job, admission: &NodeAdmission) -> io::Result<AuthorizedJob> {
    AuthorizedJob::admit(
        job.manifest.clone(),
        job.binding.clone(),
        job.manifest_digest.clone(),
        job.attempt_number,
        admission,
    )
    .map_err(io::Error::other)
}

fn execute_local_job(
    job: Job,
    authorized: AuthorizedJob,
    socket: &Path,
    artifacts: &Path,
    staging: &Path,
    cancellation: &AtomicBool,
) -> io::Result<Judged> {
    let mut attempt = RuntimeAttempt::new(
        authorized,
        LocalArtifacts::open(artifacts)?,
        socket.into(),
        staging.into(),
        cancellation,
    )?;
    let result = engine::judge_submission(
        &mut attempt,
        &job.manifest,
        job.binding,
        job.manifest_digest,
    )
    .map_err(io::Error::other)?;
    Ok(Judged {
        result,
        stages: attempt.into_stages(),
    })
}

#[cfg(target_os = "linux")]
fn admit_diagnostic_replay(
    socket: &Path,
    job: &Path,
    staging: &Path,
    admission: &Path,
    worker_id: &v1::Identifier,
    factory_config_sha256: &str,
) -> io::Result<(Job, AuthorizedJob)> {
    use kf_contracts::diagnostic::{
        MAX_DIAGNOSTIC_BATCH_CASES, MAX_DIAGNOSTIC_WALL_US, MAX_PREPARATION_WALL_US,
        MAX_SAMPLE_BYTES, MAX_SAMPLE_WALL_US,
    };

    let refused = || io::Error::other("diagnostic replay refused");
    let admission = crate::diagnostic_stdio::load_policy(admission)?;
    let job = read_local_job(job)?;
    let manifest = &job.manifest;
    let compile_wall = manifest.compile_limits.wall_time_us;
    let run_wall = manifest.run_limits.wall_time_us;
    if job.binding.worker_id != *worker_id
        || job.attempt_number != 1
        || manifest.self_test
        || !matches!(manifest.language, v2::Language::C17 | v2::Language::Cpp20)
        || !(2..=MAX_DIAGNOSTIC_BATCH_CASES).contains(&manifest.cases.len())
        || compile_wall > MAX_PREPARATION_WALL_US
        || run_wall > MAX_SAMPLE_WALL_US
        || manifest.job_wall_time_us > MAX_DIAGNOSTIC_WALL_US
        || manifest.job_wall_time_us < compile_wall + manifest.cases.len() as u64 * run_wall
        || manifest.cases.iter().any(|case| {
            case.input.bytes.max(case.expected.bytes) > MAX_SAMPLE_BYTES
                || case.expected.bytes > manifest.run_limits.stdout_bytes
        })
    {
        return Err(refused());
    }
    let authorized = admit_local_job(&job, &admission)?;
    let factory = kf_sandbox::factory::FactoryClient::new(socket.to_path_buf());
    if !factory
        .health()
        .is_ok_and(|health| health.configuration_sha256 == factory_config_sha256)
        || std::fs::read_dir(staging)?.next().is_some()
    {
        return Err(refused());
    }
    Ok((job, authorized))
}

#[cfg(target_os = "linux")]
fn replay_cancellation() -> io::Result<std::sync::Arc<AtomicBool>> {
    use std::sync::{Arc, atomic::Ordering};

    let cancellation = Arc::new(AtomicBool::new(false));
    let signal = Arc::clone(&cancellation);
    std::thread::Builder::new()
        .name("diagnostic-replay-control".into())
        .spawn(move || {
            let _ = io::stdin().lock().read(&mut [0u8; 1]);
            signal.store(true, Ordering::Release);
        })?;
    Ok(cancellation)
}

#[cfg(target_os = "linux")]
pub fn replay_diagnostic_local(
    socket: &Path,
    job: &Path,
    artifacts: &Path,
    staging: &Path,
    admission: &Path,
    worker_id: &v1::Identifier,
    factory_config_sha256: &str,
) -> io::Result<v2::JudgementReport> {
    let (job, authorized) = admit_diagnostic_replay(
        socket,
        job,
        staging,
        admission,
        worker_id,
        factory_config_sha256,
    )?;
    let cancellation = replay_cancellation()?;
    let judged = execute_local_job(job, authorized, socket, artifacts, staging, &cancellation)?;
    let mut report = judged.result.report;
    report.compilation.diagnostics = String::new().try_into().expect("empty diagnostics");
    report.compilation.diagnostics_truncated = false;
    Ok(report)
}

#[cfg(target_os = "linux")]
pub fn measure_diagnostic_local(
    socket: &Path,
    job: &Path,
    artifacts: &Path,
    staging: &Path,
    admission: &Path,
    worker_id: &v1::Identifier,
    factory_config_sha256: &str,
) -> io::Result<kf_contracts::diagnostic::DiagnosticBoundaryReport> {
    use engine::{PreparationPort, RunPort};
    use kf_contracts::{
        SchemaVersion,
        diagnostic::{
            BoundaryMeasurement, BoundaryOutcome, BoundaryReportKind, DiagnosticBoundaryReport,
        },
    };
    use kf_engine::ExecutionKind;
    use old::ControlPort;

    let (job, authorized) = admit_diagnostic_replay(
        socket,
        job,
        staging,
        admission,
        worker_id,
        factory_config_sha256,
    )?;
    let refused = || io::Error::other("boundary measurement refused");
    let m = &job.manifest;
    // These input-only cases reuse the v2 admission envelope. The empty
    // reference marks the unused expected-output field, which is never read.
    let empty_digest = format!("{:x}", Sha256::digest([]));
    if m.cases
        .iter()
        .any(|case| case.expected.bytes != 0 || case.expected.digest.as_str() != empty_digest)
    {
        return Err(refused());
    }
    let cancellation = replay_cancellation()?;
    let mut attempt = RuntimeAttempt::new(
        authorized,
        LocalArtifacts::open(artifacts)?,
        socket.into(),
        staging.into(),
        &cancellation,
    )?;
    let attempt_id = job.binding.attempt_id.as_uuid();
    let verify_active = |port: &RuntimeAttempt<'_, LocalArtifacts>| {
        if port.cancelled() || port.elapsed_us() >= m.job_wall_time_us {
            return Err(refused());
        }
        Ok(())
    };
    verify_active(&attempt)?;
    let compiled = attempt
        .prepare(m, &job.binding, &format!("c-{attempt_id}"))
        .map_err(|_| refused())?;
    if !compiled.cleanup_complete || compiled.failure.is_some() {
        return Err(refused());
    }
    let program = compiled.report.program.ok_or_else(refused)?;
    let mut cases = Vec::with_capacity(m.cases.len());
    for (index, case) in m.cases.iter().enumerate() {
        verify_active(&attempt)?;
        let observed = attempt
            .run(
                &program,
                case,
                &m.run_limits,
                &format!("r-{attempt_id}-{index}"),
            )
            .map_err(|_| refused())?;
        let outcome = match observed.kind {
            ExecutionKind::Exited { code: 0 } => BoundaryOutcome::Completed,
            ExecutionKind::Exited { .. } | ExecutionKind::Signaled => BoundaryOutcome::RuntimeError,
            ExecutionKind::TimeLimit => BoundaryOutcome::TimeLimit,
            ExecutionKind::MemoryLimit => BoundaryOutcome::MemoryLimit,
            ExecutionKind::OutputLimit => BoundaryOutcome::OutputLimit,
            ExecutionKind::OtherLimit => BoundaryOutcome::OtherLimit,
            ExecutionKind::Cancelled | ExecutionKind::InfrastructureFailure => {
                return Err(refused());
            }
        };
        cases.push(BoundaryMeasurement {
            case_id: case.case_id,
            outcome,
            cpu_time_us: observed.cpu_time_us.ok_or_else(refused)?,
            memory_peak_bytes: observed.memory_peak_bytes.ok_or_else(refused)?,
        });
    }
    verify_active(&attempt)?;
    let report = DiagnosticBoundaryReport {
        schema_version: SchemaVersion,
        kind: BoundaryReportKind::Value,
        binding: job.binding,
        manifest_digest: job.manifest_digest,
        cases,
    };
    report.validate().map_err(io::Error::other)?;
    Ok(report)
}

#[cfg(test)]
mod publication_tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::{MetadataExt, PermissionsExt},
    };

    #[test]
    fn early_failure_requires_positive_cleanup_and_preserves_foreign_entries() {
        // Only synthetic artifact bytes; this test does not execute source,
        // acquire privileged authority, or claim Linux isolation acceptance.
        let parent =
            std::env::temp_dir().join(format!("kf-worker-publication-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&parent).unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(fs::metadata(&parent).unwrap().uid() >= 1000);
        let publication = StagePublication::create(&parent, "short-read").unwrap();
        let result: Result<(), old::StageFailure> = with_publication(publication, |p| {
            p.put("source", &mut b"ab".as_slice(), 4)
                .map_err(artifact_error)
        });
        let error = result.unwrap_err();
        assert_eq!(error.code, v1::FailureCode::ArtifactUnavailable);
        assert!(error.cleanup_complete);
        assert!(!parent.join("short-read").exists());
        let publication = StagePublication::create(&parent, "unconfirmed-root").unwrap();
        let result: Result<(), old::StageFailure> = with_publication(publication, |_| {
            Err(failure(v1::FailureCode::SandboxUnavailable, false))
        });
        assert!(
            !result.unwrap_err().cleanup_complete,
            "staging success cannot upgrade an unconfirmed Root cleanup"
        );
        assert!(!parent.join("unconfirmed-root").exists());
        let publication = StagePublication::create(&parent, "foreign").unwrap();
        let foreign = parent.join("foreign/canary");
        let result: Result<(), old::StageFailure> = with_publication(publication, |_| {
            fs::write(&foreign, b"test-owned foreign inode").unwrap();
            Err(failure(v1::FailureCode::ArtifactUnavailable, true))
        });
        let error = result.unwrap_err();
        assert_eq!(error.code, v1::FailureCode::CleanupFailed);
        assert!(!error.cleanup_complete);
        let inode = fs::metadata(&foreign).unwrap().ino();
        assert_eq!(fs::read(&foreign).unwrap(), b"test-owned foreign inode");
        assert_eq!(fs::metadata(&foreign).unwrap().ino(), inode);
        // The fixture owns these exact entries; remove them only after proving
        // automatic cleanup retained the unknown inode and reported failure.
        fs::remove_file(foreign).unwrap();
        fs::remove_file(parent.join("foreign/.owner")).unwrap();
        fs::remove_dir(parent.join("foreign")).unwrap();
        fs::remove_dir(parent).unwrap();
    }
}
