"""Validate the source and data bindings before admitting a batch for isolated replay."""

import re
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256

from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.diagnostic import MAX_SAMPLE_BYTES
from kf_oj.contracts.diagnostic_batch import (
    MAX_BATCH_MANIFEST_BYTES,
    BatchFile,
    DiagnosticBatchManifest,
    ModelReferenceCandidate,
    ReviewedReference,
)
from kf_oj.infra.artifacts.repository import ArtifactRepository
from kf_oj.infra.artifacts.store import ArtifactError, ArtifactStore
from kf_oj.modules.problems.public import ProblemPort, ProblemUnavailable
from kf_oj.modules.submissions.public import reviewed_diagnostic_reference

from .diagnostic_inputs import DiagnosticInputsUnavailable
from .diagnostic_runtime import diagnostic_batch_configuration, diagnostic_checker_configuration


@dataclass(frozen=True, slots=True)
class DiagnosticInputCheck:
    configuration: v2.LanguageConfiguration
    source: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class DiagnosticBatch:
    manifest: DiagnosticBatchManifest
    configuration: v2.LanguageConfiguration
    manifest_sha256: str
    cases: tuple[tuple[bytes, bytes | None], ...] = field(repr=False)
    reference_source: bytes | None = field(repr=False)
    input_check: DiagnosticInputCheck | None = None

    def receipt(self) -> dict:
        return {
            "schema_version": 1,
            "problem_version_id": str(self.manifest.problem_version_id),
            "purpose": self.manifest.purpose,
            "manifest_sha256": self.manifest_sha256,
            "runtime_sha256": sha256(self.configuration.model_dump_json().encode()).hexdigest(),
            "state": "reference_pending"
            if isinstance(self.manifest.reference, ModelReferenceCandidate)
            else "ready_for_replay",
        }


