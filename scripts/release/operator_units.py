"""Finite fixed-purpose operator helpers; no caller-controlled executable or identity."""

import hashlib
import json
import os
import re
import resource
import subprocess
from pathlib import Path

from . import preflight
from .policy import DeliveryError, invalid_constant, unique_object


class OperatorExecutionError(DeliveryError):
    """A fixed helper was dispatched but its result or cleanup was unconfirmed."""

    def __init__(self, message: str, failures: list | None = None):
        super().__init__(message)
        self.failures = failures or []


PROFILES = {
    "node-health": ("root", 0, 128, 16, 50, 300),
    "broker-health": ("rabbitmq", 1204, 256, 64, 100, 45),
    "factory-health": ("kf-worker", 1202, 32, 4, 100, 30),
    "import-health": ("kf-oj", 1200, 128, 16, 100, 60),
    "outbox-health": ("kf-oj", 1200, 128, 16, 100, 60),
    "node-maintenance": ("root", 0, 256, 32, 50, 900),
    "node-observation": ("root", 0, 128, 16, 25, 360),
}


# OTP 27 reserves a 32 MiB dual-mapped JIT block before serving CLI queries.
FILE_LIMIT_MIB = {"broker-health": 64}
HELPER_CGROUP = Path("/sys/fs/cgroup/kfonlinejudge.slice")

DIAGNOSTIC_PREFIX = "KF_OPERATOR_DIAGNOSTIC "
READINESS_ERRORS = frozenset(
    {
        "bounded_journal_unavailable",
        "https_entry_unavailable",
        "selected_binding_changed",
        "db_authority_unavailable",
        "api_live_unavailable",
        "execution_owner_unavailable",
        "broker_metadata_unavailable",
        "outbox_pipeline_unavailable",
        "import_pipeline_unavailable",
        "factory_status_unavailable",
        "notification_delivery_unconfirmed",
        *(
            name + "_unavailable"
            for name in (
                "api",
                "coordinator",
                "outbox",
                "problem-import",
                "worker",
                "postgres",
                "rabbitmq",
                "sandbox-factory",
                "proxy",
            )
        ),
    }
)


def failure_record(name: str, outcome: str, result=None, error=None) -> dict:
    """Record only fixed identifiers, sizes and digests; never child output or argv."""
    from .service_install import UnitManagerError

    record = {"helper": name, "outcome": outcome, "returncode": None}
    if result is not None:
        record["returncode"] = result.returncode
    for stream in ("stdout", "stderr"):
        value = getattr(result, stream, b"") if result is not None else b""
        if isinstance(error, subprocess.TimeoutExpired):
            value = getattr(error, stream, None) or b""
        record[stream + "_bytes"] = len(value)
        record[stream + "_sha256"] = hashlib.sha256(value).hexdigest()
    record["error_sha256"] = hashlib.sha256(
        (str(error) if isinstance(error, DeliveryError) else type(error).__name__).encode()
    ).hexdigest()
    if isinstance(error, UnitManagerError):
        record["unit_manager"] = error.diagnostic
    return record


