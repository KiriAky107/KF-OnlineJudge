"""Contract checks only; upload/parser/security evidence requires the independent VM."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kf_oj.contracts.admin_problems import (
    CandidatePreview,
    CreateProblemImport,
    LegacyImportBudget,
    ProblemImportCapabilities,
    ProblemImportDetail,
)
from kf_oj.contracts.judging import AdmissionPolicy
from kf_oj.modules.problems.import_format import ImportEnvelope
from kf_oj.modules.problems.web_format import decode_web_declaration

ROOT = Path(__file__).resolve().parents[2]


def legacy_manifest():
    policy = AdmissionPolicy.model_validate_json(
        (ROOT / "tests/fixtures/contracts/judge-admission.valid.json").read_bytes()
    )
    return {
        "schema_version": 1,
        "slug": "web-fixture",
        "title": "Public",
        "statement_file": "statement.md",
        "languages": ["c17", "cpp20"],
        "toolchain": policy.toolchain.model_dump(mode="json"),
        "comparison": "exact_bytes",
        "compile_limits": policy.compile_ceiling.model_dump(),
        "run_limits": policy.run_ceiling.model_dump(),
        "job_wall_time_us": 60000000,
        "cases": [{"key": "binary", "input_file": "hidden.in", "expected_file": "hidden.out"}],
        "samples": [{"input_file": "sample.in", "output_file": "sample.out"}],
    }


def test_web_declaration_separates_public_data_and_preserves_local_format():
    source = legacy_manifest()
    original = copy.deepcopy(source)
    declared = decode_web_declaration(json.dumps(source).encode())
    assert declared.members == frozenset(
        ("problem.json", "statement.md", "hidden.in", "hidden.out", "sample.in", "sample.out")
    )
    assert declared.manifest.model_dump(mode="json") == original
    assert ImportEnvelope.model_validate(source).root == declared.manifest
    for mutation in (
        lambda x: x["samples"][0].update(input_file="hidden.in"),
        lambda x: x.update(statement_file="problem.json"),
        lambda x: x["cases"][0].update(input_file="Statement.md"),
        lambda x: x["cases"][0].update(input_file="hidden.zip"),
        lambda x: x.update(languages=["rust"]),
        lambda x: x.update(download_url="https://example.invalid/private"),
    ):
        value = copy.deepcopy(source)
        mutation(value)
        with pytest.raises(ValueError):
            decode_web_declaration(json.dumps(value).encode())
    with pytest.raises(ValueError):
        decode_web_declaration(
            json.dumps(source)
            .replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
            .encode()
        )


def test_import_operation_key_and_preview_have_no_private_payload_fields():
    operation = {
        "schema_version": 1,
        "operation_id": str(uuid4()),
        "archive_digest": "a" * 64,
        "archive_bytes": 64 * 1024**2,
    }
    assert CreateProblemImport.model_validate(operation).archive_bytes == 64 * 1024**2
    for field, value in (
        ("archive_bytes", True),
        ("archive_bytes", 64 * 1024**2 + 1),
        ("url", "https://example.invalid"),
        ("role", "admin"),
    ):
        with pytest.raises(ValidationError):
            CreateProblemImport.model_validate(operation | {field: value})
    manifest = ImportEnvelope.model_validate(legacy_manifest()).root
    preview = CandidatePreview(
        candidate_id=uuid4(),
        slug=manifest.slug,
        title=manifest.title,
        statement="Public only",
        samples=(),
        judging_schema_version=1,
        languages=manifest.languages,
        legacy_budget=LegacyImportBudget(
            compile_limits=manifest.compile_limits,
            run_limits=manifest.run_limits,
            job_wall_time_us=manifest.job_wall_time_us,
        ),
        case_count=1,
    )
    now = datetime.now(UTC)
    response = ProblemImportDetail(
        operation_id=uuid4(),
        state="ready",
        expected_archive_digest="a" * 64,
        archive_bytes=100,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=1),
        failure_code=None,
        candidate_id=preview.candidate_id,
        published_version_id=None,
        candidate=preview,
    )
    assert set(response.candidate.model_dump()) == {
        "candidate_id",
        "slug",
        "title",
        "statement",
        "samples",
        "judging_schema_version",
        "languages",
        "legacy_budget",
        "language_configurations",
        "case_count",
    }
    with pytest.raises(ValidationError):
        CandidatePreview.model_validate(preview.model_dump() | {"input_digest": "a" * 64})
    capabilities = ProblemImportCapabilities(uploads_available=False, validation_available=False)
    assert (
        capabilities.limits.global_concurrency == 1 and capabilities.limits.owner_candidates == 10
    )


def test_standalone_import_worker_resolves_all_referenced_metadata():
    # A separate trusted interpreter avoids accidental model registration by API test imports.
    import subprocess
    import sys

    script = """
import kf_oj.workers.problem_import
from kf_oj.infra.db.base import Base
for table in Base.metadata.tables.values():
 for foreign in table.foreign_keys:
  assert foreign.column.table is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
