"""Owned code execution with user-provided input, never hidden judging cases."""

from datetime import datetime
from typing import Annotated

from pydantic import Field, model_validator

from kf_oj.contracts.judging import Identifier, WireModel
from kf_oj.contracts.judging_v2 import Language
from kf_oj.contracts.submissions import CreateSubmission, EvaluationState


class CreateSelfTest(CreateSubmission):
    stdin: Annotated[str, Field(max_length=65536, repr=False)] = ""
    public_sample_ordinal: Annotated[int, Field(strict=True, ge=0, le=65535)] | None = None
    diagnostic_request_id: Identifier | None = None

    @model_validator(mode="after")
    def input_bytes(self):
        if len(self.stdin.encode("utf-8")) > 65536:
            raise ValueError("stdin exceeds byte policy")
        if self.diagnostic_request_id is not None and self.public_sample_ordinal is None:
            raise ValueError("diagnostic reference requires an advertised public sample")
        return self


class SelfTestDetail(WireModel):
    self_test_id: Identifier
    problem_version_id: Identifier
    language: Language
    created_at: datetime
    state: EvaluationState
    verdict: str | None
    failure_code: str | None
    stdout: str | None = None
    stderr: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    compile_diagnostics: str | None = None
    compile_diagnostics_truncated: bool = False
    cpu_time_us: int | None = None
    memory_peak_bytes: int | None = None
