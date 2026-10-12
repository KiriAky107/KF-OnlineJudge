"""Compose public contracts and private identities without application state writes."""

import hashlib
import json
import os
import re
import secrets
import stat
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import manifest, preflight
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256

PROFILES = {
    "api": "kf_platform",
    "coordinator": "kf_coordinator",
    "import": "kf_problem_import",
    "identity": "kf_identity_operator",
    "migration": "kf_migrator",
}
IMPORT_CGROUP = "/sys/fs/cgroup/kfonlinejudge.slice/kf-problem-import.service"
CONFIG_FILES = {
    "api.env",
    "outbox.env",
    "import.env",
    "identity.env",
    "migration.env",
    "coordinator.json",
    "worker.json",
    "runtime.json",
    "admission.json",
    "measurement.json",
}


def private(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
    ):
        raise DeliveryError("private single-link operator input required")


def profile(path: Path) -> dict:
    private(path)
    return validate_profile(read_json(path, 16384))


def validate_profile(value: dict) -> dict:
    value = fields(
        value,
        {"schema_version", "origin", "worker_id", "measurement_nonce"},
        "node profile",
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise DeliveryError("unsupported node profile")
    origin = value["origin"]
    if not isinstance(origin, str) or len(origin) > 253:
        raise DeliveryError("invalid browser origin")
    parts = urlsplit(origin)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", parts.hostname)
        or parts.port not in (None, 443)
    ):
        raise DeliveryError("explicit HTTPS origin required")
    try:
        worker = uuid.UUID(value["worker_id"])
    except (ValueError, TypeError, AttributeError) as error:
        raise DeliveryError("invalid worker identity") from error
    if str(worker) != value["worker_id"] or worker.int == 0:
        raise DeliveryError("canonical nonzero worker UUID required")
    if value["measurement_nonce"] is not None:
        sha256(value["measurement_nonce"])
    return value


def connections(path: Path) -> dict:
    private(path)
    return validate_connections(read_json(path, 16384))


def validate_connections(value: dict) -> dict:
    value = fields(value, {"schema_version", "database", "amqp"}, "service connections")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise DeliveryError("unsupported service connections")
    database = fields(value["database"], set(PROFILES), "database connections")
    passwords = set()
    for name, raw in database.items():
        if not isinstance(raw, str) or len(raw) > 1024:
            raise DeliveryError("invalid database connection")
        parts = urlsplit(raw)
        unix = (
            name == "import"
            and parts.hostname is None
            and parse_qs(parts.query, strict_parsing=True) == {"host": ["/run/postgresql"]}
        )
        network = (
            name != "import"
            and parts.hostname == "127.0.0.1"
            and parts.port == 5432
            and not parts.query
        )
        if (
            parts.scheme != "postgresql"
            or parts.username != PROFILES[name]
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", parts.password or "")
            or parts.path != "/kf_onlinejudge"
            or parts.fragment
            or not (unix or network)
        ):
            raise DeliveryError("database authority or loopback/socket policy mismatch")
        if parts.password in passwords:
            raise DeliveryError("service credentials must be independent")
        passwords.add(parts.password)
    broker = fields(value["amqp"], {"outbox", "worker"}, "broker connections")
    for name, raw in broker.items():
        if not isinstance(raw, str) or len(raw) > 1024:
            raise DeliveryError("invalid broker connection")
        parts = urlsplit(raw)
        if (
            parts.scheme != "amqp"
            or parts.hostname != "127.0.0.1"
            or parts.port != 5672
            or parts.username != "kf_" + name
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", parts.password or "")
            or parts.path != "/kf"
            or parts.query
            or parts.fragment
        ):
            raise DeliveryError("broker authority or loopback policy mismatch")
        if parts.password in passwords:
            raise DeliveryError("service credentials must be independent")
        passwords.add(parts.password)
    return value


def environmental(values: dict) -> bytes:
    """JSON strings are valid systemd EnvironmentFile double-quoted values here.

    Input URLs/passwords cannot contain dollar/backtick/newline expansions;
    only trusted JSON admission data and known fixed paths appear otherwise.
    """
    return "".join(
        key + "=" + json.dumps(value, separators=(",", ":")) + "\n"
        for key, value in sorted(values.items())
    ).encode()


