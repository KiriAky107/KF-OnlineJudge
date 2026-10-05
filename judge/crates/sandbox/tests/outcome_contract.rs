use kf_sandbox::outcome::*;
use kf_sandbox::spec::{ExecutionSpec, SpecPolicy, ValidatedSpec};

fn spec() -> ValidatedSpec {
    serde_json::from_str::<ExecutionSpec>(include_str!("fixtures/spec.valid.json"))
        .unwrap()
        .validate(&SpecPolicy::default())
        .unwrap()
}

fn report() -> ExecutionReport {
    serde_json::from_str(include_str!("fixtures/outcome.exited.json")).unwrap()
}

fn measured<T>(value: T) -> Measurement<T> {
    Measurement::Measured { value }
}

fn unavailable<T>() -> Measurement<T> {
    Measurement::Unavailable {
        reason: MeasurementFailure::ReadFailed,
    }
}

fn observation(time: u64, evidence: LimitEvidence) -> LimitObservation {
    LimitObservation {
        at_wall_time_us: time,
        evidence,
    }
}

fn error(stage: ExecutionStage) -> ExecutionError {
    ExecutionError {
        stage,
        code: ErrorCode::OperationFailed,
        errno: Some(5),
    }
}

fn cpu_limited() -> ExecutionReport {
    let mut result = report();
    result.usage.cpu_time_us = measured(spec().spec().limits.cpu_time_us);
    result.limits.push(observation(
        1000,
        LimitEvidence::CpuTime {
            observed_us: spec().spec().limits.cpu_time_us,
        },
    ));
    result.termination = Termination::Signaled {
        signal: 9,
        core_dumped: false,
    };
    result
}

#[test]
fn exited_fixture_roundtrips_without_a_judge_verdict() {
    let mut result = report();
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::Exited { code: 0 })
    );
    let json = serde_json::to_string(&result).unwrap();
    assert!(!json.contains("accepted"));
    assert_eq!(
        serde_json::from_str::<ExecutionReport>(&json).unwrap(),
        result
    );
    result.termination = Termination::Exited { code: 23 };
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::Exited { code: 23 })
    );
    result.termination = Termination::Signaled {
        signal: 11,
        core_dumped: true,
    };
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::Signaled {
            signal: 11,
            core_dumped: true
        })
    );
}

#[test]
fn unknown_fields_versions_states_and_out_of_range_exit_codes_are_rejected() {
    let valid = serde_json::to_value(report()).unwrap();
    for (pointer, value) in [
        ("/schema_version", serde_json::json!(2)),
        ("/termination/kind", serde_json::json!("accepted")),
        ("/termination/code", serde_json::json!(256)),
        ("/usage/cpu_time_us/state", serde_json::json!("estimated")),
        ("/usage/memory_peak_bytes/value", serde_json::json!(-1)),
    ] {
        let mut invalid = valid.clone();
        *invalid.pointer_mut(pointer).unwrap() = value;
        assert!(
            serde_json::from_value::<ExecutionReport>(invalid).is_err(),
            "{pointer}"
        );
    }
    let mut invalid = valid.clone();
    invalid["usage"]["host_path"] = serde_json::json!("/secret");
    assert!(serde_json::from_value::<ExecutionReport>(invalid).is_err());
    let mut invalid = valid;
    invalid["stdout"]
        .as_object_mut()
        .unwrap()
        .remove("observed_bytes");
    assert!(serde_json::from_value::<ExecutionReport>(invalid).is_err());
}

#[test]
fn missing_measurements_are_infrastructure_failures_instead_of_zero_usage() {
    for field in 0..6 {
        let mut result = report();
        match field {
            0 => result.usage.cpu_time_us = unavailable(),
            1 => result.usage.wall_time_us = unavailable(),
            2 => result.usage.memory_peak_bytes = unavailable(),
            3 => {
                result.usage.memory_events = Measurement::Unavailable {
                    reason: MeasurementFailure::CounterReset,
                }
            }
            4 => result.stdout.observed_bytes = unavailable(),
            _ => result.stderr.observed_bytes = unavailable(),
        }
        assert_eq!(
            result.classify(&spec()),
            Ok(ExecutionOutcome::InfrastructureFailure)
        );
        assert!(
            serde_json::to_string(&result)
                .unwrap()
                .contains("unavailable")
        );
    }
}

#[test]
fn sigkill_and_global_oom_counter_alone_do_not_prove_task_memory_limit() {
    let mut result = report();
    result.termination = Termination::Signaled {
        signal: 9,
        core_dumped: false,
    };
    result.usage.memory_events = measured(MemoryEvents {
        max: 0,
        oom: 0,
        oom_kill: 1,
        oom_group_kill: 0,
    });
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::Signaled {
            signal: 9,
            core_dumped: false
        })
    );
    result.limits.push(observation(1000, LimitEvidence::Memory));
    assert_eq!(result.classify(&spec()), Err(ReportError("limit_evidence")));
    result.usage.memory_events = measured(MemoryEvents {
        max: 1,
        oom: 1,
        oom_kill: 1,
        oom_group_kill: 1,
    });
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::LimitExceeded {
            primary: LimitKind::Memory
        })
    );
}