def draft_counted_list_reduction(batch: DiagnosticBatch) -> dict:
    """Propose smaller independent inputs for the declared counted-list format."""
    manifest = batch.manifest
    if (
        manifest.generation.recipe != "counted-i64-list-v1"
        or not isinstance(manifest.reference, ReviewedReference)
        or batch.input_check is None
    ):
        raise DiagnosticInputsUnavailable("reduction generator requires an approved list recipe")
    original, _ = batch.cases[0]
    original_ref = manifest.cases[0].input.artifact
    if ArtifactStore.reference(original) != original_ref:
        raise DiagnosticInputsUnavailable("reduction source differs from its approved input")
    lines = original.splitlines()
    if len(lines) != 2 or not re.fullmatch(rb"[1-9][0-9]{0,3}", lines[0]):
        raise DiagnosticInputsUnavailable("invalid counted-list input")
    values = lines[1].split()
    if not len(values) == int(lines[0]) <= 4096 or any(
        not re.fullmatch(rb"-?[0-9]{1,19}", value) or not -(2**63) <= int(value) < 2**63
        for value in values
    ):
        raise DiagnosticInputsUnavailable("invalid counted-list input")
    midpoint = len(values) // 2
    proposals = set()
    if midpoint:
        for start in (0, (len(values) - midpoint) // 2, len(values) - midpoint):
            subset = values[start : start + midpoint]
            compact = str(len(subset)).encode() + b"\n" + b" ".join(subset) + b"\n"
            if len(compact) < len(original):
                proposals.add(compact)
    ordered = sorted(proposals, key=lambda data: (len(data), sha256(data).hexdigest()))
    return {
        "schema_version": 1,
        "state": "verification_pending" if ordered else "no_smaller_proposal",
        "problem_version_id": str(manifest.problem_version_id),
        "manifest_sha256": batch.manifest_sha256,
        "original_input_sha256": original_ref.digest,
        "generation_revision_sha256": manifest.generation.revision.artifact.digest,
        "candidates": [
            {
                "artifact": ArtifactStore.reference(data).model_dump(mode="json"),
                "input_base64": b64encode(data).decode("ascii"),
            }
            for data in ordered
        ],
    }


async def resolve_diagnostic_batch(
    session: AsyncSession,
    problems: ProblemPort,
    artifacts: ArtifactRepository,
    admission: v2.NodeAdmission,
    manifest_bytes: bytes,
    manifest_sha256: str,
    read_file: Callable[[str, int], bytes],
) -> DiagnosticBatch:
    """Check an operator-pinned manifest and capture the exact checked bytes."""
    if (
        len(manifest_bytes) > MAX_BATCH_MANIFEST_BYTES
        or sha256(manifest_bytes).hexdigest() != manifest_sha256
    ):
        raise DiagnosticInputsUnavailable("diagnostic batch manifest differs from the operator pin")
    manifest = v1.decode_wire(DiagnosticBatchManifest, manifest_bytes)
    try:
        version = await problems.for_self_test(session, manifest.problem_version_id)
    except ProblemUnavailable:
        raise DiagnosticInputsUnavailable("diagnostic batch version unavailable") from None

    reference = manifest.reference
    checker = manifest.input_checker
    members = tuple(
        file
        for case in manifest.cases
        for file in (case.input, case.independent_expected)
        if file is not None
    )
    configuration = diagnostic_batch_configuration(
        version,
        manifest.language,
        admission,
        purpose=manifest.purpose,
        case_count=len(manifest.cases),
        package_bytes=(
            v1.MAX_SOURCE_BYTES
            + (reference.source.bytes if reference is not None else 0)
            + (checker.source.artifact.bytes if checker is not None else 0)
            + (1 + int(manifest.reduction is not None) + int(manifest.searches_reduction))
            * sum(member.artifact.bytes for member in members)
        ),
        expected_bytes=max(
            (
                case.independent_expected.artifact.bytes
                for case in manifest.cases
                if case.independent_expected is not None
            ),
            default=0,
        ),
        repeat_candidate=manifest.reduction is not None,
        search_reduction=manifest.searches_reduction,
        check_input=checker is not None,
    )

    def checked_file(member: BatchFile) -> bytes:
        if member.artifact.bytes > MAX_SAMPLE_BYTES:
            raise DiagnosticInputsUnavailable("diagnostic batch file exceeds the byte limit")
        payload = read_file(member.name, member.artifact.bytes)
        if (
            len(payload) != member.artifact.bytes
            or sha256(payload).hexdigest() != member.artifact.digest
        ):
            raise DiagnosticInputsUnavailable("diagnostic batch file differs from its pin")
        return payload

    checked_file(manifest.generation.revision)
    if manifest.oracle is not None:
        checked_file(manifest.oracle.revision)
    input_check = None
    if checker is not None:
        checked_file(checker.constraints_revision)
        input_check = DiagnosticInputCheck(
            diagnostic_checker_configuration(
                configuration,
                manifest.purpose,
                len(manifest.cases),
                repeat_candidate=manifest.reduction is not None,
                search_reduction=manifest.searches_reduction,
            ),
            checked_file(checker.source),
        )
    cases = tuple(
        (
            checked_file(case.input),
            checked_file(case.independent_expected) if case.independent_expected else None,
        )
        for case in manifest.cases
    )
    reference_source = None
    if isinstance(reference, ReviewedReference):
        if not await reviewed_diagnostic_reference(
            session,
            submission_id=reference.submission_id,
            evaluation_id=reference.evaluation_id,
            version_id=manifest.problem_version_id,
            language=manifest.language,
            accepted_report_sha256=reference.accepted_report_sha256,
            source=reference.source,
        ):
            raise DiagnosticInputsUnavailable("reviewed reference evidence unavailable")
        try:
            reference_source = await artifacts.read(session, reference.source)
        except ArtifactError:
            raise DiagnosticInputsUnavailable("reviewed reference source unavailable") from None

    return DiagnosticBatch(
        manifest, configuration, manifest_sha256, cases, reference_source, input_check
    )