def measured_hardware() -> dict:
    value = preflight.hardware()
    # Operator command cgroup does not define execution scheduling. The fixed
    # execution/supervisor budgets below are bound separately and read back by
    # Sandbox; no transient installer PID/unit name is part of the epoch.
    value.pop("parent_limits")
    # Bind stable node-wide scheduling and ancestor constraints too. Per-service
    # ceilings are in the policy; Sandbox separately verifies their actual
    # values on every execution. This is not a transient operator unit snapshot.
    value["node_scheduler"] = {}
    for label, group in (
        ("root", Path("/sys/fs/cgroup")),
        ("node", Path("/sys/fs/cgroup/kfonlinejudge.slice")),
    ):
        value["node_scheduler"][label] = {
            name: (group / name).read_text().strip() if (group / name).exists() else None
            for name in ("cpu.max", "memory.max", "cpuset.cpus.effective")
        }
    return value


def admission_runtime(payload: Path, data: dict) -> tuple[dict, dict]:
    from kf_oj.contracts.judging_v2 import AdmissionRegistry

    admission = read_json(payload / "deploy/judge.admission.example.json", 16384)
    root = read_json(payload / "deploy/sandbox.managed.runtime.example.json", 16384)
    for cap in admission["multi_runtime"]["capabilities"]:
        image = {
            "c17": "gnu",
            "cpp20": "gnu",
            "python313": "python313",
            "java21": "java21",
        }[cap["language"]]
        cap["toolchain"] = data["images"][image]
    admission = AdmissionRegistry.model_validate(admission).model_dump(mode="json")
    state = "/var/lib/kf-judge/images/" + data["release_id"]
    root["image_root"] = state + "/legacy-gnu"
    root["delegated_cgroup_root"] = "/sys/fs/cgroup/kfonlinejudge.slice/kf-judge-tasks"
    root["identities"] = {
        "worker_uid": preflight.ACCOUNTS["kf-worker"],
        "worker_gid": preflight.ACCOUNTS["kf-worker"],
        "first_task_uid": preflight.TASK_UID,
        "first_task_gid": preflight.TASK_GID,
        "count": 1,
    }
    for image, entry in zip(("gnu", "python313", "java21"), root["runtime_images"], strict=True):
        entry["image_root"] = state + "/" + image
        entry["runtime_identity"] = data["images"][image]
    if root["max_concurrent_runs"] != 1:
        raise DeliveryError("single-slot configuration required")
    return admission, root


