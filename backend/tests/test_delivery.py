"""Pure delivery-contract tests; these do not claim installation or VM safety."""

import importlib
import json
import secrets
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
policy = importlib.import_module("scripts.release.policy")
manifest = importlib.import_module("scripts.release.manifest")
builder = importlib.import_module("scripts.release.build")
configuration = importlib.import_module("scripts.release.configuration")
architecture = importlib.import_module("scripts.check_architecture")
proxy = importlib.import_module("scripts.release.proxy")
services = importlib.import_module("scripts.release.services")
service_bootstrap = importlib.import_module("scripts.release.service_bootstrap")
service_install = importlib.import_module("scripts.release.service_install")
factory_service = importlib.import_module("scripts.release.factory_service")
dependencies = importlib.import_module("scripts.release.dependencies")
dependency_install = importlib.import_module("scripts.release.dependency_install")
dependency_initialize = importlib.import_module("scripts.release.dependency_initialize")
node_health = importlib.import_module("scripts.release.node_health")
broker_health = importlib.import_module("scripts.release.broker_health")
lifecycle = importlib.import_module("scripts.release.lifecycle")
journal = importlib.import_module("scripts.release.journal")


def test_delivery_authorities_and_layout():
    contract = policy.contract(ROOT / "deploy/release/contract.v1.json")
    assert contract["services"]["worker"] == "none"
    assert contract["services"]["sandbox-factory"] == "none"
    assert contract["layout"]["task_cgroup"] != contract["layout"]["judge_state"]
    assert contract["capacity"]["execution_slots"] == 1
    assert contract["recovery"]["consistent_cut_rpo_seconds"] == 0


@pytest.mark.parametrize("value", [True, 0, 50001, "1"])
def test_integer_resource_limits_cannot_be_coerced(value):
    with pytest.raises(policy.DeliveryError):
        policy.integer(value, 1, 50000, "entry count")


@pytest.mark.parametrize(
    "name",
    [
        "docs/a.md",
        "backend/.env",
        "backend/../secrets",
        "/bin/tool",
        "images/.ssh/id",
        "site/entry.pyc",
    ],
)
def test_private_and_noncanonical_paths_are_not_payload(name):
    with pytest.raises(policy.DeliveryError):
        policy.relative_name(name)


def test_expected_hash_is_external_and_not_placeholder():
    for value in ("0" * 64, "a" * 64, "bad"):
        with pytest.raises(policy.DeliveryError):
            policy.sha256(value)


def test_no_silent_contract_fields(tmp_path):
    value = json.loads((ROOT / "deploy/release/contract.v1.json").read_text())
    value["ignore_isolation"] = True
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(value))
    with pytest.raises(policy.DeliveryError, match="contract fields"):
        policy.contract(path)


def test_duplicate_metadata_key_is_rejected():
    with pytest.raises(policy.DeliveryError, match="duplicate"):
        json.loads(
            '{"schema_version":1,"schema_version":2}', object_pairs_hook=policy.unique_object
        )


def test_inventory_of_trusted_read_only_files(tmp_path):
    directory = tmp_path / "backend"
    directory.mkdir()
    file = directory / "version.txt"
    file.write_bytes(b"accepted-source\n")
    file.chmod(0o444)
    directory.chmod(0o555)
    entries = manifest.inventory(tmp_path)
    assert entries[0]["path"] == "backend/version.txt"
    assert entries[0]["bytes"] == 16
    assert manifest.directories(tmp_path) == ["backend"]


def test_delivery_cannot_import_database_state(tmp_path, monkeypatch):
    (tmp_path / "bad.py").write_text("from kf_oj.modules.submissions.models import Evaluation\n")
    monkeypatch.setattr(architecture, "RELEASE", tmp_path)
    assert len(architecture.release_violations()) == 1


def test_delivery_may_use_public_wire_contracts(tmp_path, monkeypatch):
    (tmp_path / "public.py").write_text("from kf_oj.contracts.judging_v2 import NodeAdmission\n")
    monkeypatch.setattr(architecture, "RELEASE", tmp_path)
    assert architecture.release_violations() == []


def test_signed_guest_library_alias_uses_guest_root(tmp_path):
    root = tmp_path / "runtime"
    library = root / "services/usr/lib/library.so.1"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"public-library-data")
    alias = library.parent / "library.so"
    alias.symlink_to("/usr/lib/library.so.1")
    records = {
        "services/usr/lib/library.so": {"link": "/usr/lib/library.so.1"},
        "services/usr/lib/library.so.1": {
            "bytes": library.stat().st_size,
            "sha256": policy.digest(library),
        },
    }
    target = tmp_path / "payload/lib/library.so"
    builder.curated(root, records, "services/usr/lib/library.so", target)
    assert target.read_bytes() == library.read_bytes()
    assert not target.is_symlink()


