"""Explicit ordinary PostgreSQL initialization; no schema migration or server start."""

import argparse
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

from . import (
    configuration,
    credentials,
    dependencies,
    dependency_install,
    install,
    preflight,
    service_install,
)
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256
from .services import python_environment

UNIT = "kf-postgres-initialize.service"
DATA = Path(dependencies.STORAGE["postgres"]["path"]) / "data"
RECEIPT = dependency_install.ROOT / "postgres.initialization.json"
CREDENTIAL = Path("/run/credentials") / UNIT / "bootstrap"
CGROUP = Path("/sys/fs/cgroup/kfonlinejudge.slice") / UNIT


def validate_password(contents: bytes) -> None:
    if re.fullmatch(rb"[0-9a-f]{64}\n", contents) is None:
        raise DeliveryError("explicit 256-bit hexadecimal bootstrap password required")


def cluster_identity() -> dict:
    root = DATA.lstat()
    version = DATA / "PG_VERSION"
    info = version.lstat()
    if (
        not stat.S_ISDIR(root.st_mode)
        or (root.st_uid, root.st_gid, stat.S_IMODE(root.st_mode)) != (1203, 1203, 0o700)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1203, 1203, 0o600)
        or info.st_size != 3
        or version.read_bytes() != b"18\n"
    ):
        raise DeliveryError("initialized PostgreSQL identity/version differs")
    return {"device": root.st_dev, "inode": root.st_ino, "major_version": 18, "uid": 1203}


def validate_receipt(value: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "source_sha",
            "configuration_id",
            "cluster_identity",
            "bootstrap_sha256",
            "initialized",
            "migrated",
            "services_started",
            "execution_verified",
        },
        "database initialization receipt",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["source_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["source_sha"])
        or not isinstance(value["configuration_id"], str)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", value["configuration_id"])
        or value["initialized"] is not True
        or any(
            value[key] is not False
            for key in ("migrated", "services_started", "execution_verified")
        )
    ):
        raise DeliveryError("initialization receipt binding differs")
    sha256(value["bootstrap_sha256"])
    validate_cluster_identity(value["cluster_identity"])
    return value


def validate_cluster_identity(value: dict) -> dict:
    identity = fields(value, {"device", "inode", "major_version", "uid"}, "cluster identity")
    if (
        any(type(identity[key]) is not int or identity[key] < 1 for key in identity)
        or identity["major_version"] != 18
        or identity["uid"] != 1203
    ):
        raise DeliveryError("recorded PostgreSQL cluster identity differs")
    return identity