def diagnostic_chain(stderr: bytes) -> list:
    """Accept one small, strictly shaped record from a fixed child CLI."""
    if len(stderr) > 4096:
        return []
    lines = [line for line in stderr.splitlines() if line.startswith(DIAGNOSTIC_PREFIX.encode())]
    try:
        if len(lines) != 1:
            return []
        value = decode(lines[0][len(DIAGNOSTIC_PREFIX) :], 3072)
        if (
            set(value) != {"schema_version", "failures"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            return []
        failures = value["failures"]
        if not isinstance(failures, list) or not 1 <= len(failures) <= 3:
            return []
        fields = {
            "helper",
            "outcome",
            "returncode",
            "stdout_bytes",
            "stdout_sha256",
            "stderr_bytes",
            "stderr_sha256",
            "error_sha256",
        }
        for row in failures:
            if (
                not isinstance(row, dict)
                or not fields <= set(row)
                or set(row) - fields - {"readiness_errors", "unit_manager"}
            ):
                return []
            if row["helper"] not in PROFILES or row["outcome"] not in {
                "dispatch",
                "exit",
                "decode",
                "cleanup",
                "timeout",
                "preconditions",
                "admission",
            }:
                return []
            code = row["returncode"]
            if code is not None and (type(code) is not int or not -255 <= code <= 255):
                return []
            if any(
                type(row[k]) is not int or not 0 <= row[k] <= 2**63 - 1
                for k in ("stdout_bytes", "stderr_bytes")
            ):
                return []
            if any(
                not isinstance(row[k], str) or not re.fullmatch("[a-f0-9]{64}", row[k])
                for k in ("stdout_sha256", "stderr_sha256", "error_sha256")
            ):
                return []
            errors = row.get("readiness_errors", [])
            if (
                not isinstance(errors, list)
                or len(errors) > len(READINESS_ERRORS)
                or any(not isinstance(code, str) or code not in READINESS_ERRORS for code in errors)
            ):
                return []
            if "unit_manager" in row:
                manager = row["unit_manager"]
                if (
                    not isinstance(manager, dict)
                    or set(manager)
                    != {
                        "operation",
                        "returncode",
                        "not_loaded",
                        "stdout_bytes",
                        "stdout_sha256",
                        "stderr_bytes",
                        "stderr_sha256",
                    }
                    or manager["operation"] not in {"show", "stop", "other"}
                    or type(manager["returncode"]) is not int
                    or not -255 <= manager["returncode"] <= 255
                    or type(manager["not_loaded"]) is not bool
                    or any(
                        type(manager[k]) is not int or not 0 <= manager[k] <= 2**63 - 1
                        for k in ("stdout_bytes", "stderr_bytes")
                    )
                    or any(
                        not isinstance(manager[k], str)
                        or not re.fullmatch("[a-f0-9]{64}", manager[k])
                        for k in ("stdout_sha256", "stderr_sha256")
                    )
                ):
                    return []
        return failures
    except (TypeError, ValueError, KeyError):
        return []


def diagnostic_suffix(error: Exception) -> str:
    if not isinstance(error, OperatorExecutionError) or not error.failures:
        return ""
    # Revalidate even internally built records before they cross a CLI boundary.
    value = json.dumps({"schema_version": 1, "failures": error.failures}, separators=(",", ":"))
    line = DIAGNOSTIC_PREFIX + value
    if len(error.failures) > 4 or len(line) > 3072:
        return ""
    # The child decoder allows three ancestors; validate the fourth separately.
    for row in error.failures:
        single = DIAGNOSTIC_PREFIX + json.dumps({"schema_version": 1, "failures": [row]})
        if not diagnostic_chain(single.encode()):
            return ""
    return line + "\n"


def readiness_failure(message: str, result: dict, *, admission: bool = False):
    error = OperatorExecutionError(message)
    row = failure_record("node-health", "admission" if admission else "preconditions", error=error)
    errors = result.get("errors")
    if (
        isinstance(errors, list)
        and len(errors) <= len(READINESS_ERRORS)
        and all(isinstance(code, str) and code in READINESS_ERRORS for code in errors)
    ):
        row["readiness_errors"] = errors
    child = DIAGNOSTIC_PREFIX + json.dumps(
        {"schema_version": 1, "failures": result.get("operator_failures", [])}
    )
    error.failures = diagnostic_chain(child.encode())[:2] + [row]
    return error


def guarded(name: str) -> None:
    _, uid, memory, pids, cpu, _ = PROFILES[name]
    if (
        os.getuid() != uid
        or os.geteuid() != uid
        or os.getgid() != uid
        or not set(os.getgroups()) <= {uid}
    ):
        raise DeliveryError("fixed operator helper identity required")
    observed = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    if observed.get("NoNewPrivs", "").strip() != "1" or any(
        not observed.get(k, "").strip() or int(observed[k].strip(), 16) != 0
        for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    ):
        raise DeliveryError("operator helper privilege boundary differs")
    group = preflight.current_cgroup()
    if str(group) != "/sys/fs/cgroup/kfonlinejudge.slice/kf-" + name + ".service":
        raise DeliveryError("operator helper cgroup differs")
    for directory, expected in (
        (group, [str(memory * 1024**2), "0", str(pids), f"{cpu * 1000} 100000"]),
        (
            Path("/sys/fs/cgroup/kfonlinejudge.slice"),
            [str(3840 * 1024**2), "0", "384", "200000 100000"],
        ),
    ):
        actual = [
            (directory / k).read_text().strip()
            for k in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
        ]
        if actual != expected:
            raise DeliveryError("operator helper/node budgets differ")
    for key, maximum in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, FILE_LIMIT_MIB.get(name, 16) * 1024**2),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_STACK, 16 * 1024**2),
    ):
        if resource.getrlimit(key) != (maximum, maximum):
            raise DeliveryError("operator helper process budgets differ")