@pytest.fixture
def delivery_configuration(tmp_path, monkeypatch):
    """Synthetic operator files only; no services, DB or user program are run."""
    import hashlib

    images = {}
    for name, toolchain, kind, version in (
        ("gnu", "gnu-cxx-v1", "linux_gnu_cxx_x86_64_v1", "16.2.1+r23+gd564253eb6c8"),
        ("python313", "cpython313-v1", "linux_cpython313_x86_64_v1", "3.13.16"),
        ("java21", "openjdk21-v1", "linux_openjdk21_x86_64_v1", "21.0.12.1+1-LTS"),
    ):
        images[name] = {
            "toolchain_id": toolchain,
            "image_kind": kind,
            "image_revision": 1,
            "runtime_version": version,
            "image_digest": hashlib.sha256(name.encode()).hexdigest(),
        }
    data = {
        "images": images,
        "release_id": "sd-123456789abc",
        "source_sha": "123456789abc" + "d" * 28,
    }
    node = {
        "schema_version": 1,
        "origin": "https://oj.test",
        "worker_id": "a28adc33-8fbd-4a33-8b21-7a46431e506c",
        "measurement_nonce": secrets.token_hex(32),
    }
    urls = {}
    for name, role in configuration.PROFILES.items():
        password = secrets.token_urlsafe(32)
        authority = "" if name == "import" else "127.0.0.1:5432"
        urls[name] = f"postgresql://{role}:{password}@{authority}/kf_onlinejudge"
        if name == "import":
            urls[name] += "?host=/run/postgresql"
    endpoints = {
        "schema_version": 1,
        "database": urls,
        "amqp": {
            name: f"amqp://kf_{name}:{secrets.token_urlsafe(32)}@127.0.0.1:5672/kf"
            for name in ("worker", "outbox")
        },
    }
    configuration.validate_profile(node)
    configuration.validate_connections(endpoints)
    observed = {
        "kernel": "6.18-fixture",
        "architecture": "x86_64",
        "online_cpus": 2,
        "processor": {"model name": "fixture"},
    }
    monkeypatch.setattr(configuration, "measured_hardware", lambda: dict(observed))
    raw = configuration.compose(ROOT, data, node, endpoints, attest=True)
    paths = {}
    for name, content in raw.items():
        path = tmp_path / name
        path.write_bytes(content)
        path.chmod(0o600)
        paths[name] = path
    return paths, raw, data, node, endpoints, observed


def test_generated_configuration_preserves_wire_policies_and_backend_settings(
    delivery_configuration, monkeypatch
):
    from kf_oj.core.settings import Settings

    paths, raw, data, _, _, _ = delivery_configuration
    configuration.validate_contents(paths, ROOT, data, attest=True)
    env = configuration.environment_read(raw["api.env"])
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg:")
    admission = json.loads(raw["admission.json"])
    coordinator, worker, root = [
        json.loads(raw[name]) for name in ("coordinator.json", "worker.json", "runtime.json")
    ]
    assert settings.judge_admission.model_dump(mode="json") == admission
    assert coordinator["admission"] == worker["admission"] == admission
    assert "database_url" not in worker and "database_url" not in root
    assert root["identities"]["count"] == 1
    assert root["image_root"] not in [image["image_root"] for image in root["runtime_images"]]
    assert admission["legacy"]["toolchain"] == {
        "toolchain_id": "gnu-cxx-v1",
        "image_kind": "linux_gnu_cxx_x86_64_v1",
        "image_revision": 1,
    }


def test_epoch_rebinds_when_kernel_changes(delivery_configuration):
    paths, raw, data, node, endpoints, observed = delivery_configuration
    before = json.loads(raw["measurement.json"])["epoch"]
    observed["kernel"] = "6.19-fixture"
    with pytest.raises(policy.DeliveryError, match="measurement policy mismatch"):
        configuration.validate_contents(paths, ROOT, data, attest=True)
    after = configuration.compose(ROOT, data, node, endpoints, attest=True)
    assert json.loads(after["measurement.json"])["epoch"] != before


def test_unattested_configuration_has_no_comparable_epoch(delivery_configuration):
    _, _, data, node, endpoints, _ = delivery_configuration
    raw = configuration.compose(ROOT, data, node, endpoints, attest=False)
    assert json.loads(raw["coordinator.json"])["workers"][0]["measurement_epoch"] is None


def test_configuration_receipt_is_bound_before_path_use():
    source = "123456789abc" + "d" * 28
    hashes = {name: "b" * 64 for name in configuration.CONFIG_FILES}
    receipt = {
        "schema_version": 1,
        "source_sha": source,
        "release_id": "sd-123456789abc",
        "configuration_id": configuration.configuration_id(source, "f" * 64, hashes),
        "manifest_sha256": "f" * 64,
        "configuration_files": hashes,
        "operator_attested_measurement": False,
        "installed": False,
        "execution_verified": False,
    }
    configuration.validate_receipt(receipt, installed=False)
    receipt["installed"] = 0
    with pytest.raises(policy.DeliveryError, match="configuration binding"):
        configuration.validate_receipt(receipt, installed=False)


