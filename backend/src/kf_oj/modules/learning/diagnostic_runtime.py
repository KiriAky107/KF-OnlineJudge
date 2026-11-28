"""Select independent optimization diagnostic budgets for an admitted GNU runtime."""

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2
from kf_oj.contracts.diagnostic import (
    MAX_DIAGNOSTIC_WALL_US,
    MAX_PREPARATION_WALL_US,
    MAX_SAMPLE_WALL_US,
    DiagnosticMode,
)
from kf_oj.contracts.diagnostic_batch import BatchPurpose
from kf_oj.modules.problems.public import JudgingVersion

from .diagnostic_inputs import DiagnosticInputsUnavailable

INPUT_CHECK_BUDGET_US = 6_000_000
# A batch's consecutive workers must each obtain a fresh root factory status.
FACTORY_STAGE_INTERVAL_US = 1_050_000


def _limit_stage(
    ceilings: tuple[v1.RunLimits, ...],
    wall_us: int,
) -> v1.RunLimits:
    limits = {
        name: min(getattr(stage, name) for stage in ceilings) for name in v1.RunLimits.model_fields
    }
    limits["wall_time_us"] = min(limits["wall_time_us"], wall_us)
    limits["cpu_time_us"] = min(limits["cpu_time_us"], limits["wall_time_us"])
    return v1.RunLimits(**limits)


def _legacy_pin(runtime: v2.RuntimeIdentity) -> v1.Toolchain:
    return v1.Toolchain(
        toolchain_id=runtime.toolchain_id,
        image_kind=runtime.image_kind,
        image_revision=runtime.image_revision,
    )


def diagnostic_configuration(
    version: JudgingVersion,
    language: str,
    admission: v2.NodeAdmission | None,
    *,
    package_bytes: int,
    expected_bytes: int,
    mode: DiagnosticMode = "optimization",
) -> v2.LanguageConfiguration:
    """Resolve a trusted exact GNU pin and bound the selected passes independently of judging."""
    passes = 1 if mode == "ubsan" else 2
    wall_ceiling = (
        MAX_PREPARATION_WALL_US + 2 * MAX_SAMPLE_WALL_US
        if mode == "ubsan"
        else MAX_DIAGNOSTIC_WALL_US
    )
    return _diagnostic_configuration(
        version,
        language,
        admission,
        package_bytes=package_bytes,
        expected_bytes=expected_bytes,
        preparations=passes,
        runs=passes,
        wall_ceiling=wall_ceiling,
    )


def diagnostic_batch_configuration(
    version: JudgingVersion,
    language: str,
    admission: v2.NodeAdmission | None,
    *,
    purpose: BatchPurpose,
    case_count: int,
    package_bytes: int,
    expected_bytes: int,
    repeat_candidate: bool = False,
    search_reduction: bool = False,
    check_input: bool = False,
) -> v2.LanguageConfiguration:
    programs, runs = _batch_stages(purpose, case_count, repeat_candidate, search_reduction)
    return _diagnostic_configuration(
        version,
        language,
        admission,
        package_bytes=package_bytes,
        expected_bytes=expected_bytes,
        preparations=programs,
        runs=runs,
        wall_ceiling=MAX_DIAGNOSTIC_WALL_US,
        reserve_wall=(
            (programs - 1 + int(check_input)) * FACTORY_STAGE_INTERVAL_US
            + (INPUT_CHECK_BUDGET_US if check_input else 0)
        ),
        fit_compile_to_budget=check_input,
    )


