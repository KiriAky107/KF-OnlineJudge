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
