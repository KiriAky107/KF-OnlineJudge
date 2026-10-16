use kf_sandbox::launcher::lifecycle::{Event, Lifecycle, State, StopReason};
use kf_sandbox::outcome::{ErrorCode, ExecutionStage, LimitKind};
use kf_sandbox::spec::{ExecutionSpec, SpecPolicy};

const STARTUP: [Event; 8] = [
    Event::BeginPreparation,
    Event::CgroupConfirmed,
    Event::NamespacesConfirmed,
    Event::RootConfirmed,
    Event::IdentityConfirmed,
    Event::FilterConfirmed,
    Event::ExecGateReleased,
    Event::ExecConfirmed,
];

fn lifecycle(prefix: usize) -> Lifecycle {
    let spec: ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    let spec = spec.validate(&SpecPolicy::default()).unwrap();
    let mut task = Lifecycle::new(&spec);
    assert_eq!(task.run_id(), spec.spec().run_id);
    for event in &STARTUP[..prefix] {
        task.apply(*event).unwrap();
    }
    task
}

fn settle(task: &mut Lifecycle) {
    task.apply(Event::TreeReaped).unwrap();
    task.apply(Event::CollectionFinished).unwrap();
    task.apply(Event::CleanupSucceeded).unwrap();
    assert_eq!(task.state(), State::Finished);
}

#[test]
fn initialization_exec_confirmation_and_tree_reaping_are_distinct() {
    let mut task = lifecycle(6);
    assert_eq!(task.state(), State::FilterReady);
    assert!(!task.exec_released());
    task.apply(Event::ExecGateReleased).unwrap();
    assert_eq!(task.state(), State::ExecReleased);
    assert!(task.exec_released());
    task.apply(Event::ExecConfirmed).unwrap();
    assert_eq!(task.state(), State::Waiting);
    task.apply(Event::MainExited).unwrap();
    assert_eq!(task.state(), State::Terminating);
    assert_eq!(task.stop_reason(), Some(StopReason::MainExited));
    settle(&mut task);
    assert!(task.failure().is_none());
    assert!(task.cleanup_failure().is_none());
}

#[test]
fn every_incomplete_startup_prefix_rejects_and_latches_early_exec() {
    for prefix in 0..6 {
        let mut task = lifecycle(prefix);
        assert!(task.apply(Event::ExecGateReleased).is_err());
        assert_eq!(task.state(), State::Terminating);
        assert_eq!(task.failure().unwrap().code, ErrorCode::ProtocolFailed);
        // Ignoring the error and submitting the omitted confirmations cannot
        // revive this attempt or open its exec gate.
        for event in &STARTUP[prefix..] {
            assert!(task.apply(*event).is_err());
        }
        assert!(!task.exec_released());
        settle(&mut task);
    }
}

#[test]
fn duplicated_or_out_of_order_security_confirmation_aborts() {
    for (prefix, event) in [
        (1, Event::FilterConfirmed),
        (2, Event::CgroupConfirmed),
        (3, Event::IdentityConfirmed),
        (4, Event::RootConfirmed),
        (6, Event::FilterConfirmed),
    ] {
        let mut task = lifecycle(prefix);
        assert!(task.apply(event).is_err());
        assert_eq!(task.state(), State::Terminating);
        assert_eq!(task.stop_reason(), Some(StopReason::Failure));
        assert!(!task.exec_released());
    }
}

#[test]
fn faults_preserve_the_outstanding_stage_and_cannot_restart_execution() {
    let stages = [
        ExecutionStage::Preparation,
        ExecutionStage::Cgroup,
        ExecutionStage::Namespaces,
        ExecutionStage::RootFilesystem,
        ExecutionStage::Identity,
        ExecutionStage::Seccomp,
        ExecutionStage::Exec,
        ExecutionStage::Exec,
        ExecutionStage::Wait,
    ];
    for (prefix, stage) in stages.into_iter().enumerate() {
        let mut task = lifecycle(prefix);
        task.apply(Event::Failed {
            code: ErrorCode::OperationFailed,
            errno: Some(13),
        })
        .unwrap();
        assert_eq!(task.state(), State::Terminating);
        assert_eq!(task.failure().unwrap().stage, stage);
        assert_eq!(task.failure().unwrap().errno, Some(13));
        assert!(task.apply(Event::ExecGateReleased).is_err());
        assert_eq!(task.failure().unwrap().code, ErrorCode::OperationFailed);
        settle(&mut task);
    }
}

#[test]
fn cancellation_at_every_pre_exec_boundary_is_idempotent_and_never_execs() {
    for prefix in 0..=6 {
        let mut task = lifecycle(prefix);
        task.apply(Event::Cancel).unwrap();
        task.apply(Event::Cancel).unwrap();
        assert_eq!(task.state(), State::Terminating);
        assert!(task.cancel_requested());
        assert_eq!(task.stop_reason(), Some(StopReason::Cancelled));
        assert!(!task.exec_released());
        settle(&mut task);
        assert!(task.failure().is_none());
    }
}

