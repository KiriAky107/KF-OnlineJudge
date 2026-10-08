//! Verified single-task composition, with a separate ordinary entry policy.
use super::{
    blocked::{InitializationFailure, failure},
    capture::CaptureLease,
    protocol::{NamespaceCommand, NamespaceEvent},
    reap::OwnedChildren,
    seccomp::FilteredTarget,
};
use crate::{
    artifacts::{ArtifactRoot, OutputSnapshot, SnapshotFile},
    io::{CaptureStatus, PipePump},
    outcome::*,
    runtime::ValidatedRuntimeConfig,
    spec::ValidatedSpec,
    supervisor::{BudgetEvent, BudgetSupervisor},
};
use rustix::{
    fd::AsFd,
    process::{WaitId, WaitIdOptions},
};
use std::{
    io,
    path::Path,
    time::{Duration, Instant},
};

pub(crate) struct Completed {
    pub report: ExecutionReport,
    pub stdout: SnapshotFile,
    pub stderr: SnapshotFile,
    pub outputs: Vec<OutputSnapshot>,
    pub proof: RunProof,
}
#[derive(Debug, serde::Serialize)]
pub(crate) struct RunProof {
    pub reaped_orphans: u32,
    pub reaped_descendants: u32,
    pub stdin_closed_early: bool,
    pub order: Vec<&'static str>,
}
#[derive(Clone, Copy)]
pub(crate) enum MonitorMode {
    Normal,
    #[cfg(feature = "vm-acceptance")]
    CancelAfter(u64),
    #[cfg(feature = "vm-acceptance")]
    FailAfter(u64),
    #[cfg(feature = "vm-acceptance")]
    RestartReady,
}
fn capture(status: CaptureStatus, id: &str) -> OutputCapture {
    OutputCapture {
        artifact_id: (status.retained > 0).then(|| id.into()),
        observed_bytes: Measurement::Measured {
            value: status.observed,
        },
        retained_bytes: status.retained,
        truncated: status.exceeded,
        reached_eof: status.eof,
    }
}
fn error(stage: ExecutionStage, code: ErrorCode, e: &io::Error) -> ExecutionError {
    failure(stage, code, Some(e))
}
fn wall(usage: &ResourceUsage) -> u64 {
    match usage.wall_time_us {
        Measurement::Measured { value } => value,
        _ => 0,
    }
}
fn consume(event: BudgetEvent) -> (ResourceUsage, Vec<LimitObservation>) {
    match event {
        BudgetEvent::Running { usage } => (usage, Vec::new()),
        BudgetEvent::Finished {
            usage,
            observations,
        } => (usage, observations),
    }
}