#[test]
fn multiple_limits_use_first_observation_then_stable_tie_break_and_keep_evidence() {
    let mut result = cpu_limited();
    let wall_limit = spec().spec().limits.wall_time_us;
    result.usage.wall_time_us = measured(wall_limit + 2);
    result.limits[0].at_wall_time_us = wall_limit + 1;
    result.limits.push(observation(
        wall_limit,
        LimitEvidence::WallTime {
            observed_us: spec().spec().limits.wall_time_us,
        },
    ));
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::LimitExceeded {
            primary: LimitKind::WallTime
        })
    );
    result.limits[1].at_wall_time_us = wall_limit + 1;
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::LimitExceeded {
            primary: LimitKind::CpuTime
        })
    );
    result.limits.reverse();
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::LimitExceeded {
            primary: LimitKind::CpuTime
        })
    );
    assert_eq!(result.limits.len(), 2);
    assert!(matches!(
        result.termination,
        Termination::Signaled { signal: 9, .. }
    ));
}

#[test]
fn cancellation_wins_before_or_at_limit_but_cannot_replace_an_earlier_limit() {
    let mut result = cpu_limited();
    for (time, expected) in [
        (999, ExecutionOutcome::Cancelled),
        (1000, ExecutionOutcome::Cancelled),
        (
            1001,
            ExecutionOutcome::LimitExceeded {
                primary: LimitKind::CpuTime,
            },
        ),
    ] {
        result.cancelled_at_wall_time_us = Some(time);
        assert_eq!(result.classify(&spec()), Ok(expected));
    }
    result.cancelled_at_wall_time_us = Some(2001);
    assert_eq!(
        result.classify(&spec()),
        Err(ReportError("cancellation_time"))
    );
}

#[test]
fn cleanup_and_wait_failures_cannot_be_reported_as_success_or_hide_observed_limits() {
    let mut result = cpu_limited();
    result.cleanup = CleanupState::Failed;
    assert_eq!(result.classify(&spec()), Err(ReportError("cleanup")));
    result.errors.push(error(ExecutionStage::Cleanup));
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::InfrastructureFailure)
    );
    assert_eq!(result.limits.len(), 1);
    let mut result = report();
    result.termination = Termination::WaitFailed;
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::InfrastructureFailure)
    );
}

#[test]
fn startup_failure_requires_a_stage_error_and_separately_preserves_cleanup_failure() {
    let mut result = report();
    result.termination = Termination::NotStarted;
    result.cleanup = CleanupState::NotNeeded;
    assert_eq!(
        result.classify(&spec()),
        Err(ReportError("missing_start_error"))
    );
    result.errors.push(error(ExecutionStage::Seccomp));
    assert_eq!(result.classify(&spec()), Ok(ExecutionOutcome::NotStarted));
    result.cleanup = CleanupState::Failed;
    result.errors.push(error(ExecutionStage::Cleanup));
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::InfrastructureFailure)
    );
    result.limits.push(observation(
        1,
        LimitEvidence::Processes {
            max_events_delta: 1,
        },
    ));
    assert_eq!(result.classify(&spec()), Err(ReportError("not_started")));
}

#[test]
fn bounded_truncated_output_is_a_lower_bound_and_requires_matching_limit_evidence() {
    let mut result = report();
    let limit = spec().spec().limits.stdout_bytes;
    result.stdout = OutputCapture {
        artifact_id: Some("stdout-001".into()),
        observed_bytes: measured(limit + 1),
        retained_bytes: limit,
        truncated: true,
        reached_eof: false,
    };
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::InfrastructureFailure)
    );
    result.limits.push(observation(
        1000,
        LimitEvidence::Stdout {
            observed_bytes: limit + 1,
        },
    ));
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::LimitExceeded {
            primary: LimitKind::Stdout
        })
    );
    result.stdout.truncated = false;
    assert_eq!(result.classify(&spec()), Err(ReportError("output_capture")));
    result.stdout.truncated = true;
    result.stdout.retained_bytes = limit + 1;
    assert_eq!(result.classify(&spec()), Err(ReportError("output_capture")));
}

#[test]
fn incomplete_streams_and_silent_truncation_are_not_successful_execution() {
    let mut result = report();
    result.stderr.reached_eof = false;
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::InfrastructureFailure)
    );
    result.stderr.truncated = true;
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::InfrastructureFailure)
    );
    result.cancelled_at_wall_time_us = Some(1000);
    assert_eq!(result.classify(&spec()), Ok(ExecutionOutcome::Cancelled));
}

