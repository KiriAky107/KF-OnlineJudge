"""Bounded anonymous aggregates; raw measurements never pass through JS floats."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, StrictBool, model_validator

from kf_oj.contracts.judging import I64_MAX, Digest, Identifier, Version, WireModel
from kf_oj.contracts.judging_v2 import Language

MAX_PERFORMANCE_SAMPLES = 10_000
MIN_PERFORMANCE_SAMPLES = 5
MAX_PERFORMANCE_BUCKETS = 64


def bounded_decimal(value: str) -> str:
    if int(value) > I64_MAX:
        raise ValueError("measurement exceeds signed storage range")
    return value


DecimalMeasurement = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=19, pattern=r"^(0|[1-9][0-9]*)$"),
    AfterValidator(bounded_decimal),
]
SampleCount = Annotated[int, Field(strict=True, ge=0, le=MAX_PERFORMANCE_SAMPLES)]
UnavailableReason = Literal[
    "not_accepted", "measurement_missing", "environment_unverified", "insufficient_samples"
]


class PerformanceBucket(WireModel):
    start: DecimalMeasurement
    end: DecimalMeasurement
    upper_inclusive: StrictBool
    count: SampleCount

    @model_validator(mode="after")
    def interval(self) -> Self:
        if int(self.end) <= int(self.start):
            raise ValueError("empty or reversed interval")
        return self


class PerformanceMetric(WireModel):
    unit: Literal["cpu_microseconds", "bytes"]
    current: DecimalMeasurement | None
    available: StrictBool
    reason: UnavailableReason | None
    sample_count: SampleCount
    window_truncated: StrictBool
    oldest_sample_at: datetime | None
    # These counts are computed against the original integers, never rounded bins.
    strictly_slower_count: SampleCount | None
    tied_count: SampleCount | None
    buckets: Annotated[tuple[PerformanceBucket, ...], Field(max_length=MAX_PERFORMANCE_BUCKETS)]

    @model_validator(mode="after")
    def aggregate(self) -> Self:
        if self.oldest_sample_at is not None and self.oldest_sample_at.utcoffset() is None:
            raise ValueError("sample timestamp must have a timezone")
        if self.available:
            if (
                self.reason is not None
                or self.current is None
                or self.sample_count < MIN_PERFORMANCE_SAMPLES
                or self.strictly_slower_count is None
                or self.tied_count is None
                or self.oldest_sample_at is None
                or not self.buckets
            ):
                raise ValueError("incomplete available aggregate")
            if self.strictly_slower_count + self.tied_count > self.sample_count:
                raise ValueError("comparison counts exceed the cohort")
            if sum(b.count for b in self.buckets) != self.sample_count:
                raise ValueError("histogram loses samples")
            if int(self.buckets[0].start) != 0:
                raise ValueError("histogram must include zero")
            for index, bucket in enumerate(self.buckets):
                if bucket.upper_inclusive != (index == len(self.buckets) - 1):
                    raise ValueError("only the last interval includes its upper endpoint")
                if index and self.buckets[index - 1].end != bucket.start:
                    raise ValueError("histogram intervals must be contiguous")
        elif (
            self.reason is None
            or self.buckets
            or self.strictly_slower_count is not None
            or self.tied_count is not None
        ):
            raise ValueError("unavailable aggregate cannot expose comparison data")
        if self.reason == "measurement_missing" and self.current is not None:
            raise ValueError("missing measurement cannot claim a value")
        if self.reason == "insufficient_samples" and (
            self.sample_count >= MIN_PERFORMANCE_SAMPLES or self.current is None
        ):
            raise ValueError("inconsistent insufficient cohort")
        return self


class SubmissionPerformance(WireModel):
    schema_version: Version = 1
    kind: Literal["judge.performance"] = "judge.performance"
    submission_id: Identifier
    problem_version_id: Identifier
    language: Language
    as_of: datetime
    # Opaque trusted comparison identity, not a worker name, path or credential.
    comparison_group: Digest | None
    window_limit: Literal[10_000] = MAX_PERFORMANCE_SAMPLES
    minimum_samples: Literal[5] = MIN_PERFORMANCE_SAMPLES
    cpu: PerformanceMetric
    memory: PerformanceMetric

    @model_validator(mode="after")
    def snapshot(self) -> Self:
        if self.as_of.utcoffset() is None:
            raise ValueError("snapshot timestamp must have a timezone")
        if self.cpu.unit != "cpu_microseconds" or self.memory.unit != "bytes":
            raise ValueError("metric unit mismatch")
        for metric in (self.cpu, self.memory):
            if metric.available and self.comparison_group is None:
                raise ValueError("comparison requires trusted environment binding")
            if metric.oldest_sample_at is not None and metric.oldest_sample_at > self.as_of:
                raise ValueError("sample is newer than the snapshot")
        return self