pub(crate) fn execute(
    config: &ValidatedRuntimeConfig,
    spec: &ValidatedSpec,
    binary: &Path,
    artifacts: &ArtifactRoot,
    mode: MonitorMode,
) -> Result<Completed, InitializationFailure> {
    #[cfg(not(feature = "vm-acceptance"))]
    let _ = mode;
    let mut owned = OwnedChildren::begin().map_err(|e| InitializationFailure {
        failure: error(ExecutionStage::Preparation, ErrorCode::OperationFailed, &e),
        cleanup_failure: None,
    })?;
    super::capture::private_supervisor().map_err(|e| InitializationFailure {
        failure: error(ExecutionStage::Preparation, ErrorCode::OperationFailed, &e),
        cleanup_failure: None,
    })?;
    let mut target = FilteredTarget::prepare(config, spec, binary, artifacts)?;
    let setup = (|| {
        let work = target.root().pin_work()?;
        let (lease, dir) = CaptureLease::prepare(config, spec)?;
        let (pump, stdio) = PipePump::prepare(target.take_stdin(), spec, dir)?;
        crate::cleanup::verify_owned(config, spec, true)?;
        target.root_mut().target.prepare_execution(spec, stdio)?;
        Ok::<_, io::Error>((work, lease, pump))
    })();
    let (work, mut lease, mut pump) = match setup {
        Ok(s) => s,
        Err(e) => {
            return Err(target.refuse_at(ExecutionStage::Exec, Some(&e)));
        }
    };
    let init = target.root().init_pid();
    let bootstrap = target.root().bootstrap_pid();
    let mut errors = Vec::new();
    let mut output_limits = Vec::new();
    #[cfg(feature = "vm-acceptance")]
    let mut cancelled = None;
    #[cfg(not(feature = "vm-acceptance"))]
    let cancelled: Option<u64> = None;
    let mut main = None;
    let mut reaped_descendants = 0;
    let mut order = vec!["verified_isolation", "exec_gate"];
    let (usage, mut limits, reaped_orphans) = {
        let ns = &mut target.root_mut().target;
        let budget = BudgetSupervisor::prepare(&mut ns.prepared.cgroup, spec);
        let mut budget = match budget {
            Ok(b) => b,
            Err(e) => {
                return Err(InitializationFailure {
                    failure: error(ExecutionStage::Exec, ErrorCode::OperationFailed, &e.source),
                    cleanup_failure: e
                        .termination_error
                        .as_ref()
                        .map(|e| error(ExecutionStage::Cleanup, ErrorCode::CleanupIncomplete, e)),
                });
            }
        };
        if let Err(e) = budget.mark_exec_release() {
            return Err(InitializationFailure {
                failure: error(ExecutionStage::Exec, ErrorCode::OperationFailed, &e.source),
                cleanup_failure: e
                    .termination_error
                    .as_ref()
                    .map(|e| error(ExecutionStage::Cleanup, ErrorCode::CleanupIncomplete, e)),
            });
        }
        let released = (|| {
            ns.control.send(&NamespaceCommand::ReleaseExecution {})?;
            budget.release_prepared_gate(&[bootstrap, init], &ns.init_pidfd)?;
            if ns.control.receive::<NamespaceEvent>()? != (NamespaceEvent::ExecutionSpawned {}) {
                return Err(io::Error::other("spawn acknowledgement"));
            }
            ns.control.receive_fd()
        })();
        let main_fd = match released {
            Ok(fd) => Some(fd),
            Err(e) => {
                errors.push(error(ExecutionStage::Exec, ErrorCode::ProtocolFailed, &e));
                None
            }
        };
        // Fixed VM crash probe only: retain a real supervisor-owned residual
        // child in the same marked cgroup to exercise post-crash startup kill.
        #[cfg(feature = "vm-acceptance")]
        let _restart_child = if matches!(mode, MonitorMode::RestartReady) && main_fd.is_some() {
            match crate::acceptance::restart::ready(config, spec, "live") {
                Ok(child) => Some(child),
                Err(e) => {
                    errors.push(error(ExecutionStage::Exec, ErrorCode::OperationFailed, &e));
                    None
                }
            }
        } else {
            None
        };
        let mut monitoring_failed = !errors.is_empty();
        if !monitoring_failed {
            loop {
                let event = match budget.poll() {
                    Ok(e) => e,
                    Err(e) => {
                        errors.push(error(
                            ExecutionStage::Accounting,
                            ErrorCode::MeasurementUnavailable,
                            &e.source,
                        ));
                        break;
                    }
                };
                let finished = matches!(event, BudgetEvent::Finished { .. });
                let usage = consume(event).0;
                match pump.poll() {
                    Ok(status) => {
                        for (s, stdout) in [(status.stdout, true), (status.stderr, false)] {
                            if s.exceeded
                                && !output_limits.iter().any(|o: &LimitObservation| {
                                    o.evidence.kind()
                                        == if stdout {
                                            LimitKind::Stdout
                                        } else {
                                            LimitKind::Stderr
                                        }
                                })
                            {
                                output_limits.push(LimitObservation {
                                    at_wall_time_us: wall(&usage),
                                    evidence: if stdout {
                                        LimitEvidence::Stdout {
                                            observed_bytes: s.observed,
                                        }
                                    } else {
                                        LimitEvidence::Stderr {
                                            observed_bytes: s.observed,
                                        }
                                    },
                                });
                            }
                        }
                    }
                    Err(e) => {
                        errors.push(error(
                            ExecutionStage::Output,
                            ErrorCode::OutputReadFailed,
                            &e,
                        ));
                        monitoring_failed = true;
                    }
                }
                match ns.control.try_receive::<NamespaceEvent>() {
                    Ok(Some(NamespaceEvent::Tree { record })) => {
                        if let Some(errno) = record.exec_errno {
                            errors.push(ExecutionError {
                                stage: ExecutionStage::Exec,
                                code: ErrorCode::OperationFailed,
                                errno: Some(errno),
                            });
                        }
                        if record.main.is_some() {
                            main = record.main;
                        }
                        reaped_descendants = record.reaped_descendants;
                    }
                    Ok(None) => {}
                    Err(_) if finished => {}
                    Err(e) => {
                        errors.push(error(ExecutionStage::Wait, ErrorCode::ProtocolFailed, &e));
                        monitoring_failed = true;
                    }
                    Ok(Some(_)) => {
                        errors.push(error(
                            ExecutionStage::Wait,
                            ErrorCode::ProtocolFailed,
                            &io::Error::other("runtime event order"),
                        ));
                        monitoring_failed = true;
                    }
                }
                #[cfg(feature = "vm-acceptance")]
                match mode {
                    #[cfg(feature = "vm-acceptance")]
                    MonitorMode::CancelAfter(t) if wall(&usage) >= t => {
                        cancelled = Some(wall(&usage))
                    }
                    #[cfg(feature = "vm-acceptance")]
                    MonitorMode::FailAfter(t) if wall(&usage) >= t => {
                        errors.push(error(
                            ExecutionStage::Wait,
                            ErrorCode::ProtocolFailed,
                            &io::Error::other("VM injected monitor failure"),
                        ));
                        monitoring_failed = true;
                    }
                    _ => {}
                }
                if finished
                    || main.is_some()
                    || cancelled.is_some()
                    || !output_limits.is_empty()
                    || monitoring_failed
                {
                    break;
                }
                std::thread::sleep(Duration::from_millis(2));
            }
        }
        pump.close_input();
        // One termination primitive for normal descendant cleanup and every fault.
        let latest = match budget.terminate() {
            Ok(e) => consume(e).0,
            Err(e) => {
                errors.push(error(
                    ExecutionStage::Accounting,
                    ErrorCode::MeasurementUnavailable,
                    &e.source,
                ));
                e.usage
            }
        };
        order.push("cgroup_empty");
        if let Err(e) = ns.prepared.target.terminate() {
            errors.push(error(
                ExecutionStage::Cleanup,
                ErrorCode::CleanupIncomplete,
                &e,
            ));
        }
        // Drain queued authentic main-exit records before waiting adopted orphans.
        for _ in 0..8 {
            match ns.control.try_receive::<NamespaceEvent>() {
                Ok(Some(NamespaceEvent::Tree { record })) => {
                    if record.main.is_some() {
                        main = record.main;
                    }
                    reaped_descendants = record.reaped_descendants;
                }
                Ok(None) => continue,
                _ => break,
            }
        }
        if main.is_none()
            && let Some(fd) = main_fd.as_ref()
        {
            match rustix::process::waitid(
                WaitId::PidFd(fd.as_fd()),
                WaitIdOptions::EXITED | WaitIdOptions::NOWAIT | WaitIdOptions::NOHANG,
            ) {
                Ok(Some(s)) => {
                    if let Some(code) = s.exit_status() {
                        main = Some(Termination::Exited { code: code as u8 });
                    } else if let Some(signal) = s.terminating_signal() {
                        main = Some(Termination::Signaled {
                            signal: signal as u8,
                            core_dumped: s.dumped(),
                        });
                    }
                }
                Err(rustix::io::Errno::CHILD) => {}
                _ => {}
            }
        }
        let reaped = match owned.finish() {
            Ok(n) => n,
            Err(e) => {
                errors.push(error(
                    ExecutionStage::Cleanup,
                    ErrorCode::CleanupIncomplete,
                    &e,
                ));
                0
            }
        };
        order.push("owned_reaped");
        // If a main status was only available through adopted wait4, its owned
        // pidfd waitid above records it before the general orphan drain.
        let final_sample = if errors.iter().any(|e| e.stage == ExecutionStage::Cleanup) {
            // Keep counters unavailable if the required wait ownership proof
            // failed. Never present an earlier sample as final-after-reap.
            Err(crate::supervisor::SupervisionFailure {
                source: io::Error::other("reaping incomplete"),
                termination_error: None,
                usage: ResourceUsage {
                    cpu_time_us: Measurement::Unavailable {
                        reason: MeasurementFailure::ReadFailed,
                    },
                    wall_time_us: latest.wall_time_us.clone(),
                    memory_peak_bytes: Measurement::Unavailable {
                        reason: MeasurementFailure::ReadFailed,
                    },
                    memory_events: Measurement::Unavailable {
                        reason: MeasurementFailure::ReadFailed,
                    },
                },
            })
        } else {
            budget.after_reap()
        };
        order.push("final_accounting");
        match final_sample {
            Ok(e) => {
                let (u, l) = consume(e);
                (u, l, reaped)
            }
            Err(e) => {
                errors.push(error(
                    ExecutionStage::Accounting,
                    ErrorCode::MeasurementUnavailable,
                    &e.source,
                ));
                (e.usage, Vec::new(), reaped)
            }
        }
    };
    limits.append(&mut output_limits);
    // Finite final drain after all writers died. A saturated stream was already
    // closed after observing one byte beyond its ceiling.
    let deadline = Instant::now() + Duration::from_secs(1);
    while !pump.status().outputs_eof()
        && !pump.status().output_limit_exceeded()
        && Instant::now() < deadline
    {
        if let Err(e) = pump.poll() {
            errors.push(error(
                ExecutionStage::Output,
                ErrorCode::OutputReadFailed,
                &e,
            ));
            break;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    // Drain the other stream even if one exceeded; max 64MiB total reservation.
    for _ in 0..(crate::artifacts::MAX_SNAPSHOT_BYTES / 4096 + 2) {
        let status = pump.status();
        if (status.stdout.eof || status.stdout.exceeded)
            && (status.stderr.eof || status.stderr.exceeded)
        {
            break;
        }
        if let Err(e) = pump.poll() {
            errors.push(error(
                ExecutionStage::Output,
                ErrorCode::OutputReadFailed,
                &e,
            ));
            break;
        }
    }
    let status = pump.status();
    // Retain actual final excess evidence even if it arrived after task exit.
    for (s, stdout) in [(status.stdout, true), (status.stderr, false)] {
        if s.exceeded
            && !limits.iter().any(|o| {
                o.evidence.kind()
                    == if stdout {
                        LimitKind::Stdout
                    } else {
                        LimitKind::Stderr
                    }
            })
        {
            limits.push(LimitObservation {
                at_wall_time_us: wall(&usage),
                evidence: if stdout {
                    LimitEvidence::Stdout {
                        observed_bytes: s.observed,
                    }
                } else {
                    LimitEvidence::Stderr {
                        observed_bytes: s.observed,
                    }
                },
            });
        }
    }
    let stdout = SnapshotFile::from_capture(pump.stdout());
    let stderr = SnapshotFile::from_capture(pump.stderr());
    let outputs = if errors.iter().any(|e| e.stage == ExecutionStage::Cleanup) {
        Vec::new()
    } else {
        match target.root().final_outputs(&work) {
            Ok(s) => s,
            Err(e) => {
                errors.push(error(
                    ExecutionStage::ArtifactExport,
                    ErrorCode::ArtifactExportFailed,
                    &e,
                ));
                Vec::new()
            }
        }
    };
    order.push("final_export");
    drop(work);
    drop(pump);
    if let Err(e) = lease.cleanup() {
        errors.push(error(
            ExecutionStage::Cleanup,
            ErrorCode::CleanupIncomplete,
            &e,
        ));
    }
    if !errors.iter().any(|e| e.stage == ExecutionStage::Cleanup)
        && let Err(e) = target.finish_resources()
    {
        errors.push(error(
            ExecutionStage::Cleanup,
            ErrorCode::CleanupIncomplete,
            &e,
        ));
    }
    order.push("resource_removal");
    let stdout = stdout.map_err(|e| InitializationFailure {
        failure: error(ExecutionStage::Output, ErrorCode::OutputReadFailed, &e),
        cleanup_failure: errors
            .iter()
            .find(|e| e.stage == ExecutionStage::Cleanup)
            .cloned(),
    })?;
    let stderr = stderr.map_err(|e| InitializationFailure {
        failure: error(ExecutionStage::Output, ErrorCode::OutputReadFailed, &e),
        cleanup_failure: errors
            .iter()
            .find(|e| e.stage == ExecutionStage::Cleanup)
            .cloned(),
    })?;
    let report = ExecutionReport {
        schema_version: spec.spec().schema_version,
        run_id: spec.spec().run_id.clone(),
        termination: main.unwrap_or(Termination::StoppedBySupervisor),
        usage,
        stdout: capture(status.stdout, "stdout"),
        stderr: capture(status.stderr, "stderr"),
        artifacts: outputs
            .iter()
            .enumerate()
            .map(|(i, f)| OutputArtifact {
                path: f.path.clone(),
                artifact_id: format!("output-{i}"),
                bytes: f.file.bytes(),
            })
            .collect(),
        limits,
        cancelled_at_wall_time_us: cancelled,
        cleanup: if errors.iter().any(|e| e.stage == ExecutionStage::Cleanup) {
            CleanupState::Failed
        } else {
            CleanupState::Complete
        },
        errors,
    };
    Ok(Completed {
        report,
        stdout,
        stderr,
        outputs,
        proof: RunProof {
            reaped_orphans,
            reaped_descendants,
            stdin_closed_early: status.stdin_closed_early,
            order,
        },
    })
}