def test_same_source_can_have_distinct_immutable_configuration_batches():
    source, manifest_sha = "123456789abc" + "d" * 28, "f" * 64
    hashes = {name: "b" * 64 for name in configuration.CONFIG_FILES}
    before = configuration.configuration_id(source, manifest_sha, hashes)
    changed = {**hashes, "measurement.json": "c" * 64}
    after = configuration.configuration_id(source, manifest_sha, changed)
    assert before != after
    assert configuration.installed_path("sd-123456789abc", before, "runtime.json") != (
        configuration.installed_path("sd-123456789abc", after, "runtime.json")
    )


def test_configuration_batch_hash_covers_all_members():
    source, manifest_sha = "123456789abc" + "d" * 28, "f" * 64
    hashes = {name: "b" * 64 for name in configuration.CONFIG_FILES}
    receipt = {
        "schema_version": 1,
        "source_sha": source,
        "release_id": "sd-123456789abc",
        "configuration_id": configuration.configuration_id(source, manifest_sha, hashes),
        "manifest_sha256": manifest_sha,
        "configuration_files": {**hashes, "worker.json": "c" * 64},
        "operator_attested_measurement": False,
        "installed": False,
        "execution_verified": False,
    }
    with pytest.raises(policy.DeliveryError, match="batch identity mismatch"):
        configuration.validate_receipt(receipt, installed=False)
    receipt["configuration_id"] = "cfg-../../private"
    with pytest.raises(policy.DeliveryError, match="configuration binding"):
        configuration.validate_receipt(receipt, installed=False)


@pytest.mark.parametrize(
    "origin",
    [
        "http://oj.test",
        "https://oj.test:443",
        "https://oj.test/",
        "https://oj.test;include",
        "https://user@oj.test",
        "https://oj..test",
        "https://-oj.test",
        "https://OJ.test",
    ],
)
def test_proxy_rejects_ambiguous_and_injected_origins(origin):
    value = {
        "schema_version": 1,
        "origin": origin,
        "certificate_sha256": "b" * 64,
        "key_sha256": "c" * 64,
    }
    with pytest.raises(policy.DeliveryError):
        proxy.validate_profile(value)


def test_proxy_policy_cannot_change_upstream_paths_or_limits():
    value = {
        "schema_version": 1,
        "origin": "https://oj.test",
        "certificate_sha256": "b" * 64,
        "key_sha256": "c" * 64,
        "upstream": "http://127.0.0.1:8100",
    }
    with pytest.raises(policy.DeliveryError, match="proxy profile fields"):
        proxy.validate_profile(value)
    value.pop("upstream")
    value["certificate_sha256"] = "a" * 64
    with pytest.raises(policy.DeliveryError):
        proxy.validate_profile(value)


def test_proxy_is_separate_from_private_execution_control():
    value = {
        "schema_version": 1,
        "origin": "https://oj.test",
        "certificate_sha256": "b" * 64,
        "key_sha256": "c" * 64,
    }
    contents = proxy.render("sd-123456789abc", value).decode()
    assert "127.0.0.1:8100" not in contents and "control.sock" not in contents
    assert "location ~ ^/(api|internal)(/|$) { return 404; }" in contents
    assert "ssl_reject_handshake on" in contents
    assert "proxy_request_buffering off" in contents
    assert "$request_uri" not in contents and "$http_cookie" not in contents
    assert value["key_sha256"] not in contents


def test_ordinary_units_bind_exact_batch_without_inline_secrets():
    source, checksum = "123456789abc" + "d" * 28, "f" * 64
    hashes = {name: "b" * 64 for name in configuration.CONFIG_FILES}
    receipt = {
        "schema_version": 1,
        "source_sha": source,
        "release_id": "sd-123456789abc",
        "configuration_id": configuration.configuration_id(source, checksum, hashes),
        "manifest_sha256": checksum,
        "configuration_files": hashes,
        "operator_attested_measurement": False,
        "installed": False,
        "execution_verified": False,
    }
    units = services.render(receipt)
    assert set(units) == {
        "kf-api.service",
        "kf-outbox.service",
        "kf-coordinator.service",
        "kf-worker.service",
        "kf-problem-import.service",
    }
    for contents in units.values():
        text = contents.decode()
        assert "User=root" not in text
        assert "MemorySwapMax=0" in text and "StartLimitBurst=3" in text
        assert receipt["configuration_id"] in text and receipt["release_id"] in text
        assert "Password=" not in text and "postgresql://" not in text and "amqp://" not in text
    assert "PrivateNetwork=yes" in units["kf-problem-import.service"].decode()
    assert "PrivatePIDs=yes" in units["kf-problem-import.service"].decode()
    assert "RestrictAddressFamilies=AF_UNIX\n" in units["kf-problem-import.service"].decode()
    assert "-m scripts.release.service_bootstrap outbox" in units["kf-outbox.service"].decode()
    assert service_bootstrap.PYTHON_MODULES["outbox"] == ["kf_oj.infra.messaging.publisher"]
    assert "LoadCredential=worker.json:" in units["kf-worker.service"].decode()


