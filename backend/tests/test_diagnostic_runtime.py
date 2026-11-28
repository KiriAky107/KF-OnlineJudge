from dataclasses import replace
from pathlib import Path

import pytest

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.modules.learning.diagnostic_inputs import DiagnosticInputsUnavailable
from kf_oj.modules.learning.diagnostic_runtime import diagnostic_configuration
from kf_oj.modules.problems.public import JudgingVersion

_FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/contracts-v2"


@pytest.mark.parametrize("language", ("c17", "cpp20"))
def test_diagnostic_budget_preserves_exact_runtime_and_reserves_both_passes(language):
    registry = v2.AdmissionRegistry.model_validate_json(
        (_FIXTURES / "judge-registry.valid.json").read_bytes()
    )
    job = v2.JobManifest.model_validate_json(
        (_FIXTURES / f"judge-manifest.valid-{language}.json").read_bytes()
    )
    configuration = v2.LanguageConfiguration(
        **job.model_dump(
            include={
                "language",
                "toolchain",
                "compile_limits",
                "run_limits",
                "job_wall_time_us",
            }
        )
    )
    version = JudgingVersion(
        job.problem_version_id,
        job.problem_version_id,
        (job.language,),
        None,
        None,
        None,
        None,
        (),
        judging_schema_version=2,
        language_configurations=(configuration,),
    )

    def select(source=version, policy=registry, *, expected=1, package=100):
        return diagnostic_configuration(
            source, language, policy, package_bytes=package, expected_bytes=expected
        )

    selected = select()
    assert selected.toolchain == configuration.toolchain
    assert selected.compile_limits.wall_time_us == 20_000_000
    assert selected.run_limits.wall_time_us == 3_000_000
    assert selected.job_wall_time_us == 55_000_000
    assert selected.compile_limits.fits(configuration.compile_limits)
    assert selected.run_limits.fits(configuration.run_limits)
    assert configuration.compile_limits.wall_time_us == 30_000_000

    with pytest.raises(DiagnosticInputsUnavailable, match="published version"):
        select(
            replace(
                version,
                language_configurations=(
                    configuration.model_copy(
                        update={
                            "toolchain": configuration.toolchain.model_copy(
                                update={"image_digest": "9" * 64}
                            )
                        }
                    ),
                ),
            )
        )
    with pytest.raises(DiagnosticInputsUnavailable, match="independent budget"):
        select(expected=selected.run_limits.stdout_bytes + 1)
    with pytest.raises(DiagnosticInputsUnavailable, match="independent budget"):
        select(package=registry.multi_runtime.max_package_bytes + 1)
    with pytest.raises(DiagnosticInputsUnavailable, match="independent budget"):
        select(policy=registry.multi_runtime.model_copy(update={"max_job_wall_time_us": 1_000_000}))

    legacy = replace(
        version,
        languages=(v1.Language(language),),
        toolchain=registry.legacy.toolchain,
        compile_limits=job.compile_limits,
        run_limits=job.run_limits,
        job_wall_time_us=job.job_wall_time_us,
        judging_schema_version=1,
        language_configurations=(),
    )
    assert select(legacy) == selected
    with pytest.raises(DiagnosticInputsUnavailable, match="legacy diagnostic"):
        select(legacy, registry.multi_runtime)
    with pytest.raises(DiagnosticInputsUnavailable, match="legacy diagnostic"):
        select(replace(legacy, toolchain=legacy.toolchain.model_copy(update={"image_revision": 2})))
