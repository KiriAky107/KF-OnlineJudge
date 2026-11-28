//! One private, broker-owned input stream. EOF after the frame cancels the attempt.
use crate::diagnostic::{
    DiagnosticRun as WorkerRun, PublicSampleInput, observe_public_sample, observe_ubsan_sample,
};
use kf_contracts::{
    SchemaVersion,
    diagnostic::{
        CLAIM_SECONDS, DiagnosticMode, DiagnosticObservation, DiagnosticResult, DiagnosticRun,
        MAX_CONTROL_BYTES, ResultKind, ResultState,
    },
    judging::{FailureCode, Identifier, LeaseBinding, decode_wire},
    judging_versions::NodeAdmission,
};
use kf_engine::diagnostic::{OptimizationError, OptimizationRequest};
use kf_sandbox::{execution::read_bounded_file, factory::FactoryClient};
use std::{
    io::{self, Read, Write},
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

fn invalid() -> io::Error {
    io::Error::other("diagnostic request refused")
}

pub(crate) fn load_policy(path: &Path) -> io::Result<NodeAdmission> {
    let metadata = std::fs::symlink_metadata(path)?;
    if !metadata.is_file() || metadata.nlink() != 1 || metadata.mode() & 0o7777 != 0o600 {
        return Err(invalid());
    }
    let policy: NodeAdmission =
        decode_wire(&read_bounded_file(path, 32768)?).map_err(|_| invalid())?;
    policy.validate().map_err(|_| invalid())?;
    Ok(policy)
}

fn admit(run: &DiagnosticRun, policy: &NodeAdmission, worker_id: &Identifier) -> io::Result<()> {
    let node = match policy {
        NodeAdmission::Multi(multi) => Some(multi),
        NodeAdmission::Registry(registry) => registry.multi_runtime.as_ref(),
        NodeAdmission::Legacy(_) => None,
    }
    .ok_or_else(invalid)?;
    let config = &run.configuration;
    let cap = node
        .capabilities
        .iter()
        .find(|cap| cap.language == config.language)
        .ok_or_else(invalid)?;
    if run.binding.worker_id != *worker_id
        || cap.toolchain != config.toolchain
        || !config.compile_limits.fits(&cap.compile_ceiling)
        || !config.run_limits.fits(&cap.run_ceiling)
        || config.job_wall_time_us > node.max_job_wall_time_us
        || run.source.bytes + run.sample.input.bytes + run.sample.expected.bytes
            > node.max_package_bytes
    {
        return Err(invalid());
    }
    Ok(())
}

fn read_artifact(reader: &mut impl Read, bytes: u64) -> io::Result<Vec<u8>> {
    let mut contents = vec![0; usize::try_from(bytes).map_err(|_| invalid())?];
    reader.read_exact(&mut contents)?;
    Ok(contents)
}

fn read_run(
    reader: &mut impl Read,
    policy: &NodeAdmission,
    worker_id: &Identifier,
) -> io::Result<(DiagnosticRun, PublicSampleInput)> {
    let mut size = [0; 4];
    reader.read_exact(&mut size)?;
    let size = u32::from_be_bytes(size) as usize;
    if size == 0 || size > MAX_CONTROL_BYTES {
        return Err(invalid());
    }
    let mut header = vec![0; size];
    reader.read_exact(&mut header)?;
    let run: DiagnosticRun = decode_wire(&header).map_err(|_| invalid())?;
    admit(&run, policy, worker_id)?;
    let source = read_artifact(reader, run.source.bytes)?;
    let input = read_artifact(reader, run.sample.input.bytes)?;
    let expected = read_artifact(reader, run.sample.expected.bytes)?;
    let checked = PublicSampleInput::from_resolved_mode(
        &OptimizationRequest {
            configuration: &run.configuration,
            binding: &LeaseBinding {
                evaluation_id: run.binding.request_id,
                attempt_id: run.binding.execution_id,
                worker_id: run.binding.worker_id,
                fencing_version: 1,
            },
            source: &run.source,
            public_sample: &run.sample,
            expected: &expected,
        },
        source,
        input,
        run.mode,
    )
    .map_err(|_| invalid())?;
    Ok((run, checked))
}

fn claim_deadline(expiry_ms: u64) -> Option<Instant> {
    let expiry = UNIX_EPOCH.checked_add(Duration::from_millis(expiry_ms))?;
    let start = Instant::now();
    let remaining = expiry.duration_since(SystemTime::now()).ok()?;
    if remaining > Duration::from_secs(CLAIM_SECONDS) {
        return None;
    }
    start.checked_add(remaining)
}

fn report_outcome<T>(
    report: &mut DiagnosticResult,
    attempt: io::Result<WorkerRun<T>>,
    save: impl FnOnce(&mut DiagnosticResult, T),
) {
    match attempt.map(|run| run.result) {
        Ok(Ok(observation)) => save(report, observation),
        Ok(Err(OptimizationError::Cancelled)) => report.state = ResultState::Cancelled,
        Ok(Err(OptimizationError::Stage(error)))
            if error.cleanup_complete && error.code != FailureCode::CleanupFailed =>
        {
            report.state = ResultState::Failed;
            report.failure_code = Some(error.code);
        }
        _ => {}
    }
}

pub fn run_stdio(
    socket: PathBuf,
    staging: PathBuf,
    admission: &Path,
    worker_id: &Identifier,
    factory_config_sha256: &str,
) -> io::Result<()> {
    let policy = load_policy(admission)?;
    let (run, input) = read_run(&mut io::stdin().lock(), &policy, worker_id)?;
    let mut report = DiagnosticResult {
        schema_version: SchemaVersion,
        kind: ResultKind::Value,
        binding: run.binding,
        state: ResultState::CleanupUnconfirmed,
        observation: None,
        failure_code: None,
        ubsan: None,
    };
    if let Some(deadline) = claim_deadline(run.claim_expires_at_unix_ms) {
        let ready = FactoryClient::new(socket.clone())
            .health()
            .is_ok_and(|health| health.configuration_sha256 == factory_config_sha256);
        if ready {
            let cancellation = Arc::new(AtomicBool::new(false));
            let signal = Arc::clone(&cancellation);
            std::thread::Builder::new()
                .name("diagnostic-control".into())
                .spawn(move || {
                    let _ = io::stdin().lock().read(&mut [0u8; 1]);
                    signal.store(true, Ordering::Release);
                })?;
            match run.mode {
                DiagnosticMode::Optimization => report_outcome(
                    &mut report,
                    observe_public_sample(input, socket, staging, deadline, &cancellation),
                    |report, observation| {
                        if observation.case_id == report.binding.execution_id {
                            report.state = ResultState::Observed;
                            report.observation = Some(DiagnosticObservation {
                                o2: observation.o2,
                                o0: observation.o0,
                            });
                        }
                    },
                ),
                DiagnosticMode::Ubsan => report_outcome(
                    &mut report,
                    observe_ubsan_sample(input, socket, staging, deadline, &cancellation),
                    |report, observation| {
                        report.state = ResultState::Observed;
                        report.ubsan = Some(observation);
                    },
                ),
            }
        } else {
            // Authentication of the expected factory precedes every attempt.
            report.state = ResultState::Failed;
            report.failure_code = Some(FailureCode::SandboxUnavailable);
        }
    } else {
        report.state = ResultState::Failed;
        report.failure_code = Some(FailureCode::JobDeadline);
    }
    report.validate().map_err(|_| invalid())?;
    let mut stdout = io::stdout().lock();
    serde_json::to_writer(&mut stdout, &report).map_err(io::Error::other)?;
    stdout.flush()
}