def test_systemd_credential_bytes_are_checked_before_private_copy(tmp_path):
    import hashlib
    import os

    path = tmp_path / "configuration.json"
    content = b'{"synthetic_configuration":true}\n'
    path.write_bytes(content)
    path.chmod(0o400)
    expected = hashlib.sha256(content).hexdigest()
    assert service_bootstrap.read_credential(path, expected, os.getuid()) == content
    with pytest.raises(policy.DeliveryError, match="fingerprint mismatch"):
        service_bootstrap.read_credential(path, "b" * 64, os.getuid())
    path.chmod(0o600)
    path.write_bytes(b"x" * 32769)
    with pytest.raises(policy.DeliveryError, match="bounded systemd credential"):
        service_bootstrap.read_credential(path, expected, os.getuid())


def test_import_configuration_has_the_same_required_admission(delivery_configuration):
    _, _, data, node, endpoints, _ = delivery_configuration
    raw = configuration.compose(ROOT, data, node, endpoints, attest=False)
    api = configuration.environment_read(raw["api.env"])
    importer = configuration.environment_read(raw["import.env"])
    assert importer["KF_OJ_JUDGE_ADMISSION"] == api["KF_OJ_JUDGE_ADMISSION"]
    assert set(importer) == service_bootstrap.ROLE_ENVIRONMENT["problem-import"]


def ordinary_observation(role):
    user, memory, pids, _, _, _ = services.PROFILES[role]
    uid = str(service_bootstrap.preflight.ACCOUNTS[user])
    return {
        "cgroup": "/sys/fs/cgroup/kfonlinejudge.slice/kf-" + role + ".service",
        "uids": [uid] * 4,
        "gids": [uid] * 4,
        "groups": [uid],
        "no_new_privileges": "1",
        "capabilities": ["0000000000000000"] * 5,
        "pid": 1,
        "service_limits": {
            "memory.max": str(memory * 1024**2),
            "memory.swap.max": "0",
            "pids.max": str(pids),
            "cpu.max": "100000 100000",
        },
        "node_limits": {
            "memory.max": str(3840 * 1024**2),
            "memory.swap.max": "0",
            "pids.max": "384",
            "cpu.max": "200000 100000",
        },
        "process_limits": {
            "core": [0, 0],
            "file": [64 * 1024**2] * 2,
            "files": [64 if role == "problem-import" else 256] * 2,
            "stack": [16 * 1024**2] * 2,
        },
        "interfaces": ["lo"] if role == "problem-import" else None,
    }


@pytest.mark.parametrize("role", services.PROFILES)
def test_ordinary_guard_parser_requires_effective_bounds(role):
    observed = ordinary_observation(role)
    service_bootstrap.validate_runtime(role, observed)
    observed["node_limits"]["memory.max"] = "max"
    with pytest.raises(policy.DeliveryError, match="effective ordinary/node"):
        service_bootstrap.validate_runtime(role, observed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", 43),
        ("no_new_privileges", "0"),
        ("groups", ["1200", "0"]),
        ("capabilities", ["0000000000000001"] * 5),
        ("interfaces", ["lo", "eth0"]),
    ],
)
def test_import_guard_refuses_missing_pid_privilege_or_network_boundary(field, value):
    observed = ordinary_observation("problem-import")
    observed[field] = value
    with pytest.raises(policy.DeliveryError):
        service_bootstrap.validate_runtime("problem-import", observed)


def test_unit_snapshot_binding_and_inventory_cannot_add_a_privileged_unit():
    value = {
        "schema_version": 1,
        "source_sha": "123456789abc" + "d" * 28,
        "configuration_id": "cfg-" + "e" * 24,
        "configuration_receipt_sha256": "f" * 64,
        "files": {name: "b" * 64 for name in service_install.NAMES},
        "installed": False,
        "services_started": False,
        "execution_verified": False,
    }
    service_install.validate_receipt(value)
    expected = "/etc/kf-onlinejudge/units/sd-123456789abc.cfg-" + "e" * 24
    assert str(service_install.batch_directory(value)) == expected
    assert (
        str(service_install.previous_batch(expected + "/kf-api.service", "kf-api.service"))
        == expected
    )
    value["files"]["kf-sandbox-factory.service"] = "b" * 64
    with pytest.raises(policy.DeliveryError, match="unit inventory"):
        service_install.validate_receipt(value)


