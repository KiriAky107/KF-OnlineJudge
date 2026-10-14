"""Finite fixed-purpose operator helpers; no caller-controlled executable or identity."""

import json
import os
import resource
import subprocess
from pathlib import Path

from . import configuration, install, preflight, service_install
from .policy import DeliveryError, invalid_constant, unique_object
from .services import python_environment

PROFILES = {
    "node-health": ("root", 0, 128, 16, 50, 300),
    "broker-health": ("rabbitmq", 1204, 256, 64, 50, 45),
    "factory-health": ("kf-worker", 1202, 32, 4, 10, 30),
    "import-health": ("kf-oj", 1200, 128, 16, 25, 60),
    "outbox-health": ("kf-oj", 1200, 128, 16, 25, 60),
    "node-maintenance": ("root", 0, 256, 32, 50, 900),
    "node-observation": ("root", 0, 128, 16, 25, 360),
}


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
        (resource.RLIMIT_FSIZE, 16 * 1024**2),
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


def run(
    name: str,
    payload: Path,
    *,
    notification_probe: bool = False,
    action: str | None = None,
) -> dict:
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
        "LimitFSIZE": "16M",
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
            "--property=ReadWritePaths=/etc/kf-admission /var/lib/kf-onlinejudge/operations"
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
    try:
        # Pipe output is bounded by the fixed helper and the service file/memory
        # ceilings; helper tools below cap each pipe before serializing it.
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=seconds + 15,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        if result.returncode or len(result.stderr) > 4096:
            raise DeliveryError("operator helper unavailable")
        value = decode(result.stdout)
        if not isinstance(value, dict):
            raise DeliveryError("operator helper object required")
    finally:
        actual = dict(
            line.split("=", 1)
            for line in service_install.systemctl(
                "show",
                unit,
                "--property=LoadState,ActiveState,MainPID,Transient,Description",
            ).splitlines()
            if "=" in line
        )
        if actual.get("LoadState") != "not-found":
            if actual.get("Transient") != "yes" or actual.get("Description") != description:
                raise DeliveryError("foreign operator helper; refused cleanup")
            service_install.systemctl("stop", unit)
            actual = dict(
                line.split("=", 1)
                for line in service_install.systemctl(
                    "show", unit, "--property=ActiveState,MainPID"
                ).splitlines()
                if "=" in line
            )
            if actual.get("ActiveState") != "inactive" or actual.get("MainPID") != "0":
                raise DeliveryError("operator helper cleanup unconfirmed")
    return value