def decode(contents: bytes, maximum: int = 32768):
    if len(contents) > maximum:
        raise DeliveryError("operator output exceeds bound")
    return json.loads(
        contents,
        object_pairs_hook=unique_object,
        parse_constant=invalid_constant,
    )


def _helper_status(unit: str) -> dict:
    from . import service_install

    return unique_object(
        [
            line.split("=", 1)
            for line in service_install.systemctl(
                "show",
                unit,
                "--property=LoadState,ActiveState,MainPID,Transient,Description,ControlGroup",
            ).splitlines()
            if "=" in line
        ]
    )


def _helper_group_empty(unit: str) -> bool:
    group = HELPER_CGROUP / unit
    try:
        return (
            not (group / "cgroup.procs").read_text().strip()
            and "populated 0" in (group / "cgroup.events").read_text().splitlines()
        )
    except FileNotFoundError:
        # Removal after the read is normal; a present group with missing
        # counters is not evidence of cleanup.
        return not group.exists()


def _helper_owned(actual: dict, unit: str, description: str) -> None:
    if actual.get("LoadState") == "not-found":
        if actual.get("ControlGroup") != "":
            raise DeliveryError("operator helper cleanup unconfirmed")
    elif (
        actual.get("LoadState") != "loaded"
        or actual.get("Transient") != "yes"
        or actual.get("Description") != description
        or actual.get("ControlGroup") not in {"", "/kfonlinejudge.slice/" + unit}
    ):
        raise DeliveryError("foreign operator helper; refused cleanup")


def _helper_stopped(actual: dict, unit: str) -> bool:
    return (
        actual.get("ActiveState") == "inactive"
        and actual.get("MainPID") == "0"
        and _helper_group_empty(unit)
    )


def _cleanup_helper(unit: str, description: str) -> None:
    from . import service_install

    actual = _helper_status(unit)
    _helper_owned(actual, unit, description)
    if _helper_stopped(actual, unit):
        # --wait/--collect can finish before this read. Stopping an already
        # empty transient unit races with systemd's automatic collection.
        return
    if actual.get("LoadState") == "not-found":
        raise DeliveryError("operator helper cleanup unconfirmed")
    try:
        service_install.systemctl("stop", unit)
    except service_install.UnitManagerError as error:
        if not error.not_loaded:
            raise
        actual = _helper_status(unit)
        if actual.get("LoadState") != "not-found":
            raise
    else:
        actual = _helper_status(unit)
    _helper_owned(actual, unit, description)
    if not _helper_stopped(actual, unit):
        raise DeliveryError("operator helper cleanup unconfirmed")