@pytest.mark.parametrize(
    "target",
    [
        "/etc/systemd/system/unknown.service",
        "/tmp/sd-123456789abc.cfg-" + "e" * 24 + "/kf-api.service",
        "/etc/kf-onlinejudge/units/sd-123456789abc.cfg-" + "e" * 24 + "/../kf-api.service",
        "/etc//kf-onlinejudge/units/sd-123456789abc.cfg-" + "e" * 24 + "/kf-api.service",
    ],
)
def test_unit_snapshot_does_not_adopt_foreign_or_ambiguous_targets(target):
    with pytest.raises(policy.DeliveryError, match="unknown system unit target"):
        service_install.previous_batch(target, "kf-api.service")


def test_factory_composition_keeps_privileged_boundary_and_finite_lifetime():
    source, checksum = "123456789abc" + "d" * 28, "f" * 64
    hashes = {name: "b" * 64 for name in configuration.CONFIG_FILES}
    receipt = {
        "schema_version": 1,
        "source_sha": source,
        "release_id": "sd-123456789abc",
        "configuration_id": configuration.configuration_id(source, checksum, hashes),
        "manifest_sha256": checksum,
        "configuration_files": hashes,
        "operator_attested_measurement": False,
        "installed": False,
        "execution_verified": False,
    }
    contents = factory_service.render(receipt).decode()
    assert " prepare-factory --config " in contents
    assert " --lifetime-seconds 3600 --max-requests 4096" in contents
    assert "KillMode=mixed" in contents and "Restart=no" in contents
    assert "NoNewPrivileges" not in contents and "SystemCallFilter" not in contents
    assert "PrivatePIDs" not in contents and "ProtectControlGroups" not in contents
    assert "EnvironmentFile" not in contents and "LoadCredential" not in contents
    assert "env -i" in contents and "MemorySwapMax=0" in contents
    assert receipt["configuration_id"] in contents


def test_factory_install_receipt_cannot_claim_execution_or_move_outside_root_batch():
    receipt = {
        "schema_version": 1,
        "source_sha": "123456789abc" + "d" * 28,
        "configuration_id": "cfg-" + "e" * 24,
        "configuration_receipt_sha256": "f" * 64,
        "unit_sha256": "b" * 64,
        "installed": False,
        "services_started": False,
        "execution_verified": False,
    }
    factory_service.validate_receipt(receipt)
    assert str(factory_service.batch_directory(receipt)) == (
        "/etc/kf-judge/units/sd-123456789abc.cfg-" + "e" * 24
    )
    receipt["execution_verified"] = True
    with pytest.raises(policy.DeliveryError, match="factory unit binding"):
        factory_service.validate_receipt(receipt)
    receipt["execution_verified"] = False
    receipt["configuration_id"] = "../foreign"
    with pytest.raises(policy.DeliveryError, match="factory unit binding"):
        factory_service.validate_receipt(receipt)


def test_dependency_definitions_are_bound_to_two_non_admin_users_and_existing_queue(
    delivery_configuration,
):
    import base64
    import hashlib
    from urllib.parse import urlsplit

    from kf_oj.infra.messaging.publisher import QUEUE_ARGUMENTS

    _, _, _, _, endpoints, _ = delivery_configuration
    definition = dependencies.broker_definitions(endpoints)
    assert {u["name"] for u in definition["users"]} == {"kf_outbox", "kf_worker"}
    assert all(not u["tags"] for u in definition["users"])
    for user in definition["users"]:
        role = user["name"].removeprefix("kf_")
        packed = base64.b64decode(user["password_hash"])
        password = urlsplit(endpoints["amqp"][role]).password
        assert (
            len(packed) == 36
            and packed[4:] == hashlib.sha256(packed[:4] + password.encode()).digest()
        )
        assert "password" not in user
    assert definition["queues"][0]["arguments"] == QUEUE_ARGUMENTS
    permissions = {p["user"]: p for p in definition["permissions"]}
    assert permissions["kf_outbox"]["read"] == "^$"
    assert permissions["kf_worker"]["write"] == "^$"
    assert definition["vhosts"] == [{"name": "kf", "metadata": {"default_queue_type": "classic"}}]


def test_dependency_templates_keep_socket_auth_network_and_actual_limit_scope():
    files = dependencies.render("sd-123456789abc", "cfg-" + "e" * 24)
    hba = files["pg_hba.conf"].decode()
    assert "trust" not in hba and "scram-sha-256" in hba
    broker = files["rabbitmq.conf"].decode()
    assert "definitions.import_backend = local_filesystem" in broker
    assert "listeners.tcp.1 = 127.0.0.1:5672" in broker
    assert "default_pass" not in broker and "guest" not in broker
    assert "default_limits.vhosts.1.max_connections = 4" in broker
    assert "default_limits.vhosts.1.max_queues = 1" in broker
    assert files["enabled_plugins"] == b"[].\n"
    for name in ("postgres", "rabbitmq"):
        unit = files["kf-" + name + ".service"].decode()
        assert "User=root" not in unit and "MemoryMax=512M" in unit
        assert "MemorySwapMax=0" in unit and "RestartSec=30" in unit
        assert "RequiresMountsFor=" + dependencies.STORAGE[name]["path"] in unit


