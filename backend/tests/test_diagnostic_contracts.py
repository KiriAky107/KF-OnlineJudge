import json
from pathlib import Path

import pytest

from kf_oj.contracts.diagnostic import DiagnosticResult, DiagnosticRun
from kf_oj.contracts.diagnostic_batch import DiagnosticBoundaryReport
from kf_oj.contracts.judging import decode_wire

_FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/diagnostics-v1"


def test_shared_diagnostic_wire_binds_the_active_attempt_and_rejects_unsafe_result():
    run = decode_wire(DiagnosticRun, (_FIXTURES / "diagnostic-run.valid.json").read_bytes())
    result = decode_wire(
        DiagnosticResult, (_FIXTURES / "diagnostic-result.valid.json").read_bytes()
    )
    assert run.binding == result.binding and run.sample.case_id == run.binding.execution_id
    assert result.observation.o0 == "accepted"
    for model, name in (
        (DiagnosticRun, "diagnostic-run.invalid-budget.json"),
        (DiagnosticResult, "diagnostic-result.invalid-cleanup.json"),
    ):
        with pytest.raises(ValueError):
            decode_wire(model, (_FIXTURES / name).read_bytes())


def test_shared_ubsan_wire_supports_one_pass_and_structured_findings():
    run_data = (_FIXTURES / "diagnostic-run.ubsan.json").read_bytes()
    result_data = (_FIXTURES / "diagnostic-result.ubsan.json").read_bytes()
    run = decode_wire(DiagnosticRun, run_data)
    result = decode_wire(DiagnosticResult, result_data)
    assert run.mode == "ubsan" and run.binding == result.binding
    assert result.observation is None and result.ubsan.finding.line == 9

    bad_result = json.loads(result_data)
    bad_result["ubsan"]["finding"]["raw"] = "untrusted stderr"
    with pytest.raises(ValueError):
        decode_wire(DiagnosticResult, json.dumps(bad_result).encode())
    with pytest.raises(ValueError):
        decode_wire(DiagnosticRun, run_data.replace(b'"ubsan"', b'"optimization"'))


def test_boundary_report_only_contains_distinct_measured_cases():
    payload = (_FIXTURES / "diagnostic-boundary.valid.json").read_bytes()
    report = decode_wire(DiagnosticBoundaryReport, payload)
    assert report.cases[1].outcome == "time_limit"
    assert report.cases[1].memory_peak_bytes == 1667072
    invalid = json.loads(payload)
    invalid["cases"][1]["case_id"] = invalid["cases"][0]["case_id"]
    with pytest.raises(ValueError):
        decode_wire(DiagnosticBoundaryReport, json.dumps(invalid).encode())
    invalid = json.loads(payload)
    invalid["cases"][1]["cpu_time_us"] = None
    with pytest.raises(ValueError):
        decode_wire(DiagnosticBoundaryReport, json.dumps(invalid).encode())
