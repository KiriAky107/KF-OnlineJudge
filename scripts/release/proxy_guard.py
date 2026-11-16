"""Ordinary proxy guard and fixed exec; never read protected application credentials."""

import argparse
import os
import resource
import stat
from pathlib import Path

from . import install, journal, preflight, proxy_install, proxy_runtime, proxy_service
from .policy import DeliveryError, digest, fields, read_json


def validate_runtime(value: dict) -> None:
    fields(
        value,
        {
            "uids",
            "gids",
            "groups",
            "no_new_privileges",
            "capabilities",
            "pid",
            "cgroup",
            "limits",
            "node_limits",
            "process_limits",
        },
        "proxy process observation",
    )
    uid = str(proxy_service.UID)
    expected = f"{proxy_service.CAPABILITY:016x}"
    if (
        value["uids"] != [uid] * 4
        or value["gids"] != [uid] * 4
        or not set(value["groups"]) <= {uid}
        or value["no_new_privileges"] != "1"
        or type(value["pid"]) is not int
        or value["pid"] != 1
        or value["cgroup"] != "/sys/fs/cgroup/kfonlinejudge.slice/kf-proxy.service"
        or value["capabilities"] != [expected] * 5
    ):
        raise DeliveryError("ordinary proxy identity/capability boundary differs")
    if value["limits"] != {
        "memory.max": str(128 * 1024**2),
        "memory.swap.max": "0",
        "pids.max": "16",
        "cpu.max": "50000 100000",
    } or value["node_limits"] != {
        "memory.max": str(3840 * 1024**2),
        "memory.swap.max": "0",
        "pids.max": "384",
        "cpu.max": "200000 100000",
    }:
        raise DeliveryError("proxy effective node/service budgets differ")
    if value["process_limits"] != {
        "core": [0, 0],
        "file": [8 * 1024**2] * 2,
        "files": [256] * 2,
        "stack": [16 * 1024**2] * 2,
    }:
        raise DeliveryError("proxy effective process limits differ")


def observe() -> dict:
    status = dict(
        line.split(":", 1)
        for line in journal.bounded_text(Path("/proc/self/status"), 16384).splitlines()
        if ":" in line
    )
    group = preflight.current_cgroup()
    node = Path("/sys/fs/cgroup/kfonlinejudge.slice")
    names = ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
    return {
        "uids": status.get("Uid", "").split(),
        "gids": status.get("Gid", "").split(),
        "groups": status.get("Groups", "").split(),
        "no_new_privileges": status.get("NoNewPrivs", "").strip(),
        "capabilities": [
            status.get(k, "").strip() for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        ],
        "pid": os.getpid(),
        "cgroup": str(group),
        "limits": {k: (group / k).read_text().strip() for k in names},
        "node_limits": {k: (node / k).read_text().strip() for k in names},
        "process_limits": {
            k: list(resource.getrlimit(r))
            for k, r in [
                ("core", resource.RLIMIT_CORE),
                ("file", resource.RLIMIT_FSIZE),
                ("files", resource.RLIMIT_NOFILE),
                ("stack", resource.RLIMIT_STACK),
            ]
        },
    }


def run(release: str, batch: str) -> None:
    selected = {"release_id": release, "configuration_id": batch}
    directory = proxy_service.batch(selected)
    validate_runtime(observe())
    proxy_install.account()
    proxy_install.storage()
    public = directory / "launch.json"
    preflight.protected(public)
    info = public.stat()
    if stat.S_IMODE(info.st_mode) != 0o644 or info.st_gid != 0:
        raise DeliveryError("protected public proxy launch spec required")
    spec = read_json(public, 4096)
    fields(
        spec,
        {
            "schema_version",
            "release_id",
            "configuration_id",
            "origin",
            "configuration_sha256",
            "certificate_sha256",
            "key_sha256",
        },
        "proxy launch spec",
    )
    if (
        spec["schema_version"] != 1
        or type(spec["schema_version"]) is not int
        or (spec["release_id"], spec["configuration_id"]) != (release, batch)
    ):
        raise DeliveryError("proxy public launch binding differs")
    config = directory / "nginx.conf"
    preflight.protected(config)
    if digest(config) != spec["configuration_sha256"]:
        raise DeliveryError("proxy configuration bytes changed")
    for name, key in [("fullchain.pem", "certificate_sha256"), ("key.pem", "key_sha256")]:
        path = proxy_service.ROOT / "tls" / name
        preflight.protected(path)
        if path.stat().st_size > 65536 or digest(path) != spec[key]:
            raise DeliveryError("proxy TLS bytes changed")
    payload = install.RELEASES / release / "payload"
    root = payload / "proxy"
    preflight.protected(root, directory=True)
    # This guard is the already-installed, Root-protected launcher selected by
    # the fixed unit; catalog checks are additional integrity checks only.
    proxy_runtime.verify(root, digest(root / "manifest.json"))
    executable = root / "bin/nginx"
    preflight.protected(executable)
    os.execve(
        executable,
        [
            str(executable),
            "-p",
            "/run/kf-proxy/",
            "-c",
            str(config),
            "-g",
            "daemon off; master_process on;",
        ],
        {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "LD_LIBRARY_PATH": str(root / "lib")},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed ordinary HTTPS launcher")
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--configuration-id", required=True)
    args = parser.parse_args()
    try:
        run(args.release_id, args.configuration_id)
    except (OSError, ValueError) as error:
        parser.exit(
            1,
            "Proxy launch refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