@pytest.mark.parametrize(
    "field,value",
    [
        ("root", "/shared"),
        ("filesystem", "tmpfs"),
        ("shared_elsewhere", True),
        ("mountpoint", "/var/lib"),
        ("capacity_bytes", 16 * 1024**3),
        ("capacity_bytes", 128 * 1024**2),
        ("capacity_bytes", True),
        ("options", ["rw"]),
    ],
)
def test_dependency_storage_refuses_soft_unbounded_or_shared_capacity(field, value):
    observed = {
        "root": "/",
        "mountpoint": dependencies.STORAGE["postgres"]["path"],
        "filesystem": "ext4",
        "options": ["rw", "nosuid", "nodev", "noexec"],
        "shared_elsewhere": False,
        "capacity_bytes": 2 * 1024**3,
    }
    dependencies.validate_storage("postgres", observed)
    observed[field] = value
    with pytest.raises(policy.DeliveryError, match="hard dependency storage"):
        dependencies.validate_storage("postgres", observed)


@pytest.mark.parametrize("mutation", ["admin", "password", "privilege", "queue", "limit"])
def test_dependency_install_refuses_broker_hash_or_authority_tampering(
    delivery_configuration,
    mutation,
):
    _, _, _, _, endpoints, _ = delivery_configuration
    value = dependencies.broker_definitions(endpoints)
    dependency_install.validate_definitions(value, endpoints)
    if mutation == "admin":
        value["users"][0]["tags"] = ["administrator"]
    elif mutation == "password":
        value["users"][0]["password_hash"] = dependencies.salted_password("foreign-secret")
    elif mutation == "privilege":
        value["permissions"][1]["write"] = ".*"
    elif mutation == "queue":
        value["queues"][0]["arguments"]["x-overflow"] = "drop-head"
    else:
        value["users"][1]["limits"]["max-connections"] = 1000
    with pytest.raises(policy.DeliveryError, match="broker"):
        dependency_install.validate_definitions(value, endpoints)


def test_dependency_receipt_excludes_foreign_files_and_health_claims():
    receipt = {
        "schema_version": 1,
        "source_sha": "123456789abc" + "d" * 28,
        "configuration_id": "cfg-" + "e" * 24,
        "configuration_receipt_sha256": "f" * 64,
        "files": {name: "b" * 64 for name in dependency_install.FILES},
        "required_storage_mounts": dependencies.STORAGE,
        "installed": False,
        "storage_limits_verified": False,
        "services_started": False,
        "execution_verified": False,
    }
    dependency_install.validate_receipt(receipt)
    directory = "/etc/kf-dependencies/sd-123456789abc.cfg-" + "e" * 24
    assert str(dependency_install.batch_directory(receipt)) == directory
    assert (
        str(
            dependency_install.previous_batch(
                directory + "/kf-postgres.service", "kf-postgres.service"
            )
        )
        == directory
    )
    receipt["execution_verified"] = True
    with pytest.raises(policy.DeliveryError, match="binding"):
        dependency_install.validate_receipt(receipt)
    receipt["execution_verified"] = False
    receipt["files"]["server.key"] = "b" * 64
    with pytest.raises(policy.DeliveryError, match="inventory"):
        dependency_install.validate_receipt(receipt)


@pytest.mark.parametrize(
    "target",
    [
        "/etc/kf-onlinejudge/units/sd-123456789abc.cfg-" + "e" * 24 + "/kf-postgres.service",
        "/etc/kf-dependencies/sd-123456789abc.cfg-" + "e" * 24 + "/../kf-postgres.service",
        "/etc//kf-dependencies/sd-123456789abc.cfg-" + "e" * 24 + "/kf-postgres.service",
    ],
)
def test_dependency_pointer_does_not_adopt_unknown_units_or_paths(target):
    with pytest.raises(policy.DeliveryError, match="unknown dependency unit"):
        dependency_install.previous_batch(target, "kf-postgres.service")


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"123\n",
        b"b" * 64,
        b"b" * 64 + b"\nextra\n",
        b"b" * 32 + b" " + b"b" * 31 + b"\n",
    ],
)
def test_database_initializer_refuses_empty_multiline_or_noncanonical_password(contents):
    with pytest.raises(policy.DeliveryError, match="bootstrap password"):
        dependency_initialize.validate_password(contents)