#[test]
fn cancellation_after_release_still_requires_tree_reaping() {
    for prefix in [7, 8] {
        let mut task = lifecycle(prefix);
        task.apply(Event::Cancel).unwrap();
        assert!(task.exec_released());
        assert_eq!(task.state(), State::Terminating);
        settle(&mut task);
    }
}

#[test]
fn main_exit_does_not_authorize_early_accounting_or_cleanup() {
    for event in [Event::CollectionFinished, Event::CleanupSucceeded] {
        let mut task = lifecycle(8);
        task.apply(Event::MainExited).unwrap();
        assert!(task.apply(event).is_err());
        assert_eq!(task.state(), State::Terminating);
        assert_eq!(task.failure().unwrap().stage, ExecutionStage::Wait);
        settle(&mut task);
    }
}

#[test]
fn cleanup_fault_is_independent_and_retry_does_not_erase_diagnostics() {
    let mut task = lifecycle(3);
    task.apply(Event::Failed {
        code: ErrorCode::OperationFailed,
        errno: Some(1),
    })
    .unwrap();
    task.apply(Event::TreeReaped).unwrap();
    task.apply(Event::CollectionFinished).unwrap();
    task.apply(Event::CleanupFailed { errno: Some(16) })
        .unwrap();
    assert_eq!(task.state(), State::Cleaning);
    assert_eq!(
        task.failure().unwrap().stage,
        ExecutionStage::RootFilesystem
    );
    assert_eq!(task.failure().unwrap().errno, Some(1));
    assert_eq!(
        task.cleanup_failure().unwrap().stage,
        ExecutionStage::Cleanup
    );
    assert_eq!(
        task.cleanup_failure().unwrap().code,
        ErrorCode::CleanupIncomplete
    );
    assert_eq!(task.cleanup_failure().unwrap().errno, Some(16));
    task.apply(Event::CleanupSucceeded).unwrap();
    assert_eq!(task.state(), State::Finished);
    assert!(task.cleanup_failure().is_some());
}

#[test]
fn accounting_faults_cannot_restore_live_task_phases_or_skip_collection() {
    let mut task = lifecycle(8);
    task.apply(Event::MainExited).unwrap();
    task.apply(Event::TreeReaped).unwrap();
    task.apply(Event::Failed {
        code: ErrorCode::MeasurementUnavailable,
        errno: None,
    })
    .unwrap();
    task.apply(Event::Cancel).unwrap();
    assert_eq!(task.state(), State::Accounting);
    assert_eq!(task.failure().unwrap().stage, ExecutionStage::Accounting);
    assert!(task.cancel_requested());
    assert_eq!(task.stop_reason(), Some(StopReason::MainExited));
    assert!(task.apply(Event::CleanupSucceeded).is_err());
    assert_eq!(task.state(), State::Accounting);
    task.apply(Event::CollectionFinished).unwrap();
    task.apply(Event::CleanupSucceeded).unwrap();
}

#[test]
fn malformed_errno_becomes_a_protocol_fault_without_storing_invalid_values() {
    for errno in [0, 4096, u16::MAX] {
        let mut task = lifecycle(1);
        assert!(
            task.apply(Event::Failed {
                code: ErrorCode::OperationFailed,
                errno: Some(errno),
            })
            .is_err()
        );
        assert_eq!(task.state(), State::Terminating);
        assert_eq!(task.failure().unwrap().code, ErrorCode::ProtocolFailed);
        assert_eq!(task.failure().unwrap().errno, None);
    }
}

#[test]
fn limit_stop_and_late_cancel_preserve_the_first_stop_reason() {
    let mut task = lifecycle(8);
    task.apply(Event::LimitObserved(LimitKind::CpuTime))
        .unwrap();
    task.apply(Event::Cancel).unwrap();
    task.apply(Event::LimitObserved(LimitKind::Memory)).unwrap();
    assert_eq!(
        task.stop_reason(),
        Some(StopReason::Limit(LimitKind::CpuTime))
    );
    assert!(task.cancel_requested());
    settle(&mut task);
}

#[test]
fn finished_tasks_reject_all_events_without_reopening_or_mutating_results() {
    let mut task = lifecycle(0);
    task.apply(Event::Cancel).unwrap();
    settle(&mut task);
    for event in STARTUP.into_iter().chain([
        Event::Cancel,
        Event::LimitObserved(LimitKind::Memory),
        Event::CleanupFailed { errno: Some(1) },
        Event::CleanupSucceeded,
    ]) {
        assert!(task.apply(event).is_err());
        assert_eq!(task.state(), State::Finished);
        assert_eq!(task.stop_reason(), Some(StopReason::Cancelled));
        assert!(!task.exec_released());
        assert!(task.failure().is_none());
        assert!(task.cleanup_failure().is_none());
    }
}