def run_in_unit() -> dict:
    observed = dependencies.check_runtime("postgres", initializing=True)
    interfaces = Path("/proc/net/dev").read_text().splitlines()[2:]
    if {line.split(":", 1)[0].strip() for line in interfaces} != {"lo"}:
        raise DeliveryError("network-isolated database initializer required")
    credential = credentials.validate_metadata(CREDENTIAL, 1203, 65)
    if credential.st_size != 65:
        raise DeliveryError("private database initialization credential required")
    validate_password(CREDENTIAL.read_bytes())
    if DATA.exists() or DATA.is_symlink():
        raise DeliveryError("database initializer refuses any existing target")
    preflight.protected(Path("/usr/bin/initdb"))
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent"}
    version = subprocess.run(
        ["/usr/bin/initdb", "--version"], capture_output=True, timeout=5, env=env
    )
    if version.returncode or version.stdout.strip() != b"initdb (PostgreSQL) 18.6":
        raise DeliveryError("database initializer tool version differs")
    result = subprocess.run(
        [
            "/usr/bin/initdb",
            "--pgdata=" + str(DATA),
            "--username=postgres",
            "--pwfile=" + str(CREDENTIAL),
            "--auth-host=scram-sha-256",
            "--auth-local=scram-sha-256",
            "--encoding=UTF8",
            "--locale=C.UTF-8",
            "--data-checksums",
            "--no-clean",
            "--no-instructions",
            "--set=shared_buffers=64MB",
            "--set=max_connections=32",
            "--set=work_mem=1MB",
            "--set=maintenance_work_mem=32MB",
            "--set=max_wal_senders=0",
            "--set=listen_addresses=",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=165,
    )
    if result.returncode:
        raise DeliveryError("ordinary database initialization failed; partial data retained")
    return {
        **observed,
        "initialized": True,
        "cluster_identity": cluster_identity(),
        "services_started": False,
    }


def status() -> dict:
    return dict(
        line.split("=", 1)
        for line in service_install.systemctl(
            "show",
            UNIT,
            "--property=LoadState,ActiveState,SubState,MainPID,Result,Description,Transient,ControlGroup,ExecMainStatus",
        ).splitlines()
        if "=" in line
    )


def initialize(password_file: Path, password_sha: str) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0:
        raise DeliveryError("explicit database initialization requires root operator")
    preflight.protected(password_file)
    configuration.private(password_file)
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(password_file))
        or password_file.stat().st_size != 65
        or digest(password_file) != sha256(password_sha)
    ):
        raise DeliveryError("private database bootstrap fingerprint/path differs")
    validate_password(password_file.read_bytes())
    selected, original = service_install.current_receipt()
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in (
        "credentials.py",
        "dependencies.py",
        "dependency_install.py",
        "dependency_initialize.py",
    ):
        shipped = payload / "delivery/scripts/release" / name
        preflight.protected(shipped)
        if digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("database initializer differs from selected delivery")
    dependency_install.require_stopped()
    dependency_install.check_accounts()
    dependencies.storage_observation("postgres")
    if dependency_install.known_link(
        service_install.SYSTEM_UNITS / "kf-postgres.service"
    ) != dependency_install.ROOT / (selected["release_id"] + "." + selected["configuration_id"]):
        raise DeliveryError("database dependency snapshot is not the selected binding")
    current = status()
    if (
        current.get("LoadState") != "not-found"
        or current.get("ActiveState") != "inactive"
        or current.get("MainPID") != "0"
    ):
        raise DeliveryError("unknown or residual database initialization unit")
    if RECEIPT.exists() or RECEIPT.is_symlink():
        preflight.protected(RECEIPT)
        configuration.private(RECEIPT)
        recorded = validate_receipt(read_json(RECEIPT, 8192))
        if recorded["bootstrap_sha256"] != password_sha or canonical(
            recorded["cluster_identity"]
        ) != canonical(cluster_identity()):
            raise DeliveryError(
                "database initialization identity/credential changed; explicit recovery required"
            )
        return {
            "initialized": True,
            "reused": True,
            "credentials_authenticated": False,
            "migrated": False,
            "services_started": False,
            "execution_verified": False,
        }
    if DATA.exists() or DATA.is_symlink():
        raise DeliveryError("unrecorded or partial database target; explicit recovery required")
    description = (
        "KF PostgreSQL initialization "
        + original["source_sha"]
        + "."
        + selected["configuration_id"]
    )
    command = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--unit=" + UNIT,
        "--description=" + description,
        "--slice=kfonlinejudge.slice",
    ]
    properties = {
        "Type": "exec",
        "RemainAfterExit": "yes",
        "User": "postgres",
        "Group": "postgres",
        "UMask": "0077",
        "MemoryMax": "512M",
        "MemorySwapMax": "0",
        "TasksMax": "64",
        "CPUQuota": "100%",
        "LimitCORE": "0",
        "LimitFSIZE": "256M",
        "LimitNOFILE": "256",
        "LimitSTACK": "16M",
        "RuntimeMaxSec": "180",
        "TimeoutStartSec": "10",
        "TimeoutStopSec": "15",
        "Restart": "no",
        "KillMode": "control-group",
        "NoNewPrivileges": "yes",
        "CapabilityBoundingSet": "",
        "AmbientCapabilities": "",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "ProtectControlGroups": "yes",
        "PrivateDevices": "yes",
        "PrivateTmp": "yes",
        "PrivateNetwork": "yes",
        "RestrictAddressFamilies": "AF_UNIX",
        "ReadWritePaths": dependencies.STORAGE["postgres"]["path"],
        "RequiresMountsFor": dependencies.STORAGE["postgres"]["path"],
        "LoadCredential": "bootstrap:" + str(password_file),
    }
    for key, value in properties.items():
        command += ["--property=" + key + "=" + value]
    command += [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "LC_ALL=C.UTF-8",
        *python_environment(payload).split(),
        str(payload / "python/bin/python3.13"),
        "-s",
        "-P",
        "-m",
        "scripts.release.dependency_initialize",
        "--run-in-unit",
    ]
    dispatched = False
    try:
        dispatched = True
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        if result.returncode:
            raise DeliveryError("database initialization dispatch failed")
        deadline = time.monotonic() + 185
        while True:
            actual = status()
            if actual.get("Description") != description or actual.get("Transient") != "yes":
                raise DeliveryError("database initialization unit ownership differs")
            if actual.get("SubState") == "exited" and actual.get("ActiveState") == "active":
                break
            if (
                actual.get("ActiveState") not in {"active", "activating"}
                or time.monotonic() >= deadline
            ):
                raise DeliveryError(
                    "database initialization failed/expired; partial state retained "
                    f"(result={actual.get('Result', 'unknown')}, "
                    f"exit={actual.get('ExecMainStatus', 'unknown')})"
                )
            time.sleep(0.5)
        if actual.get("Result") != "success" or actual.get("MainPID") != "0":
            raise DeliveryError("database initialization unit failed; partial state retained")
        recorded = validate_receipt(
            {
                "schema_version": 1,
                "source_sha": original["source_sha"],
                "configuration_id": selected["configuration_id"],
                "cluster_identity": cluster_identity(),
                "bootstrap_sha256": password_sha,
                "initialized": True,
                "migrated": False,
                "services_started": False,
                "execution_verified": False,
            }
        )
        if install.selection() != selected:
            raise DeliveryError("selected binding changed during database initialization")
        install.exclusive_file(RECEIPT, canonical(recorded), 0o600)
    finally:
        if dispatched:
            stop_owned_unit(description)
    return {
        "initialized": True,
        "reused": False,
        "credentials_authenticated": False,
        "migrated": False,
        "services_started": False,
        "execution_verified": False,
    }