def test_database_initialization_receipt_does_not_prove_migration_or_authentication():
    receipt = {
        "schema_version": 1,
        "source_sha": "b" * 40,
        "configuration_id": "cfg-" + "e" * 24,
        "cluster_identity": {"device": 5, "inode": 900, "major_version": 18, "uid": 1203},
        "bootstrap_sha256": "f" * 64,
        "initialized": True,
        "migrated": False,
        "services_started": False,
        "execution_verified": False,
    }
    dependency_initialize.validate_receipt(receipt)
    receipt["migrated"] = True
    with pytest.raises(policy.DeliveryError, match="binding"):
        dependency_initialize.validate_receipt(receipt)
    receipt["migrated"] = False
    receipt["cluster_identity"]["uid"] = 0
    with pytest.raises(policy.DeliveryError, match="cluster identity"):
        dependency_initialize.validate_receipt(receipt)


@pytest.mark.parametrize(
    "field,value",
    [
        ("superuser", True),
        ("set_role", True),
        ("connection_limit", -1),
        ("login", "postgres"),
        ("inherited", False),
    ],
)
def test_readiness_rejects_wrong_or_excess_database_authority(field, value):
    observed = {
        "login": "kf_coordinator",
        "database": "kf_onlinejudge",
        "canlogin": True,
        "superuser": False,
        "createdb": False,
        "createrole": False,
        "replication": False,
        "bypassrls": False,
        "member": True,
        "inherited": True,
        "set_role": False,
        "connection_limit": 8,
        "privileges": node_health.PRIVILEGES["coordinator"].copy(),
    }
    node_health.validate_role("coordinator", observed)
    observed[field] = value
    with pytest.raises(policy.DeliveryError, match="role authority"):
        node_health.validate_role("coordinator", observed)


def test_readiness_refuses_context_swap_or_claimed_isolation_certificate():
    value = {
        "schema_version": 1,
        "configuration_sha256": "b" * 64,
        "supervisor_authorized": True,
        "execution_gate": "required_per_execution",
        "isolation_verified": False,
        "max_concurrent_runs": 1,
        "active_helpers": 0,
        "cleanup_complete": True,
        "remaining_requests": 4096,
        "remaining_lifetime_seconds": 3600,
    }
    node_health.validate_factory(value, "b" * 64)
    with pytest.raises(policy.DeliveryError, match="factory"):
        node_health.validate_factory(value, "c" * 64)
    value["isolation_verified"] = True
    with pytest.raises(policy.DeliveryError, match="factory"):
        node_health.validate_factory(value, "b" * 64)


def test_busy_unknown_cleanup_or_expiring_factory_never_admits_new_work():
    root = {
        "cleanup_complete": True,
        "active_helpers": 0,
        "remaining_requests": 4096,
        "remaining_lifetime_seconds": 3600,
    }
    counters = {
        "in_flight": {"count_capped": 0, "truncated": False},
        "cleanup_unconfirmed": {"count_capped": 0, "truncated": False},
    }
    admission = {
        "legacy": {"max_job_wall_time_us": 60000000},
        "multi_runtime": {"max_job_wall_time_us": 60000000},
    }
    assert node_health.admission_ready(root, counters, {"consumers": 1}, admission)
    root["remaining_requests"] = 256
    assert not node_health.admission_ready(root, counters, {"consumers": 1}, admission)
    root["remaining_requests"] = 4096
    root["remaining_lifetime_seconds"] = 119
    assert not node_health.admission_ready(root, counters, {"consumers": 1}, admission)
    root["remaining_lifetime_seconds"] = 3600
    root["cleanup_complete"] = False
    assert not node_health.admission_ready(root, counters, {"consumers": 1}, admission)
    root["cleanup_complete"] = True
    counters["cleanup_unconfirmed"]["count_capped"] = 1
    assert not node_health.admission_ready(root, counters, {"consumers": 1}, admission)


def test_actual_broker_argument_parser_keeps_typed_budgets_and_rejects_duplicates():
    value = [
        [key, "longstr" if key == "x-overflow" else "long", val]
        for key, val in dependencies.QUEUE_ARGUMENTS.items()
    ]
    assert broker_health.queue_arguments(value) == dependencies.QUEUE_ARGUMENTS
    value[-1] = value[0].copy()
    with pytest.raises(policy.DeliveryError, match="duplicate"):
        broker_health.queue_arguments(value)
    with pytest.raises(policy.DeliveryError, match="budgets"):
        broker_health.queue_arguments({**dependencies.QUEUE_ARGUMENTS, "x-max-length": 1024})


