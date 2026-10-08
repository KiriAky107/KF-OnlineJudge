"""Authorized immutable version projection; hidden references stay out of public DTOs."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from kf_oj.contracts.judging import Language, RunLimits, TestCase, Toolchain


class ProblemUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class JudgingVersion:
    version_id: UUID
    problem_id: UUID
    languages: tuple[Language, ...]
    toolchain: Toolchain
    compile_limits: RunLimits
    run_limits: RunLimits
    job_wall_time_us: int
    cases: tuple[TestCase, ...]


class ProblemPort(Protocol):
    async def for_submission(self, session, version_id: UUID) -> JudgingVersion: ...