def diagnostic_checker_configuration(
    batch: v2.LanguageConfiguration,
    purpose: BatchPurpose,
    case_count: int,
    *,
    repeat_candidate: bool = False,
    search_reduction: bool = False,
) -> v2.LanguageConfiguration:
    programs, runs = _batch_stages(purpose, case_count, repeat_candidate, search_reduction)
    replay_wall = (
        programs * (batch.compile_limits.wall_time_us + FACTORY_STAGE_INTERVAL_US)
        + runs * batch.run_limits.wall_time_us
    )
    remaining = batch.job_wall_time_us - replay_wall
    if remaining < INPUT_CHECK_BUDGET_US:
        raise DiagnosticInputsUnavailable("diagnostic budget cannot cover the input checker")
    run_wall = min(250_000, remaining // (2 * case_count))
    run = _limit_stage((batch.run_limits,), run_wall)
    preparation = _limit_stage(
        (batch.compile_limits,), min(5_000_000, remaining - case_count * run.wall_time_us)
    )
    return v2.LanguageConfiguration(
        language=batch.language,
        toolchain=batch.toolchain,
        compile_limits=preparation,
        run_limits=run,
        job_wall_time_us=preparation.wall_time_us + case_count * run.wall_time_us,
    )


def _batch_stages(
    purpose: BatchPurpose, case_count: int, repeat_candidate: bool, search_reduction: bool
) -> tuple[int, int]:
    if search_reduction:
        return 3, 2 * case_count + 4
    programs = 2 if purpose == "differential" else 1
    return programs, (programs + int(repeat_candidate)) * case_count


def _diagnostic_configuration(
    version: JudgingVersion,
    language: str,
    admission: v2.NodeAdmission | None,
    *,
    package_bytes: int,
    expected_bytes: int,
    preparations: int,
    runs: int,
    wall_ceiling: int,
    reserve_wall: int = 0,
    fit_compile_to_budget: bool = False,
) -> v2.LanguageConfiguration:
    if language not in ("c17", "cpp20") or language not in version.languages:
        raise DiagnosticInputsUnavailable("diagnostic language unavailable")
    registry = admission if isinstance(admission, v2.AdmissionRegistry) else None
    policy = registry.multi_runtime if registry is not None else admission
    if not isinstance(policy, v2.AdmissionPolicy):
        raise DiagnosticInputsUnavailable("exact diagnostic runtime unavailable")
    capability = next((cap for cap in policy.capabilities if cap.language == language), None)
    if capability is None:
        raise DiagnosticInputsUnavailable("diagnostic language unavailable")

    max_package = policy.max_package_bytes
    max_wall = min(wall_ceiling, policy.max_job_wall_time_us)
    if version.judging_schema_version == 1:
        legacy = registry.legacy if registry is not None else None
        if (
            legacy is None
            or version.compile_limits is None
            or version.run_limits is None
            or legacy.toolchain != version.toolchain
            or _legacy_pin(capability.toolchain) != version.toolchain
            or version.language_configurations
        ):
            raise DiagnosticInputsUnavailable("legacy diagnostic runtime unavailable")
        compile_stages = (
            version.compile_limits,
            legacy.compile_ceiling,
            capability.compile_ceiling,
        )
        run_stages = (version.run_limits, legacy.run_ceiling, capability.run_ceiling)
        max_package = min(max_package, legacy.max_package_bytes)
        max_wall = min(max_wall, legacy.max_job_wall_time_us)
    elif version.judging_schema_version == 2:
        configured = next(
            (c for c in version.language_configurations if c.language == language), None
        )
        if configured is None or configured.toolchain != capability.toolchain:
            raise DiagnosticInputsUnavailable("diagnostic runtime differs from published version")
        compile_stages = (configured.compile_limits, capability.compile_ceiling)
        run_stages = (configured.run_limits, capability.run_ceiling)
    else:
        raise DiagnosticInputsUnavailable("diagnostic runtime unavailable")

    run_limits = _limit_stage(run_stages, MAX_SAMPLE_WALL_US)
    compile_wall = MAX_PREPARATION_WALL_US
    if fit_compile_to_budget:
        compile_wall = min(
            compile_wall, (max_wall - runs * run_limits.wall_time_us - reserve_wall) // preparations
        )
        if compile_wall < 1000:
            raise DiagnosticInputsUnavailable("diagnostic budget cannot cover repeated cases")
    compile_limits = _limit_stage(compile_stages, compile_wall)
    required_wall = preparations * compile_limits.wall_time_us + runs * run_limits.wall_time_us
    if (
        max_wall < required_wall + reserve_wall
        or package_bytes > max_package
        or expected_bytes > run_limits.stdout_bytes
    ):
        raise DiagnosticInputsUnavailable("diagnostic work exceeds independent budget")
    return v2.LanguageConfiguration(
        language=capability.language,
        toolchain=capability.toolchain,
        compile_limits=compile_limits,
        run_limits=run_limits,
        job_wall_time_us=max_wall,
    )