def stop_owned_unit(description: str) -> None:
    """A failed unit may be stopped; failure is never initialization success."""
    actual = status()
    if actual.get("LoadState") != "not-found":
        if actual.get("Description") != description or actual.get("Transient") != "yes":
            raise DeliveryError("foreign initialization unit; refused cleanup")
        group = actual.get("ControlGroup", "")
        if group and group != "/kfonlinejudge.slice/" + UNIT:
            raise DeliveryError("foreign initialization cgroup; refused cleanup")
        service_install.systemctl("stop", UNIT)
        actual = status()
        if actual.get("LoadState") != "not-found" and (
            actual.get("Description") != description
            or actual.get("Transient") != "yes"
            or actual.get("ControlGroup", "") not in {"", "/kfonlinejudge.slice/" + UNIT}
        ):
            raise DeliveryError("initialization unit ownership changed during cleanup")
    if (
        actual.get("ActiveState") not in {"inactive", "failed"}
        or actual.get("MainPID") != "0"
        or CGROUP.exists()
        and (
            (CGROUP / "cgroup.procs").read_text().strip()
            or "populated 0" not in (CGROUP / "cgroup.events").read_text().splitlines()
        )
    ):
        raise DeliveryError("database initialization cleanup unconfirmed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit bounded PostgreSQL initialization")
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--password-sha256")
    parser.add_argument("--run-in-unit", action="store_true")
    args = parser.parse_args()
    try:
        if args.run_in_unit:
            if args.password_file is not None or args.password_sha256 is not None:
                raise DeliveryError("unexpected initialization unit override")
            result = run_in_unit()
        else:
            if args.password_file is None or args.password_sha256 is None:
                raise DeliveryError("explicit private bootstrap inputs required")
            result = initialize(args.password_file, args.password_sha256)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Database initialization refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