def compose(
    payload: Path,
    data: dict,
    node: dict,
    endpoints: dict,
    *,
    attest: bool,
    service_tokens: tuple[str, str] | None = None,
) -> dict:
    """Caller has verified the full delivery inventory before this composition."""
    admission, root = admission_runtime(payload, data)
    measurement = {
        "schema_version": 1,
        "hardware": measured_hardware(),
        "source_sha": data["source_sha"],
        "images": data["images"],
        "admission": admission,
        "sandbox_budget": root["aggregate"],
        "measurement_nonce": node["measurement_nonce"],
        "worker_id": node["worker_id"],
    }
    epoch = None
    if attest:
        if node["measurement_nonce"] is None:
            raise DeliveryError("explicit fresh measurement nonce required for attestation")
        epoch = hashlib.sha256(canonical(measurement)).hexdigest()
    worker_token, platform_token = service_tokens or (
        secrets.token_hex(32),
        secrets.token_hex(32),
    )
    coordinator = {
        "listen": "127.0.0.1:8100",
        "database_url": endpoints["database"]["coordinator"],
        "workers": [{"id": node["worker_id"], "token": worker_token, "measurement_epoch": epoch}],
        "platform_token": platform_token,
        "admission": admission,
        "lease_seconds": 120,
        "recovery_seconds": 5,
    }
    worker = {
        "worker_id": node["worker_id"],
        "service_token": worker_token,
        "coordinator_url": "http://127.0.0.1:8100",
        "platform_url": "http://127.0.0.1:8000",
        "amqp_url": endpoints["amqp"]["worker"],
        "socket": "/var/lib/kf-judge/factory/control.sock",
        "staging": "/var/lib/kf-onlinejudge/worker",
        "admission": admission,
        "control_timeout_ms": 5000,
    }
    common = {
        "KF_OJ_ENVIRONMENT": "production",
        "KF_OJ_LOG_LEVEL": "INFO",
        "KF_OJ_BROWSER_ORIGINS": canonical([node["origin"]]).decode().strip(),
        "KF_OJ_ARTIFACT_ROOT": "/var/lib/kf-onlinejudge/artifacts",
        "KF_OJ_IMPORT_STAGING_ROOT": "/var/lib/kf-onlinejudge/imports",
        "KF_OJ_ARTIFACT_MAX_BYTES": "536870912",
        "KF_OJ_ARTIFACT_MAX_FILES": "4096",
    }

    def backend_url(name):
        return endpoints["database"][name].replace("postgresql:", "postgresql+asyncpg:", 1)

    api = {
        **common,
        "KF_OJ_DATABASE_URL": backend_url("api"),
        "KF_OJ_JUDGE_ADMISSION": canonical(admission).decode().strip(),
        "KF_OJ_COORDINATOR_URL": "http://127.0.0.1:8100",
        "KF_OJ_COORDINATOR_TOKEN": platform_token,
    }
    files = {
        "api.env": environmental(api),
        "outbox.env": environmental(
            {
                **common,
                "KF_OJ_DATABASE_URL": backend_url("api"),
                "KF_OJ_AMQP_URL": endpoints["amqp"]["outbox"],
            }
        ),
        "import.env": environmental(
            {
                **common,
                "KF_OJ_DATABASE_URL": backend_url("import"),
                "KF_OJ_IMPORT_WORKER_CGROUP": IMPORT_CGROUP,
                "KF_OJ_JUDGE_ADMISSION": canonical(admission).decode().strip(),
            }
        ),
        "identity.env": environmental(
            {
                **common,
                "KF_OJ_DATABASE_URL": backend_url("api"),
                "KF_OJ_IDENTITY_OPERATOR_DATABASE_URL": backend_url("identity"),
            }
        ),
        "migration.env": environmental({**common, "KF_OJ_DATABASE_URL": backend_url("migration")}),
        "admission.json": canonical(admission),
        "runtime.json": canonical(root),
        "coordinator.json": canonical(coordinator),
        "worker.json": canonical(worker),
        "measurement.json": canonical(
            {"binding": measurement, "epoch": epoch, "operator_attested": attest}
        ),
    }
    return files


