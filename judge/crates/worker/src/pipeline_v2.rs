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
fn failure(code: v1::FailureCode, cleanup_complete: bool) -> old::StageFailure {
    old::StageFailure {
        code,
        cleanup_complete,
    }
}
fn artifact_error(_: io::Error) -> old::StageFailure {
    failure(v1::FailureCode::ArtifactUnavailable, true)
}
fn snapshot_reference(file: &SnapshotFile) -> Result<v1::ArtifactReference, old::StageFailure> {
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
    let mut raw = Vec::new();
    result
        .stderr
        .reader()
        .map_err(artifact_error)?
        .take(65537)
        .read_to_end(&mut raw)
        .map_err(artifact_error)?;
    let mut text = String::from_utf8_lossy(&raw).into_owned();
    let mut truncated = result.report.stderr.truncated || text.len() > 65536;
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
        let mut publication =
            StagePublication::create(self.io.staging_path(), run_id).map_err(artifact_error)?;
        publication
            .put("source", &mut source.as_slice(), m.source.bytes)
            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, false))?;
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
        let result = self.io.execute(spec, &mut publication)?;
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
                    text="Java submission requires default-package Main with public static void main(String[]).".into();
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
        let mut publication =
            StagePublication::create(self.io.staging_path(), run_id).map_err(artifact_error)?;
        for ((info, file), artifact) in program.files.iter().zip(&stored.files).zip(&spec.inputs) {
            if snapshot_reference(file)? != info.artifact {
                return Err(failure(v1::FailureCode::ArtifactUnavailable, true));
            }
            publication
                .put(
                    &artifact.artifact_id,
                    &mut file.reader().map_err(artifact_error)?,
                    info.artifact.bytes,
                )
                .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, false))?;
        }
        publication
            .put("stdin", &mut input.as_slice(), case.input.bytes)
            .map_err(|_| failure(v1::FailureCode::ArtifactUnavailable, false))?;
        drop(input);
        let validated = spec
            .clone()
            .validate(&kf_sandbox::spec::SpecPolicy::default())
            .map_err(|_| failure(v1::FailureCode::SandboxUnavailable, true))?;
        let result = self.io.execute(spec, &mut publication)?;
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
        Ok(Observation {
            kind: kind(outcome),
            stdout,
            cpu_time_us: metric(&result.report.usage.cpu_time_us),
            memory_peak_bytes: metric(&result.report.usage.memory_peak_bytes),
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
    let job: Job = v1::decode_wire(&kf_sandbox::execution::read_bounded_file(
        job,
        v1::MAX_WIRE_BYTES as u64,
    )?)
    .map_err(io::Error::other)?;
    let authorized = AuthorizedJob::admit(
        job.manifest.clone(),
        job.binding.clone(),
        job.manifest_digest.clone(),
        job.attempt_number,
        admission,
    )
    .map_err(io::Error::other)?;
    let cancellation = AtomicBool::new(false);
    let mut attempt = RuntimeAttempt::new(
        authorized,
        LocalArtifacts::open(artifacts)?,
        socket.into(),
        staging.into(),
        &cancellation,
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
