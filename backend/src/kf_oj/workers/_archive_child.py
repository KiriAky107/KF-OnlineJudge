"""Fixed data-only child. It inherits no DB credentials and executes no uploaded contents."""

import base64
import json
import os
import resource
import signal
import sys
import time
from uuid import UUID

from pydantic import TypeAdapter

from kf_oj.contracts.admin_problems import (
    VALIDATION_DEADLINE_SECONDS,
    CandidatePreview,
    LegacyImportBudget,
)
from kf_oj.contracts.judging_v2 import NodeAdmission
from kf_oj.contracts.problems import PublicLanguageConfiguration, PublicSample
from kf_oj.infra.artifacts.archive_reader import ArchiveError, ArchiveReader
from kf_oj.modules.problems.candidate_validation import validate_candidate
from kf_oj.modules.problems.import_format import ProblemImport
from kf_oj.modules.problems.web_format import decode_web_declaration


def main():
    # These are ordinary downward-only process limits, independent of the parent's life.
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2, 128 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.alarm(VALIDATION_DEADLINE_SECONDS)
    archive, directory, lock, size, digest, candidate, policy_json = sys.argv[1:]
    archive, directory, lock = int(archive), int(directory), int(lock)
    os.fstat(lock)  # Held inherited flock prevents GC releasing capacity while any child lives.
    deadline = time.monotonic() + VALIDATION_DEADLINE_SECONDS
    result = {"failure_code": "invalid_archive"}
    try:
        reader = ArchiveReader(archive, int(size), deadline=deadline)
        reader.verify_digest(digest)
        result["failure_code"] = "invalid_manifest"
        declared = decode_web_declaration(reader.read("problem.json", 256 * 1024))
        if {m.name for m in reader.members} != set(declared.members):
            raise ValueError("unexpected package members")
        manifest = declared.manifest
        result["failure_code"] = "invalid_data"
        statement = reader.read(manifest.statement_file, 256 * 1024).decode("utf-8")
        sample_data = {
            name: reader.read(name, 65536)
            for name in sorted({n for s in manifest.samples for n in (s.input_file, s.output_file)})
        }
        samples = tuple(
            PublicSample(
                input=base64.b64encode(sample_data[s.input_file]).decode("ascii"),
                output=base64.b64encode(sample_data[s.output_file]).decode("ascii"),
            )
            for s in manifest.samples
        )
        references = reader.extract_remaining(directory)
        reader.verify_unchanged()
        legacy = isinstance(manifest, ProblemImport)
        preview = CandidatePreview(
            candidate_id=UUID(candidate),
            slug=manifest.slug,
            title=manifest.title,
            statement=statement,
            samples=samples,
            judging_schema_version=manifest.schema_version,
            languages=manifest.languages,
            legacy_budget=LegacyImportBudget(
                compile_limits=manifest.compile_limits,
                run_limits=manifest.run_limits,
                job_wall_time_us=manifest.job_wall_time_us,
            )
            if legacy
            else None,
            language_configurations=()
            if legacy
            else tuple(
                PublicLanguageConfiguration.from_configuration(c)
                for c in manifest.language_configurations
            ),
            case_count=len(manifest.cases),
        )
        result["failure_code"] = "policy_rejected"
        validated = validate_candidate(
            manifest.model_dump(mode="json"),
            preview.model_dump(mode="json"),
            {k: {"digest": v[0], "bytes": v[1]} for k, v in references.items()},
            TypeAdapter(NodeAdmission).validate_json(policy_json),
        )
        result = {
            "manifest": validated.manifest,
            "preview": validated.preview.model_dump(mode="json"),
            "references": {k: v.model_dump(mode="json") for k, v in validated.references.items()},
            "manifest_digest": validated.digest,
        }
    except ArchiveError as error:
        result = {
            "failure_code": "digest_mismatch"
            if str(error) == "archive_digest_mismatch"
            else "invalid_archive"
        }
    except (ValueError, OSError, MemoryError):
        result = {"failure_code": result["failure_code"]}
    payload = json.dumps(result, separators=(",", ":"), ensure_ascii=True).encode()
    if len(payload) > 4 * 1024**2:
        payload = b'{"failure_code":"invalid_data"}'
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