def run(
    name: str,
    payload: Path,
    *,
    notification_probe: bool = False,
    action: str | None = None,
) -> dict:
    from . import configuration, install, service_install
    from .services import python_environment

    if os.getuid() != 0 or os.geteuid() != 0 or name not in PROFILES:
        raise DeliveryError("fixed root operator helper required")
    if name == "node-maintenance":
        if action not in {"start", "resume", "drain", "stop", "restart"} or notification_probe:
            raise DeliveryError("explicit fixed maintenance action required")
    elif action is not None:
        raise DeliveryError("unexpected operator action override")
    user, _, memory, pids, cpu, seconds = PROFILES[name]
    unit = "kf-" + name + ".service"
    current = dict(
        line.split("=", 1)
        for line in service_install.systemctl(
            "show", unit, "--property=LoadState,ActiveState,MainPID"
        ).splitlines()
        if "=" in line
    )
    if (
        current.get("LoadState") != "not-found"
        or current.get("ActiveState") != "inactive"
        or current.get("MainPID") != "0"
    ):
        raise DeliveryError("operator helper already active or unknown")
    release_id = payload.parent.name
    selected = install.selection(check_configuration=False)
    if (
        payload != install.RELEASES / selected["release_id"] / "payload"
        or release_id != selected["release_id"]
    ):
        raise DeliveryError("operator helper is not the selected payload")
    description = "KF fixed " + name + " " + release_id + "." + selected["configuration_id"]
    command = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--unit=" + unit,
        "--description=" + description,
        "--slice=kfonlinejudge.slice",
    ]
    properties = {
        "Type": "exec",
        "User": user,
        "Group": user,
        "UMask": "0077",
        "MemoryMax": str(memory) + "M",
        "MemorySwapMax": "0",
        "TasksMax": str(pids),
        "CPUQuota": str(cpu) + "%",
        "LimitCORE": "0",
        "LimitFSIZE": str(FILE_LIMIT_MIB.get(name, 16)) + "M",
        "LimitNOFILE": "256",
        "LimitSTACK": "16M",
        "RuntimeMaxSec": str(seconds),
        "TimeoutStartSec": "10",
        "TimeoutStopSec": "10",
        "Restart": "no",
        "KillMode": "control-group",
        "NoNewPrivileges": "yes",
        "CapabilityBoundingSet": "",
        "AmbientCapabilities": "",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateTmp": "yes",
        "PrivateDevices": "yes",
        "ProtectControlGroups": "yes",
        "RestrictAddressFamilies": "AF_UNIX AF_INET",
        "WorkingDirectory": str(payload),
    }
    if name == "node-observation":
        # The unified io controller may be disabled until a unit requests
        # accounting. Enable it through systemd before reading io.stat for
        # this slice and its services; keep missing counters a hard failure.
        properties["IOAccounting"] = "yes"
    for key, value in properties.items():
        command.append("--property=" + key + "=" + value)
    if name in {"import-health", "outbox-health"}:
        path = configuration.installed_path(
            selected["release_id"],
            selected["configuration_id"],
            "import.env" if name == "import-health" else "outbox.env",
        )
        command += [
            "--property=EnvironmentFile=" + str(path),
            "--property=PrivatePIDs=yes",
        ]
        if name == "import-health":
            command += [
                "--property=PrivateNetwork=yes",
                "--property=RestrictAddressFamilies=AF_UNIX",
            ]
    if name == "node-maintenance":
        command += [
            "--property=RuntimeDirectory=kf-node-verification",
            "--property=RuntimeDirectoryMode=0700",
            "--property=ReadWritePaths=/etc/kf-admission /var/lib/kf-onlinejudge/operations"
            " /run/kf-node-verification",
        ]
    if name in {"import-health", "outbox-health"}:
        if notification_probe:
            raise DeliveryError("unexpected import probe override")
        command += [
            "--property=Environment=PATH=/usr/bin:/bin LC_ALL=C.UTF-8 "
            + python_environment(payload)
        ]
        command += [
            str(payload / "python/bin/python3.13"),
            "-s",
            "-P",
            "-m",
            "scripts.release." + name.replace("-", "_"),
            "--inside",
        ]
    else:
        command += ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C.UTF-8"]
    if name == "factory-health":
        if notification_probe:
            raise DeliveryError("unexpected factory probe override")
        command += [
            *python_environment(payload).split(),
            str(payload / "python/bin/python3.13"),
            "-s",
            "-P",
            "-m",
            "scripts.release.factory_health",
            "--inside",
        ]
    elif name not in {"import-health", "outbox-health"}:
        command += [
            *python_environment(payload).split(),
            str(payload / "python/bin/python3.13"),
            "-s",
            "-P",
            "-m",
            "scripts.release.lifecycle"
            if name == "node-maintenance"
            else "scripts.release." + name.replace("-", "_"),
            "--inside",
        ]
        if name == "node-maintenance":
            command += ["--action", action]
        if notification_probe and name == "node-health":
            command.append("--probe-notification")
        elif notification_probe:
            raise DeliveryError("unexpected broker probe override")
    result = None
    outcome = "dispatch"
    try:
        try:
            # Pipe output is bounded by the fixed helper and the service file/memory
            # ceilings; helper tools below cap each pipe before serializing it.
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=seconds + 15,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
            outcome = "exit"
            if result.returncode or len(result.stderr) > 4096:
                raise DeliveryError("operator helper unavailable")
            outcome = "decode"
            value = decode(result.stdout)
            if not isinstance(value, dict):
                raise DeliveryError("operator helper object required")
        finally:
            previous_outcome, outcome = outcome, "cleanup"
            _cleanup_helper(unit, description)
            outcome = previous_outcome
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, DeliveryError) else type(error).__name__
        if isinstance(error, subprocess.TimeoutExpired) and outcome != "cleanup":
            outcome = "timeout"
        failures = diagnostic_chain(result.stderr) if result is not None else []
        failures.append(failure_record(name, outcome, result, error))
        raise OperatorExecutionError(message, failures) from error
    return value