def test_counter_projection_rejects_truncated_empty_and_source_admission_mismatch():
    value = {
        "schema_version": 1,
        "admission": {"fixture": True},
        "configured_workers": 1,
        "lease_seconds": 120,
        "counters": {
            "queued": {"count_capped": 0, "truncated": False},
            "in_flight": {"count_capped": 0, "truncated": False},
            "cleanup_unconfirmed": {"count_capped": 0, "truncated": False},
            "database_clock_unix_ms": 1791830000000,
        },
    }
    node_health.validate_execution(value, value["admission"])
    with pytest.raises(policy.DeliveryError, match="admission"):
        node_health.validate_execution(value, {"fixture": False})
    value["counters"]["cleanup_unconfirmed"]["truncated"] = True
    with pytest.raises(policy.DeliveryError, match="bound"):
        node_health.validate_execution(value, value["admission"])


def empty_drain_projection():
    return {
        "execution": {
            "database_clock_unix_ms": 1791830000000,
            **{
                name: {"count_capped": 0, "truncated": False}
                for name in ("queued", "in_flight", "cleanup_unconfirmed")
            },
        },
        "outbox": {
            "schema_version": 1,
            "pending_capped": 0,
            "locked_capped": 0,
            "truncated": False,
        },
        "imports": {
            "schema_version": 1,
            "worker_ready": True,
            "reserved_bytes": 0,
            "uploaded_capped": 0,
            "validating_capped": 0,
            "truncated": False,
        },
        "broker": {
            "schema_version": 1,
            "metadata_verified": True,
            "messages_ready": 0,
            "messages_unacknowledged": 0,
            "message_bytes": 0,
            "consumers": 1,
        },
        "factory": {
            "schema_version": 1,
            "configuration_sha256": "b" * 64,
            "supervisor_authorized": True,
            "execution_gate": "required_per_execution",
            "isolation_verified": False,
            "max_concurrent_runs": 1,
            "active_helpers": 0,
            "cleanup_complete": True,
            "remaining_requests": 4096,
            "remaining_lifetime_seconds": 3600,
        },
    }


@pytest.mark.parametrize(
    "component,key,value",
    [
        ("outbox", "pending_capped", 1),
        ("imports", "uploaded_capped", 1),
        ("imports", "validating_capped", 1),
        ("imports", "truncated", True),
        ("broker", "messages_unacknowledged", 1),
        ("factory", "cleanup_complete", False),
        ("factory", "active_helpers", 1),
    ],
)
def test_drain_cannot_release_with_pending_owner_work_or_unconfirmed_cleanup(component, key, value):
    snapshot = empty_drain_projection()
    assert lifecycle.projection_empty(snapshot)
    snapshot[component][key] = value
    if component == "factory" and key == "active_helpers":
        snapshot["factory"]["cleanup_complete"] = False
    assert not lifecycle.projection_empty(snapshot)


@pytest.mark.parametrize("counter", ["queued", "in_flight", "cleanup_unconfirmed"])
def test_drain_rejects_unknown_or_truncated_execution_counters(counter):
    snapshot = empty_drain_projection()
    snapshot["execution"][counter]["count_capped"] = False
    with pytest.raises(policy.DeliveryError):
        lifecycle.projection_empty(snapshot)
    snapshot["execution"][counter] = {"count_capped": 0, "truncated": True}
    assert not lifecycle.projection_empty(snapshot)


def test_maintenance_receipt_cannot_silently_adopt_unknown_history_or_release_proof():
    value = {
        "schema_version": 1,
        "source_sha": "b" * 40,
        "configuration_id": "cfg-" + "c" * 24,
        "action": "drain",
        "phase": "quiescent",
        "success": False,
        "events": [],
        "root_cleanup_confirmed": True,
        "boot_id": "11111111-1111-4111-8111-111111111111",
        "drain_projection": empty_drain_projection(),
    }
    lifecycle.validate_record(value)
    with pytest.raises(policy.DeliveryError):
        lifecycle.validate_record({**value, "clear_lease": True})
    value["drain_projection"]["factory"]["cleanup_complete"] = False
    with pytest.raises(policy.DeliveryError):
        lifecycle.validate_record(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("shared", True),
        ("capacity_bytes", True),
        ("capacity_bytes", 128 * 1024**2),
        ("options", ["ro", "nosuid", "nodev", "noexec"]),
        ("root", "/another-directory"),
        ("filesystem", "tmpfs"),
    ],
)
def test_journal_profile_never_substitutes_rotation_settings_for_a_dedicated_hard_volume(
    field, value
):
    path = Path("/var/log/journal/" + "b" * 32 + ".kf-onlinejudge")
    observed = {
        "path": str(path),
        "root": "/",
        "filesystem": "ext4",
        "options": ["rw", "nosuid", "nodev", "noexec"],
        "shared": False,
        "capacity_bytes": 64 * 1024**2,
    }
    journal.validate_storage(observed, path)
    observed[field] = value
    with pytest.raises(policy.DeliveryError):
        journal.validate_storage(observed, path)