def validate_receipt(value: dict, *, installed: bool) -> dict:
    value = fields(
        value,
        {
            "schema_version",
            "source_sha",
            "release_id",
            "configuration_id",
            "manifest_sha256",
            "configuration_files",
            "operator_attested_measurement",
            "installed",
            "execution_verified",
        },
        "configuration receipt",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["source_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["source_sha"])
        or value["release_id"] != "sd-" + value["source_sha"][:12]
        or not isinstance(value["configuration_id"], str)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", value["configuration_id"])
        or type(value["operator_attested_measurement"]) is not bool
        or value["installed"] is not installed
        or value["execution_verified"] is not False
    ):
        raise DeliveryError("invalid configuration binding")
    sha256(value["manifest_sha256"])
    names = CONFIG_FILES
    if installed:
        names = {
            installed_path(value["release_id"], value["configuration_id"], name).as_posix()
            for name in CONFIG_FILES
        }
    fields(value["configuration_files"], names, "configuration inventory")
    for checksum in value["configuration_files"].values():
        sha256(checksum)
    hashes = {
        name: value["configuration_files"][
            installed_path(value["release_id"], value["configuration_id"], name).as_posix()
            if installed
            else name
        ]
        for name in CONFIG_FILES
    }
    if value["configuration_id"] != configuration_id(
        value["source_sha"], value["manifest_sha256"], hashes
    ):
        raise DeliveryError("configuration batch identity mismatch")
    return value


def configuration_id(source_sha: str, manifest_sha: str, hashes: dict) -> str:
    return "cfg-" + hashlib.sha256(canonical([source_sha, manifest_sha, hashes])).hexdigest()[:24]


def installed_receipt_path(release_id: str, batch: str) -> Path:
    return Path("/etc/kf-onlinejudge") / (release_id + "." + batch + ".configuration.json")


def installed_path(release_id: str, batch: str, name: str) -> Path:
    base = "/etc/kf-judge" if name == "runtime.json" else "/etc/kf-onlinejudge"
    return Path(base) / (release_id + "." + batch + "." + name)


def environment_read(contents: bytes) -> dict:
    if len(contents) > 32768:
        raise DeliveryError("environment file exceeds limit")
    values = {}
    for line in contents.decode().splitlines():
        name, sep, raw = line.partition("=")
        if not sep or not re.fullmatch(r"KF_OJ_[A-Z_]+", name) or name in values:
            raise DeliveryError("invalid service environment")
        value = json.loads(raw)
        if not isinstance(value, str) or "\n" in value or "\r" in value or "\x00" in value:
            raise DeliveryError("invalid service environment value")
        values[name] = value
    return values


def validate_contents(paths: dict[str, Path], payload: Path, data: dict, *, attest: bool) -> None:
    """Validate public policy, endpoint roles and epoch binding, without health claims."""
    if set(paths) != CONFIG_FILES:
        raise DeliveryError("incomplete service configuration")
    raw = {}
    for name, path in paths.items():
        private(path)
        if path.stat().st_size > 32768:
            raise DeliveryError("service configuration exceeds limit")
        raw[name] = path.read_bytes()
    envs = {name: environment_read(raw[name]) for name in CONFIG_FILES if name.endswith(".env")}
    measurement = fields(
        read_json(paths["measurement.json"], 32768),
        {"binding", "epoch", "operator_attested"},
        "measurement record",
    )
    binding = fields(
        measurement["binding"],
        {
            "schema_version",
            "hardware",
            "source_sha",
            "images",
            "admission",
            "sandbox_budget",
            "measurement_nonce",
            "worker_id",
        },
        "measurement binding",
    )
    origins = json.loads(envs["api.env"].get("KF_OJ_BROWSER_ORIGINS", "null"))
    if not isinstance(origins, list) or len(origins) != 1:
        raise DeliveryError("one explicit browser origin required")
    node = validate_profile(
        {
            "schema_version": 1,
            "origin": origins[0],
            "worker_id": binding["worker_id"],
            "measurement_nonce": binding["measurement_nonce"],
        }
    )
    coordinator = fields(
        read_json(paths["coordinator.json"], 32768),
        {
            "listen",
            "database_url",
            "workers",
            "platform_token",
            "admission",
            "lease_seconds",
            "recovery_seconds",
        },
        "coordinator configuration",
    )
    worker = fields(
        read_json(paths["worker.json"], 32768),
        {
            "worker_id",
            "service_token",
            "coordinator_url",
            "platform_url",
            "amqp_url",
            "socket",
            "staging",
            "admission",
            "control_timeout_ms",
        },
        "worker configuration",
    )

    def database_url(name: str, key="KF_OJ_DATABASE_URL"):
        raw = envs[name].get(key)
        if not isinstance(raw, str) or not raw.startswith("postgresql+asyncpg:"):
            raise DeliveryError("backend database driver mismatch")
        return raw.replace("postgresql+asyncpg:", "postgresql:", 1)

    endpoints = validate_connections(
        {
            "schema_version": 1,
            "database": {
                "api": database_url("api.env"),
                "coordinator": coordinator.get("database_url"),
                "import": database_url("import.env"),
                "migration": database_url("migration.env"),
                "identity": database_url("identity.env", "KF_OJ_IDENTITY_OPERATOR_DATABASE_URL"),
            },
            "amqp": {
                "outbox": envs["outbox.env"].get("KF_OJ_AMQP_URL"),
                "worker": worker.get("amqp_url"),
            },
        }
    )
    worker_token, platform_token = (
        sha256(worker.get("service_token")),
        sha256(coordinator.get("platform_token")),
    )
    if worker_token == platform_token:
        raise DeliveryError("service identities overlap")
    expected = compose(
        payload,
        data,
        node,
        endpoints,
        attest=attest,
        service_tokens=(worker_token, platform_token),
    )
    for name in CONFIG_FILES:
        # JSON comparisons via canonical bytes distinguish true/1 and null/[];
        # environmental files must have precisely the generated keys/encoding.
        if name.endswith(".json"):
            match = canonical(read_json(paths[name], 32768)) == expected[name]
        else:
            match = raw[name] == expected[name]
        if not match:
            raise DeliveryError("configuration/identity/measurement policy mismatch")


def check_installed(receipt_path: Path) -> dict:
    from .install import RELEASES

    preflight.protected(receipt_path)
    private(receipt_path)
    receipt = validate_receipt(read_json(receipt_path, 16384), installed=True)
    release = RELEASES / receipt["release_id"]
    preflight.protected(release, directory=True)
    data = manifest.verify(release, receipt["manifest_sha256"])
    if receipt["source_sha"] != data["source_sha"]:
        raise DeliveryError("configuration source differs from release")
    paths = {}
    for name in CONFIG_FILES:
        path = installed_path(receipt["release_id"], receipt["configuration_id"], name)
        preflight.protected(path)
        if digest(path) != receipt["configuration_files"][path.as_posix()]:
            raise DeliveryError("installed configuration fingerprint differs")
        paths[name] = path
    validate_contents(
        paths,
        release / "payload",
        data,
        attest=receipt["operator_attested_measurement"],
    )
    return receipt


def generate(
    bundle: Path,
    expected: str,
    node_path: Path,
    connections_path: Path,
    output: Path,
    *,
    attest: bool = False,
) -> dict:
    data = manifest.verify(bundle, expected)
    node, endpoints = profile(node_path), connections(connections_path)
    if output.exists() or output.is_symlink():
        raise DeliveryError("configuration output must be new; secrets are never overwritten")
    files = compose(bundle / "payload", data, node, endpoints, attest=attest)
    output.mkdir(mode=0o700)
    hashes = {}
    for name, contents in files.items():
        with (output / name).open("xb") as stream:
            os.chmod(output / name, 0o600)
            stream.write(contents)
        hashes[name] = digest(output / name)
    receipt = {
        "schema_version": 1,
        "source_sha": data["source_sha"],
        "release_id": data["release_id"],
        "configuration_id": configuration_id(data["source_sha"], expected, hashes),
        "manifest_sha256": expected,
        "configuration_files": hashes,
        "operator_attested_measurement": attest,
        "installed": False,
        "execution_verified": False,
    }
    (output / "receipt.json").write_bytes(canonical(receipt))
    (output / "receipt.json").chmod(0o600)
    return {
        "configuration_generated": True,
        "source_sha": data["source_sha"],
        "configuration_id": receipt["configuration_id"],
        "receipt_sha256": digest(output / "receipt.json"),
        "operator_attested_measurement": attest,
        "installed": False,
        "execution_verified": False,
    }


def install_configuration(source: Path, expected_receipt: str) -> dict:
    from .install import RELEASES, exclusive_file

    if os.geteuid() != 0:
        raise DeliveryError("configuration installation requires root")
    preflight.protected(source, directory=True)
    receipt_path = source / "receipt.json"
    preflight.protected(receipt_path)
    if digest(receipt_path) != sha256(expected_receipt):
        raise DeliveryError("configuration receipt fingerprint differs")
    receipt = validate_receipt(read_json(receipt_path, 16384), installed=False)
    data = manifest.verify(RELEASES / receipt["release_id"], receipt["manifest_sha256"])
    if (
        receipt["source_sha"] != data["source_sha"]
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
    ):
        raise DeliveryError("configuration source mismatch")
    if {p.name for p in source.iterdir()} != CONFIG_FILES | {"receipt.json"}:
        raise DeliveryError("unexpected configuration member")
    # Validate the entire group before publishing any configuration member.
    for name, checksum in receipt["configuration_files"].items():
        preflight.protected(source / name)
        if digest(source / name) != checksum:
            raise DeliveryError("configuration bytes differ")
    validate_contents(
        {name: source / name for name in CONFIG_FILES},
        RELEASES / data["release_id"] / "payload",
        data,
        attest=receipt["operator_attested_measurement"],
    )
    installed = {}
    for name, checksum in receipt["configuration_files"].items():
        member = source / name
        preflight.protected(member)
        private(member)
        if digest(member) != sha256(checksum):
            raise DeliveryError("configuration bytes differ")
        target = installed_path(data["release_id"], receipt["configuration_id"], name)
        exclusive_file(target, member.read_bytes(), 0o600)
        installed[str(target)] = checksum
    receipt = {**receipt, "configuration_files": installed, "installed": True}
    target = installed_receipt_path(data["release_id"], receipt["configuration_id"])
    exclusive_file(target, canonical(receipt), 0o600)
    return {
        "configuration_installed": True,
        "source_sha": data["source_sha"],
        "configuration_id": receipt["configuration_id"],
        "receipt_sha256": digest(target),
        "execution_verified": False,
    }