#[test]
fn artifact_exports_must_match_requested_paths_and_cannot_exceed_file_caps() {
    let mut request =
        serde_json::from_str::<ExecutionSpec>(include_str!("fixtures/spec.valid.json")).unwrap();
    request.outputs = vec!["main.out".into()];
    let validated = request.validate(&SpecPolicy::default()).unwrap();
    let mut result = report();
    result.artifacts.push(OutputArtifact {
        path: "main.out".into(),
        artifact_id: "file-001".into(),
        bytes: 7,
    });
    assert!(result.classify(&validated).is_ok());
    for invalid in [
        OutputArtifact {
            path: "../main.out".into(),
            ..result.artifacts[0].clone()
        },
        OutputArtifact {
            artifact_id: "/host/file".into(),
            ..result.artifacts[0].clone()
        },
        OutputArtifact {
            bytes: validated.spec().limits.file_bytes + 1,
            ..result.artifacts[0].clone()
        },
    ] {
        result.artifacts[0] = invalid;
        assert_eq!(result.classify(&validated), Err(ReportError("artifacts")));
        result.artifacts[0] = OutputArtifact {
            path: "main.out".into(),
            artifact_id: "file-001".into(),
            bytes: 7,
        };
    }
    result.artifacts.push(result.artifacts[0].clone());
    assert_eq!(result.classify(&validated), Err(ReportError("artifacts")));
}

#[test]
fn invalid_observation_values_times_duplicates_identity_and_signals_are_rejected() {
    for evidence in [
        LimitEvidence::CpuTime { observed_us: 1 },
        LimitEvidence::WallTime { observed_us: 1 },
        LimitEvidence::Processes {
            max_events_delta: 0,
        },
        LimitEvidence::FileSize { observed_bytes: 0 },
        LimitEvidence::WorkdirSize { observed_bytes: 0 },
        LimitEvidence::Stderr { observed_bytes: 0 },
    ] {
        let mut result = report();
        result.limits.push(observation(1000, evidence));
        assert_eq!(result.classify(&spec()), Err(ReportError("limit_evidence")));
    }
    let mut result = cpu_limited();
    result.limits[0].at_wall_time_us = u64::MAX;
    assert_eq!(result.classify(&spec()), Err(ReportError("limit_evidence")));
    let mut result = cpu_limited();
    result.limits.push(result.limits[0].clone());
    assert_eq!(result.classify(&spec()), Err(ReportError("limit_evidence")));
    result = report();
    result.run_id = "other-run".into();
    assert_eq!(
        result.classify(&spec()),
        Err(ReportError("report_identity_or_size"))
    );
    result = report();
    result.termination = Termination::Signaled {
        signal: 0,
        core_dumped: false,
    };
    assert_eq!(result.classify(&spec()), Err(ReportError("signal")));
}

#[test]
fn resource_denial_and_measured_capacity_evidence_are_classified_without_signal_guessing() {
    let limits = &spec().spec().limits.clone();
    for (evidence, primary) in [
        (
            LimitEvidence::Processes {
                max_events_delta: 1,
            },
            LimitKind::Processes,
        ),
        (
            LimitEvidence::FileSize {
                observed_bytes: limits.file_bytes + 1,
            },
            LimitKind::FileSize,
        ),
        (
            LimitEvidence::WorkdirSize {
                observed_bytes: limits.workdir_bytes + 1,
            },
            LimitKind::WorkdirSize,
        ),
    ] {
        let mut result = report();
        result.limits.push(observation(1000, evidence));
        assert_eq!(
            result.classify(&spec()),
            Ok(ExecutionOutcome::LimitExceeded { primary })
        );
    }
    let mut result = report();
    result.stderr = OutputCapture {
        artifact_id: Some("stderr-001".into()),
        observed_bytes: measured(limits.stderr_bytes + 1),
        retained_bytes: limits.stderr_bytes,
        truncated: true,
        reached_eof: true,
    };
    result.limits.push(observation(
        1000,
        LimitEvidence::Stderr {
            observed_bytes: limits.stderr_bytes + 1,
        },
    ));
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::LimitExceeded {
            primary: LimitKind::Stderr
        })
    );
}

#[test]
fn wall_evidence_cannot_claim_a_threshold_before_its_observation_time() {
    let mut result = report();
    let limit = spec().spec().limits.wall_time_us;
    result.usage.wall_time_us = measured(limit);
    result.limits.push(observation(
        limit - 1,
        LimitEvidence::WallTime { observed_us: limit },
    ));
    assert_eq!(result.classify(&spec()), Err(ReportError("limit_evidence")));
    result.limits[0].at_wall_time_us = limit;
    assert_eq!(
        result.classify(&spec()),
        Ok(ExecutionOutcome::LimitExceeded {
            primary: LimitKind::WallTime
        })
    );
}

#[test]
fn exported_files_together_cannot_exceed_workdir_budget() {
    let mut request =
        serde_json::from_str::<ExecutionSpec>(include_str!("fixtures/spec.valid.json")).unwrap();
    request.outputs = (0..5).map(|index| format!("file-{index}")).collect();
    let validated = request.validate(&SpecPolicy::default()).unwrap();
    let mut result = report();
    for index in 0..5 {
        result.artifacts.push(OutputArtifact {
            path: format!("file-{index}"),
            artifact_id: format!("artifact-{index}"),
            bytes: validated.spec().limits.file_bytes,
        });
    }
    assert_eq!(result.classify(&validated), Err(ReportError("artifacts")));
    result.artifacts.pop();
    assert_eq!(
        result.classify(&validated),
        Ok(ExecutionOutcome::Exited { code: 0 })
    );
}
