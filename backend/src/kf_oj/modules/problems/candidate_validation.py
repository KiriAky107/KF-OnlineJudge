"""Pure data validation shared by the bounded child and fenced candidate transaction."""

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4, uuid5

from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.admin_problems import CandidatePreview, LegacyImportBudget
from kf_oj.contracts.judging import MAX_SOURCE_BYTES, ArtifactReference, TestCase
from kf_oj.contracts.problems import PublicLanguageConfiguration
from kf_oj.modules.problems.import_format import ImportEnvelope, ProblemImport
from kf_oj.modules.problems.public import JudgingVersion


@dataclass(frozen=True)
class ValidatedCandidate:
    manifest: dict
    preview: CandidatePreview
    references: dict[str, ArtifactReference]

    @property
    def digest(self):
        return hashlib.sha256(
            json.dumps(
                self.manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode()
        ).hexdigest()


def validate_candidate(manifest_data, preview_data, references, policy) -> ValidatedCandidate:
    manifest = ImportEnvelope.model_validate(manifest_data).root
    preview = CandidatePreview.model_validate(preview_data)
    refs = {k: ArtifactReference.model_validate(v) for k, v in references.items()}
    legacy = isinstance(manifest, ProblemImport)
    if (
        preview.slug != manifest.slug
        or preview.title != manifest.title
        or preview.case_count != len(manifest.cases)
        or preview.judging_schema_version != manifest.schema_version
        or tuple(v.value for v in preview.languages) != tuple(v.value for v in manifest.languages)
    ):
        raise ValueError("invalid candidate projection")
    expected_budget = (
        LegacyImportBudget(
            compile_limits=manifest.compile_limits,
            run_limits=manifest.run_limits,
            job_wall_time_us=manifest.job_wall_time_us,
        )
        if legacy
        else None
    )
    configs = (
        ()
        if legacy
        else tuple(
            PublicLanguageConfiguration.from_configuration(c)
            for c in manifest.language_configurations
        )
    )
    if preview.legacy_budget != expected_budget or preview.language_configurations != configs:
        raise ValueError("invalid candidate budget")
    names = {name for c in manifest.cases for name in (c.input_file, c.expected_file)}
    if set(refs) != names:
        raise ValueError("invalid hidden references")
    cases = tuple(
        TestCase(
            case_id=uuid5(preview.candidate_id, c.key),
            input=refs[c.input_file],
            expected=refs[c.expected_file],
        )
        for c in manifest.cases
    )
    projection = JudgingVersion(
        preview.candidate_id,
        uuid4(),
        manifest.languages,
        manifest.toolchain if legacy else None,
        manifest.compile_limits if legacy else None,
        manifest.run_limits if legacy else None,
        manifest.job_wall_time_us if legacy else None,
        cases,
        manifest.schema_version,
        () if legacy else manifest.language_configurations,
    )
    for language in manifest.languages:
        v2.admit_manifest(
            policy,
            projection.manifest(
                uuid4(),
                uuid4(),
                language,
                ArtifactReference(digest="0" * 64, bytes=MAX_SOURCE_BYTES),
            ),
            1,
        )
    return ValidatedCandidate(manifest.model_dump(mode="json"), preview, refs)
