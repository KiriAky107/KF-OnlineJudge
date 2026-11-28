use kf_contracts::{
    diagnostic::{
        BoundaryOutcome, DiagnosticBoundaryReport, DiagnosticMode, DiagnosticResult, DiagnosticRun,
        UbsanCategory,
    },
    judging::{Verdict, decode_wire},
};

#[test]
fn shared_diagnostic_wire_binds_the_active_attempt_and_rejects_unsafe_result() {
    let run: DiagnosticRun = decode_wire(include_bytes!(
        "../../../../tests/fixtures/diagnostics-v1/diagnostic-run.valid.json"
    ))
    .unwrap();
    let result: DiagnosticResult = decode_wire(include_bytes!(
        "../../../../tests/fixtures/diagnostics-v1/diagnostic-result.valid.json"
    ))
    .unwrap();
    assert_eq!(run.binding, result.binding);
    assert_eq!(run.sample.case_id, run.binding.execution_id);
    assert_eq!(result.observation.unwrap().o0, Some(Verdict::Accepted));
    assert!(
        decode_wire::<DiagnosticRun>(include_bytes!(
            "../../../../tests/fixtures/diagnostics-v1/diagnostic-run.invalid-budget.json"
        ))
        .is_err()
    );
    assert!(
        decode_wire::<DiagnosticResult>(include_bytes!(
            "../../../../tests/fixtures/diagnostics-v1/diagnostic-result.invalid-cleanup.json"
        ))
        .is_err()
    );
}

#[test]
fn shared_ubsan_wire_supports_one_pass_and_structured_findings() {
    let run_data =
        include_bytes!("../../../../tests/fixtures/diagnostics-v1/diagnostic-run.ubsan.json");
    let result_data =
        include_bytes!("../../../../tests/fixtures/diagnostics-v1/diagnostic-result.ubsan.json");
    let run = decode_wire::<DiagnosticRun>(run_data).unwrap();
    let result = decode_wire::<DiagnosticResult>(result_data).unwrap();
    assert_eq!(run.mode, DiagnosticMode::Ubsan);
    assert_eq!(result.binding, run.binding);
    assert!(result.observation.is_none());
    let finding = result.ubsan.unwrap().finding.unwrap();
    assert_eq!(finding.category, UbsanCategory::SignedIntegerOverflow);
    assert_eq!((finding.line, finding.column), (9, 33));
    let mut bad: serde_json::Value = serde_json::from_slice(result_data).unwrap();
    bad["ubsan"]["finding"]["raw"] = "untrusted stderr".into();
    assert!(decode_wire::<DiagnosticResult>(&serde_json::to_vec(&bad).unwrap()).is_err());
    let mut bad: serde_json::Value = serde_json::from_slice(run_data).unwrap();
    bad["mode"] = "optimization".into();
    assert!(decode_wire::<DiagnosticRun>(&serde_json::to_vec(&bad).unwrap()).is_err());
}

#[test]
fn boundary_report_only_contains_distinct_measured_cases() {
    let payload =
        include_bytes!("../../../../tests/fixtures/diagnostics-v1/diagnostic-boundary.valid.json");
    let report: DiagnosticBoundaryReport = decode_wire(payload).unwrap();
    assert_eq!(report.cases[1].outcome, BoundaryOutcome::TimeLimit);
    assert_eq!(report.cases[1].memory_peak_bytes, 1667072);
    let mut invalid: serde_json::Value = serde_json::from_slice(payload).unwrap();
    invalid["cases"][1]["case_id"] = invalid["cases"][0]["case_id"].clone();
    assert!(
        decode_wire::<DiagnosticBoundaryReport>(&serde_json::to_vec(&invalid).unwrap()).is_err()
    );
    let mut invalid: serde_json::Value = serde_json::from_slice(payload).unwrap();
    invalid["cases"][1]["cpu_time_us"] = serde_json::Value::Null;
    assert!(
        decode_wire::<DiagnosticBoundaryReport>(&serde_json::to_vec(&invalid).unwrap()).is_err()
    );
}
