"""Pure delivery-contract tests; these do not claim installation or VM safety."""

import hashlib
import importlib
import json
import os
import re
import secrets
import stat
import struct
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

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
credentials = importlib.import_module("scripts.release.credentials")
node_health = importlib.import_module("scripts.release.node_health")
broker_health = importlib.import_module("scripts.release.broker_health")
lifecycle = importlib.import_module("scripts.release.lifecycle")
journal = importlib.import_module("scripts.release.journal")
proxy_runtime = importlib.import_module("scripts.release.proxy_runtime")
proxy_service = importlib.import_module("scripts.release.proxy_service")
proxy_guard = importlib.import_module("scripts.release.proxy_guard")
proxy_install = importlib.import_module("scripts.release.proxy_install")
backup = importlib.import_module("scripts.release.backup")
restore = importlib.import_module("scripts.release.restore")
restore_policy = importlib.import_module("scripts.release.restore_policy")
recovery_identity = importlib.import_module("scripts.release.recovery_identity")
recovery_database = importlib.import_module("scripts.release.recovery_database")
node_observation = importlib.import_module("scripts.release.node_observation")
installer = importlib.import_module("scripts.release.install")


@pytest.mark.parametrize("field", ["release_id", "configuration_id", "manifest_sha256"])
def test_activation_refuses_checked_binding_that_differs_from_explicit_request(monkeypatch, field):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    release, batch, expected = "sd-123456789abc", "cfg-" + "1" * 24, "1" * 64
    binding = {"release_id": release, "configuration_id": batch, "manifest_sha256": expected}
    binding[field] = "different"
    monkeypatch.setattr(configuration, "check_installed", lambda _: binding)
    with pytest.raises(policy.DeliveryError, match="configuration not bound"):
        installer.activate(release, expected, batch)


@pytest.mark.parametrize("mode,owner", [(stat.S_IFREG, 0), (stat.S_IFSOCK, 1000)])
def test_system_bus_preflight_refuses_unprotected_endpoint(monkeypatch, mode, owner):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_mode=mode, st_uid=owner))
    with pytest.raises(policy.DeliveryError, match="socket absent/unprotected"):
        installer.preflight.system_bus()


@pytest.mark.parametrize(
    "response", [(0, 's "262"\n'), (0, 's "262-1-arch"\n'), (1, ""), (0, 's "other"\n')]
)
def test_system_bus_preflight_requires_bounded_manager_response(monkeypatch, response):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=0))

    def run(args, **options):
        assert options["timeout"] == 5 and "--timeout=3" in args
        assert options["env"] == {"PATH": "/usr/bin", "LC_ALL": "C"}
        return SimpleNamespace(returncode=response[0], stdout=response[1], stderr="private detail")

    monkeypatch.setattr(installer.preflight.subprocess, "run", run)
    if response[0] == 0 and response[1].startswith('s "262'):
        assert installer.preflight.system_bus()["manager_reachable"]
    else:
        with pytest.raises(policy.DeliveryError, match="manager unavailable/incompatible") as error:
            installer.preflight.system_bus()
        assert "private detail" not in str(error.value)


def test_system_bus_timeout_is_an_explicit_preflight_refusal(monkeypatch):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=0))

    def run(args, **options):
        raise subprocess.TimeoutExpired(args, options["timeout"])

    monkeypatch.setattr(installer.preflight.subprocess, "run", run)
    with pytest.raises(policy.DeliveryError, match="system message bus unavailable"):
        installer.preflight.system_bus()


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o555])
def test_new_layout_directory_has_explicit_mode_under_private_umask(tmp_path, monkeypatch, mode):
    # Ordinary owned temporary files verify the umask regression. Root ownership
    # and protected ancestor checks still require independent VM acceptance.
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    path = tmp_path / "created"
    previous = os.umask(0o077)
    try:
        installer.directory(path, mode)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_layout_directory_does_not_repair_existing_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    path = tmp_path / "existing"
    path.mkdir(mode=0o700)
    before = path.stat()
    with pytest.raises(policy.DeliveryError, match="existing directory mode differs"):
        installer.directory(path, 0o755)
    assert path.stat().st_ino == before.st_ino
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o444, 0o600, 0o644])
def test_new_exclusive_file_has_requested_mode_under_private_umask(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    path = tmp_path / "created"
    previous = os.umask(0o077)
    try:
        installer.exclusive_file(path, b"ordinary fixture", mode)
    finally:
        os.umask(previous)
    assert path.read_bytes() == b"ordinary fixture"
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_exclusive_file_does_not_repair_existing_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    path = tmp_path / "existing"
    path.write_bytes(b"ordinary fixture")
    path.chmod(0o600)
    inode = path.stat().st_ino
    with pytest.raises(policy.DeliveryError, match="refused overwrite"):
        installer.exclusive_file(path, b"ordinary fixture", 0o644)
    assert path.stat().st_ino == inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "changed", ["release_id", "configuration_id", "manifest_sha256", "selection"]
)
def test_current_receipt_rejects_changed_binding_after_full_validation(monkeypatch, changed):
    selected = {
        "release_id": "sd-aaaaaaaaaaaa",
        "configuration_id": "cfg-" + "a" * 24,
        "manifest_sha256": "a" * 64,
        "receipt_sha256": "b" * 64,
    }
    installed = dict(selected)
    after = dict(selected)
    if changed == "selection":
        after["receipt_sha256"] = "c" * 64
    else:
        installed[changed] = "different verified binding"
    observations = iter([selected, after])
    monkeypatch.setattr(service_install.install, "selection", lambda **options: next(observations))
    monkeypatch.setattr(
        service_install.configuration, "installed_receipt_path", lambda *args: Path("/fixture")
    )
    monkeypatch.setattr(service_install.configuration, "check_installed", lambda path: installed)
    with pytest.raises(policy.DeliveryError, match="selected configuration changed"):
        service_install.current_receipt()


def test_new_copy_hashes_copied_bytes_and_reads_destination_before_acceptance(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"ordinary release fixture\n" * 65536)
    expected = policy.digest(source)
    reads = []
    original = installer.digest

    def readback(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(installer, "digest", readback)
    installer.copy_verified(source, target, expected, 0o444)
    assert reads == [target]
    assert target.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    inode = target.stat().st_ino
    installer.copy_verified(source, target, expected, 0o444)
    assert target.stat().st_ino == inode
    assert reads == [target, source, target]


def test_changed_copy_retains_incomplete_file_and_never_overwrites_it(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"original")
    expected = policy.digest(source)
    source.write_bytes(b"changed")
    with pytest.raises(policy.DeliveryError, match="source changed"):
        installer.copy_verified(source, target, expected, 0o444)
    inode = target.stat().st_ino
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    source.write_bytes(b"original")
    with pytest.raises(policy.DeliveryError, match="refused overwrite"):
        installer.copy_verified(source, target, expected, 0o444)
    assert target.stat().st_ino == inode
    assert target.read_bytes() == b"changed"


def test_frozen_copy_reuses_verified_records_and_checks_full_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "backend").mkdir()
    payload = source / "backend/ordinary.txt"
    payload.write_bytes(b"frozen input")
    payload.chmod(0o444)
    (source / "backend").chmod(0o555)
    records = manifest.inventory(source)
    scanned = []
    original = manifest.inventory

    def inventory(path, **kwargs):
        scanned.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(manifest, "inventory", inventory)
    installer.frozen_copy(source, target, verified_records=records)
    assert scanned == [target]
    assert original(target) == records
    installer.frozen_copy(source, target, verified_records=records)
    assert scanned == [target, target]


def test_frozen_copy_rejects_destination_io_mismatch_before_acceptance(tmp_path, monkeypatch):
    # Simulated I/O mismatch in an ordinary owned fixture; Root protection and
    # actual disk/error isolation are still separate VM acceptance.
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "backend").mkdir()
    payload = source / "backend/ordinary.txt"
    payload.write_bytes(b"frozen input")
    payload.chmod(0o444)
    (source / "backend").chmod(0o555)
    records = manifest.inventory(source)
    original = installer._copy_verified

    def simulate_io_mismatch(source, target, expected, mode, **options):
        assert options["readback"] is False
        original(source, target, expected, mode, **options)
        target.chmod(0o600)
        target.write_bytes(b"different bytes")
        target.chmod(mode)

    monkeypatch.setattr(installer, "_copy_verified", simulate_io_mismatch)
    with pytest.raises(policy.DeliveryError, match="installed image readback differs"):
        installer.frozen_copy(source, target, verified_records=records)


def test_frozen_copy_rejects_changed_source_against_trusted_records(tmp_path, monkeypatch):
    # Ordinary fixture bytes exercise the deferred batch validation. Actual
    # Root protection and installation are separately verified in the VM.
    monkeypatch.setattr(installer.preflight, "protected", lambda *args, **kwargs: None)
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    (source / "backend").mkdir()
    payload = source / "backend/ordinary.txt"
    payload.write_bytes(b"frozen input")
    payload.chmod(0o444)
    (source / "backend").chmod(0o555)
    records = manifest.inventory(source)
    payload.chmod(0o600)
    payload.write_bytes(b"changed bytes")
    payload.chmod(0o444)
    with pytest.raises(policy.DeliveryError, match="installed image readback differs"):
        installer.frozen_copy(source, target, verified_records=records)
    assert (target / "backend/ordinary.txt").read_bytes() == b"changed bytes"
    assert stat.S_IMODE(target.stat().st_mode) == 0o555
    with pytest.raises(policy.DeliveryError, match="existing protected image differs"):
        installer.frozen_copy(source, target, verified_records=records)


def test_kernel_observation_keeps_cumulative_counters_and_rejects_duplicate_rows():
    raw = (
        "usage_usec 17\nuser_usec 11\nsystem_usec 6\nnr_periods 9\nnr_throttled 2\nthrottled_usec 3"
    )
    observed = node_observation.counters(raw, node_observation.CPU_FIELDS)
    assert observed["usage_usec"] == 17 and observed["nr_throttled"] == 2
    with pytest.raises(policy.DeliveryError):
        node_observation.counters(raw + "\nusage_usec 18", node_observation.CPU_FIELDS)
    with pytest.raises(policy.DeliveryError):
        node_observation.counters("usage_usec 17", node_observation.CPU_FIELDS)


def test_kernel_io_observation_aggregates_devices_without_returning_device_identities():
    actual = node_observation.io_totals(
        "8:0 rbytes=5 wbytes=7 rios=1 wios=2 dbytes=0 dios=0\n"
        "8:1 rbytes=11 wbytes=13 rios=3 wios=4 dbytes=0 dios=0"
    )
    assert actual == {"devices_observed": 2, "rbytes": 16, "wbytes": 20, "rios": 4, "wios": 6}
    assert node_observation.io_totals("")["devices_observed"] == 0
    with pytest.raises(policy.DeliveryError):
        node_observation.io_totals(
            "8:0 rbytes=5 wbytes=7 rios=1 wios=2\n8:0 rbytes=0 wbytes=0 rios=0 wios=0"
        )


@pytest.mark.parametrize("value", ["-1", "max", "True", str(2**63)])
def test_kernel_observation_does_not_replace_unavailable_or_invalid_counters_with_zero(value):
    with pytest.raises(policy.DeliveryError):
        node_observation.number(value)


def test_postgres_recovery_retains_narrow_existing_role_authorities():
    assert recovery_identity.properties("kf_platform") == (
        True,
        False,
        False,
        False,
        False,
        False,
        8,
        True,
        None,
        None,
    )
    assert recovery_identity.memberships("kf_migrator") == [
        ("kf_oj_identity_authority_owner", False, True, True)
    ]
    assert recovery_identity.memberships("kf_coordinator") == [
        ("kf_oj_coordinator", False, True, False)
    ]
    assert recovery_identity.memberships("postgres") == []


def postgres_recovery_settings_shape():
    values = recovery_identity.recovery_settings()
    rows = [{"name": key, "setting": value, "unit": None} for key, value in values.items()]
    normalized = {
        "shared_buffers": ("8192", "8kB"),
        "work_mem": ("1024", "kB"),
        "maintenance_work_mem": ("32768", "kB"),
        "statement_timeout": ("5000", "ms"),
        "lock_timeout": ("1000", "ms"),
        "idle_in_transaction_session_timeout": ("5000", "ms"),
        "authentication_timeout": ("5", "s"),
        "temp_file_limit": ("16384", "kB"),
        "log_parameter_max_length": ("0", "B"),
        "log_parameter_max_length_on_error": ("0", "B"),
        "log_min_duration_statement": ("-1", "ms"),
        "log_min_duration_sample": ("-1", "ms"),
    }
    for row in rows:
        if row["name"] in normalized:
            row["setting"], row["unit"] = normalized[row["name"]]
    return rows


def test_postgres_recovery_readback_compares_effective_units_instead_of_cli_strings():
    rows = postgres_recovery_settings_shape()
    recovered = recovery_identity.check_settings(rows)
    assert recovered["listen_addresses"]["setting"] == ""
    assert recovered["unix_socket_permissions"]["setting"] == "0700"
    for row in rows:
        if row["name"] == "shared_buffers":
            row["setting"] = "16384"
    with pytest.raises(policy.DeliveryError):
        recovery_identity.check_settings(rows)


def recovery_receipts_shape():
    # Pure closed-contract example; no PG connection, Root or Linux acceptance.
    prepared = {
        "schema_version": 1,
        "recovery_id": "rr-" + "1" * 32,
        "backup_id": "bk-" + "2" * 32,
        "private_backup_sha256": "b" * 64,
        "source_sha": "c" * 40,
        "configuration_id": "cfg-" + "d" * 24,
        "boot_id": "e" * 8 + "-" + "e" * 4 + "-" + "e" * 4 + "-" + "e" * 4 + "-" + "e" * 12,
        "data_restored": False,
        "database_credentials_rotated": False,
        "services_started": False,
        "execution_verified": False,
    }
    data = {
        **prepared,
        "data_restored": True,
        "restored_payload_bytes": 1024,
        "restored_files": 10,
        "restored_directories": 12,
        "historical_configuration_active": False,
        "factory_replay_history_retained": True,
    }
    identity = {
        **{
            key: prepared[key]
            for key in (
                "schema_version",
                "recovery_id",
                "source_sha",
                "configuration_id",
                "boot_id",
            )
        },
        "data_receipt_sha256": "f" * 64,
        "bootstrap_sha256": "b" * 64,
        "cluster_identity": {"device": 1, "inode": 10, "major_version": 18, "uid": 1203},
        "temporary_server_stopped": True,
        "database_credentials_rotated": True,
        "services_started": False,
        "schema_migrated": False,
        "execution_state_changed": False,
        "execution_verified": False,
        "ordinary_identity_readback": {
            "dependency": "postgres",
            "actual_cgroup_limits": {
                "memory.max": "536870912",
                "memory.swap.max": "0",
                "pids.max": "64",
                "cpu.max": "100000 100000",
            },
            "data_filesystem_bytes": 2 * 1024**3,
            "execution_verified": False,
            "rotated_roles": sorted(["postgres", *configuration.PROFILES.values()]),
            "role_authority_sha256": "c" * 64,
            "authenticated": True,
            "schema_migrated": False,
            "execution_state_changed": False,
            "effective_settings": recovery_identity.check_settings(
                postgres_recovery_settings_shape()
            ),
            "temporary_server_stopped": True,
            "services_started": False,
        },
    }
    return prepared, data, identity


@pytest.mark.parametrize("failure", ["unrotated", "running", "wrong_data", "wrong_config", "extra"])
def test_recovery_completion_refuses_partial_or_unbound_steps(failure):
    prepared, data, identity = recovery_receipts_shape()
    recovery_database.validate_complete(prepared, data, identity, prepared["recovery_id"], "f" * 64)
    if failure == "unrotated":
        identity["database_credentials_rotated"] = False
    elif failure == "running":
        identity["temporary_server_stopped"] = False
    elif failure == "wrong_data":
        identity["data_receipt_sha256"] = "b" * 64
    elif failure == "wrong_config":
        identity["configuration_id"] = "cfg-" + "f" * 24
    else:
        identity["skip_verification"] = True
    with pytest.raises(policy.DeliveryError):
        recovery_database.validate_complete(
            prepared, data, identity, prepared["recovery_id"], "f" * 64
        )


def test_startup_cannot_launch_services_when_recovery_admission_is_refused(monkeypatch):
    maintenance = object.__new__(lifecycle.Maintenance)
    calls = []
    monkeypatch.setattr(maintenance, "start_locked", lambda **_: calls.append("start"))

    def refuse():
        raise policy.DeliveryError("partial recovery")

    monkeypatch.setattr(recovery_database, "admission_guard", refuse)
    with pytest.raises(policy.DeliveryError):
        maintenance.start(resume=True)
    assert calls == []


def test_restoration_keeps_old_runtime_configuration_and_operations_inactive():
    directory = Path("/var/lib/kf-onlinejudge/recovery/rr-" + "1" * 32)
    for domain in restore.HISTORICAL:
        item = {"path": domain + "/old.json", "uid": 0, "gid": 0, "mode": 0o644, "bytes": 20}
        path, uid, gid, mode = restore.target(item, directory)
        assert path == directory / "history" / domain / "old.json"
        assert (uid, gid, mode) == (0, 0, 0o600)
    path, uid, gid, mode = restore.target(
        {"path": "artifacts/object", "uid": 1200, "gid": 1200, "mode": 0o600, "bytes": 20},
        directory,
    )
    assert path == Path("/var/lib/kf-onlinejudge/artifacts/object")
    assert (uid, gid, mode) == (1200, 1200, 0o600)


def test_recovery_requires_new_worker_tokens_epoch_and_every_operational_password():
    from copy import deepcopy

    def context(offset):
        return {
            "operator_attested": True,
            "worker_id": str(UUID(int=offset + 1)),
            "measurement_nonce": format(offset + 10, "064x"),
            "measurement_epoch": format(offset + 20, "064x"),
            "worker_token": format(offset + 30, "064x"),
            "platform_token": format(offset + 40, "064x"),
            "endpoints": {
                "database": {
                    "api": "postgresql://kf_platform:"
                    + str(offset) * 32
                    + "@127.0.0.1:5432/kf_onlinejudge"
                },
                "amqp": {
                    "worker": "amqp://kf_worker:" + str(offset + 1) * 32 + "@127.0.0.1:5672/kf"
                },
            },
        }

    previous, current = context(1), context(5)
    restore_policy.fresh_identities(previous, current)
    for key in (
        "worker_id",
        "measurement_nonce",
        "measurement_epoch",
        "worker_token",
        "platform_token",
    ):
        with pytest.raises(policy.DeliveryError):
            restore_policy.fresh_identities(previous, {**current, key: previous[key]})
    changed = deepcopy(current)
    changed["endpoints"]["database"]["api"] = previous["endpoints"]["amqp"]["worker"]
    with pytest.raises(policy.DeliveryError):
        restore_policy.fresh_identities(previous, changed)


def test_backup_retention_preserves_a_newer_unexpired_recovery_copy():
    expired = {
        "backup_id": "bk-" + "1" * 32,
        "created_unix_seconds": 100,
        "expires_unix_seconds": 100 + 7 * 86400,
    }
    retained = {
        "backup_id": "bk-" + "2" * 32,
        "created_unix_seconds": 200,
        "expires_unix_seconds": 200 + 7 * 86400,
    }
    now = expired["expires_unix_seconds"]
    backup.retention_eligible(expired, retained, now)
    for unavailable in (
        expired,
        {**retained, "expires_unix_seconds": now},
        {**retained, "created_unix_seconds": now + 1},
    ):
        with pytest.raises(policy.DeliveryError):
            backup.retention_eligible(expired, unavailable, now)
    with pytest.raises(policy.DeliveryError):
        backup.retention_eligible(expired, retained, now - 1)


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


def credential_acl(uid=1203, *, owner=4, user=4, group=0, mask=4, other=0):
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", *row)
        for row in [
            (1, owner, 0xFFFFFFFF),
            (2, user, uid),
            (4, group, 0xFFFFFFFF),
            (16, mask, 0xFFFFFFFF),
            (32, other, 0xFFFFFFFF),
        ]
    )


def test_proxy_startup_refuses_another_protected_selected_binding(monkeypatch):
    monkeypatch.setattr(proxy_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        node_health,
        "binding",
        lambda: (
            {"release_id": "sd-123456789abc", "configuration_id": "cfg-" + "e" * 24},
            {},
            ROOT,
        ),
    )
    monkeypatch.setattr(installer, "selection", lambda **kwargs: pytest.fail("full image scan"))
    with pytest.raises(policy.DeliveryError, match="another selected configuration"):
        proxy_install.check_binding("sd-123456789abc", "cfg-" + "f" * 24)


@pytest.mark.parametrize("changed", ["nginx.conf", "fullchain.pem", "key.pem", "selection"])
def test_proxy_startup_still_refuses_own_bytes_or_selection_drift(monkeypatch, changed):
    selected = {"release_id": "sd-123456789abc", "configuration_id": "cfg-" + "e" * 24}
    fingerprints = {
        name: hashlib.sha256(("synthetic protected " + name).encode()).hexdigest()
        for name in ("nginx.conf", "fullchain.pem", "key.pem")
    }
    changed_fingerprint = hashlib.sha256(b"changed protected proxy bytes").hexdigest()
    spec = {
        "schema_version": 1,
        **selected,
        "origin": "https://oj.test",
        "configuration_sha256": fingerprints["nginx.conf"],
        "certificate_sha256": fingerprints["fullchain.pem"],
        "key_sha256": fingerprints["key.pem"],
    }
    monkeypatch.setattr(proxy_install.os, "geteuid", lambda: 0)
    monkeypatch.setattr(node_health, "binding", lambda: (selected, {}, ROOT))
    monkeypatch.setattr(proxy_service, "batch", lambda _: Path("/protected/proxy"))
    monkeypatch.setattr(proxy_install, "read_json", lambda *args: spec)
    monkeypatch.setattr(proxy_install.preflight, "protected", lambda *args, **kwargs: None)
    real_stat = Path.stat
    observed = {
        Path("/protected/proxy/launch.json"),
        Path("/protected/proxy/nginx.conf"),
        proxy_service.ROOT / "tls/fullchain.pem",
        proxy_service.ROOT / "tls/key.pem",
    }

    def file_info(path, *args, **kwargs):
        if path not in observed:
            return real_stat(path, *args, **kwargs)
        return SimpleNamespace(
            st_gid=1205 if path.name == "key.pem" else 0,
            st_mode=stat.S_IFREG | (0o640 if path.name == "key.pem" else 0o644),
            st_size=128,
        )

    monkeypatch.setattr(Path, "stat", file_info)
    monkeypatch.setattr(
        proxy_install,
        "digest",
        lambda p: changed_fingerprint if p.name == changed else fingerprints[p.name],
    )
    monkeypatch.setattr(proxy_install.journal, "check_profile", lambda **kwargs: None)
    monkeypatch.setattr(proxy_install, "storage", lambda **kwargs: None)
    monkeypatch.setattr(proxy_install, "account", lambda **kwargs: None)

    def selection(**kwargs):
        assert kwargs == {"check_configuration": False}
        return {**selected, "configuration_id": "cfg-" + "f" * 24}

    monkeypatch.setattr(installer, "selection", selection)
    with pytest.raises(policy.DeliveryError, match="proxy.*changed"):
        proxy_install.check_binding(selected["release_id"], selected["configuration_id"])


def test_external_postgres_configuration_binds_ident_file_to_initialized_data():
    files = dependencies.render("sd-123456789abc", "cfg-" + "e" * 24)
    text = files["postgresql.conf"].decode()
    paths = dict(re.findall(r"^(data_directory|ident_file|hba_file) = '([^']+)'$", text, re.M))
    assert set(paths) == {"data_directory", "ident_file", "hba_file"}
    assert Path(paths["ident_file"]) == Path(paths["data_directory"]) / "pg_ident.conf"
    assert Path(paths["hba_file"]).parent == dependencies.CONFIGURATION_ROOT / (
        "sd-123456789abc.cfg-" + "e" * 24
    )


@pytest.mark.parametrize("uid", [1201, 1202, 1203])
def test_credential_acl_has_exactly_one_service_reader(uid):
    assert credentials.validate_acl(credential_acl(uid), uid) is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        credential_acl()[:-1],
        b"\x01" + credential_acl()[1:],
        credential_acl(1202),
        credential_acl(owner=6),
        credential_acl(user=6),
        credential_acl(group=4),
        credential_acl(mask=7),
        credential_acl(other=4),
        credential_acl() + struct.pack("<HHI", 2, 4, 9999),
    ],
)
def test_credential_acl_refuses_extra_readers_writers_or_noncanonical_acl(raw):
    with pytest.raises(policy.DeliveryError, match="credential ACL differs"):
        credentials.validate_acl(raw, 1203)


def credential_metadata(monkeypatch, **overrides):
    values = dict(st_mode=stat.S_IFREG | 0o440, st_nlink=1, st_uid=0, st_gid=0, st_size=65)
    values.update(overrides)
    monkeypatch.setattr(Path, "lstat", lambda _: SimpleNamespace(**values))
    monkeypatch.setattr(credentials.os, "statvfs", lambda _: SimpleNamespace(f_flag=os.ST_RDONLY))
    monkeypatch.setattr(credentials.os, "getxattr", lambda *args: credential_acl())
    monkeypatch.setattr(credentials.preflight, "protected", lambda *args, **kwargs: None)


def test_acl_credential_is_limited_to_fixed_readonly_systemd_binding(monkeypatch):
    credential_metadata(monkeypatch)
    path = Path("/run/credentials/kf-postgres-initialize.service/bootstrap")
    assert credentials.validate_metadata(path, 1203, 65).st_size == 65
    monkeypatch.setattr(credentials.os, "statvfs", lambda _: SimpleNamespace(f_flag=0))
    with pytest.raises(policy.DeliveryError, match="bounded systemd credential"):
        credentials.validate_metadata(path, 1203, 65)


@pytest.mark.parametrize(
    "path,uid",
    [
        ("/tmp/kf-postgres-initialize.service/bootstrap", 1203),
        ("/run/credentials/foreign.service/bootstrap", 1203),
        ("/run/credentials/kf-postgres-initialize.service/foreign", 1203),
        ("/run/credentials/kf-postgres-initialize.service/bootstrap", 1202),
    ],
)
def test_acl_credential_refuses_wrong_directory_member_or_role(monkeypatch, path, uid):
    credential_metadata(monkeypatch)
    with pytest.raises(policy.DeliveryError, match="bounded systemd credential"):
        credentials.validate_metadata(Path(path), uid, 65)


@pytest.mark.parametrize(
    "overrides",
    [
        {"st_uid": 1203},
        {"st_gid": 1203},
        {"st_nlink": 2},
        {"st_size": 66},
        {"st_mode": stat.S_IFREG | 0o444},
        {"st_mode": stat.S_IFLNK | 0o440},
    ],
)
def test_acl_credential_refuses_unknown_file_identity_or_bounds(monkeypatch, overrides):
    credential_metadata(monkeypatch, **overrides)
    with pytest.raises(policy.DeliveryError, match="bounded systemd credential"):
        credentials.validate_metadata(
            Path("/run/credentials/kf-postgres-initialize.service/bootstrap"), 1203, 65
        )


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


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_initializer_cleanup_accepts_only_owned_stopped_empty_unit(monkeypatch, tmp_path, state):
    group = tmp_path / "cgroup"
    group.mkdir()
    (group / "cgroup.procs").write_text("")
    (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    monkeypatch.setattr(dependency_initialize, "CGROUP", group)
    actual = {
        "LoadState": "loaded",
        "Description": "owned",
        "Transient": "yes",
        "ControlGroup": "/kfonlinejudge.slice/" + dependency_initialize.UNIT,
        "ActiveState": state,
        "MainPID": "0",
    }
    monkeypatch.setattr(dependency_initialize, "status", lambda: actual)
    stopped = []
    monkeypatch.setattr(service_install, "systemctl", lambda *args: stopped.append(args))
    assert dependency_initialize.stop_owned_unit("owned") is None
    assert stopped == [("stop", dependency_initialize.UNIT)]


@pytest.mark.parametrize(
    "field,value", [("Description", "foreign"), ("Transient", "no"), ("ControlGroup", "/foreign")]
)
def test_initializer_cleanup_never_stops_foreign_unit(monkeypatch, field, value):
    actual = {
        "LoadState": "loaded",
        "Description": "owned",
        "Transient": "yes",
        "ControlGroup": "/kfonlinejudge.slice/" + dependency_initialize.UNIT,
    }
    actual[field] = value
    monkeypatch.setattr(dependency_initialize, "status", lambda: actual)
    monkeypatch.setattr(service_install, "systemctl", lambda *args: pytest.fail("foreign stop"))
    with pytest.raises(policy.DeliveryError, match="foreign"):
        dependency_initialize.stop_owned_unit("owned")


@pytest.mark.parametrize("changed", ["Description", "ControlGroup"])
def test_initializer_cleanup_refuses_unit_replacement_after_stop(monkeypatch, changed):
    actual = {
        "LoadState": "loaded",
        "Description": "owned",
        "Transient": "yes",
        "ControlGroup": "/kfonlinejudge.slice/" + dependency_initialize.UNIT,
        "ActiveState": "failed",
        "MainPID": "0",
    }
    later = {**actual, changed: "foreign"}
    states = iter([actual, later])
    monkeypatch.setattr(dependency_initialize, "status", lambda: next(states))
    monkeypatch.setattr(service_install, "systemctl", lambda *args: None)
    with pytest.raises(policy.DeliveryError, match="ownership changed"):
        dependency_initialize.stop_owned_unit("owned")


@pytest.mark.parametrize(
    "state,pid,procs,populated",
    [
        ("active", "0", "", 0),
        ("failed", "45", "", 0),
        ("failed", "0", "45\n", 0),
        ("failed", "0", "", 1),
    ],
)
def test_initializer_cleanup_refuses_live_or_residual_processes(
    monkeypatch, tmp_path, state, pid, procs, populated
):
    group = tmp_path / "cgroup"
    group.mkdir()
    (group / "cgroup.procs").write_text(procs)
    (group / "cgroup.events").write_text(f"populated {populated}\nfrozen 0\n")
    monkeypatch.setattr(dependency_initialize, "CGROUP", group)
    # Even a disappeared transient unit must not hide residual descendants.
    monkeypatch.setattr(
        dependency_initialize,
        "status",
        lambda: {
            "LoadState": "not-found",
            "ActiveState": state,
            "MainPID": pid,
        },
    )
    with pytest.raises(policy.DeliveryError, match="cleanup unconfirmed"):
        dependency_initialize.stop_owned_unit("owned")


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


def test_confirmed_drain_can_continue_to_stop_without_querying_stopped_owners(monkeypatch):
    from types import SimpleNamespace

    selected = {"release_id": "kf-" + "b" * 12, "configuration_id": "cfg-" + "c" * 24}
    previous = {
        "schema_version": 1,
        "source_sha": "b" * 40,
        "configuration_id": selected["configuration_id"],
        "action": "drain",
        "phase": "complete",
        "success": True,
        "events": [],
        "root_cleanup_confirmed": True,
        "boot_id": "11111111-1111-4111-8111-111111111111",
        "drain_projection": empty_drain_projection(),
    }
    maintenance = object.__new__(lifecycle.Maintenance)
    maintenance.selected = selected
    maintenance.record = {**previous, "action": "stop", "root_cleanup_confirmed": False}
    gate = SimpleNamespace(**selected, state="quiescent")
    monkeypatch.setattr(lifecycle.availability, "read", lambda: gate)
    observed = []
    monkeypatch.setattr(
        maintenance, "unit", lambda name: {"ActiveState": "inactive", "MainPID": "0"}
    )
    monkeypatch.setattr(maintenance, "empty_cgroup", lambda name: observed.append(name))
    monkeypatch.setattr(maintenance, "root_stopped", lambda: observed.append("root-proof"))
    assert maintenance.reuse_drained(previous) is True
    assert observed == [*lifecycle.APPS, "root-proof"]
    assert maintenance.record["drain_projection"] == previous["drain_projection"]
    observed.clear()
    with pytest.raises(policy.DeliveryError):
        maintenance.reuse_drained({**previous, "boot_id": "22222222-2222-4222-8222-222222222222"})
    assert observed == []
    gate.state = "draining"
    assert maintenance.reuse_drained(previous) is False


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


def test_read_only_observer_mount_is_not_daemon_writability_proof():
    path = Path("/var/log/journal/" + "b" * 32 + ".kf-onlinejudge")
    observed = {
        "path": str(path),
        "root": "/",
        "filesystem": "ext4",
        "options": ["ro", "nosuid", "nodev", "noexec"],
        "shared": False,
        "capacity_bytes": 64 * 1024**2,
    }
    journal.validate_storage(observed, path, allow_read_only=True)
    with pytest.raises(policy.DeliveryError):
        journal.validate_storage(observed, path)


@pytest.mark.parametrize("failure", ["publication", "receipt", "readback"])
def test_maintenance_does_not_leave_admission_open_when_final_confirmation_fails(
    monkeypatch, failure
):
    # Pure controller error flow, no Root, manager or filesystem operations.
    # Publication atomicity, actual stop and disk failure remain VM-only cases.
    gate = []
    maintenance = object.__new__(lifecycle.Maintenance)
    maintenance.selected = {}
    maintenance.payload = Path("/unused")
    monkeypatch.setattr(maintenance, "change", lambda *_: None)
    monkeypatch.setattr(recovery_database, "admission_guard", nullcontext)

    def publish(_, state):
        gate.append(state)
        if state == "open" and failure == "publication":
            raise OSError("publication readback unavailable")

    def save(_):
        if failure == "receipt":
            raise OSError("maintenance receipt unavailable")

    monkeypatch.setattr(lifecycle.availability, "write", publish)
    monkeypatch.setattr(maintenance, "save", save)
    monkeypatch.setattr(
        lifecycle.operator_units,
        "run",
        lambda *_: {
            "service_preconditions_met": True,
            "ready_for_admission": failure != "readback",
        },
    )
    with pytest.raises((OSError, policy.DeliveryError)):
        maintenance.start(resume=True)
    assert gate == ["draining", "open", "draining"]


def test_failed_gate_closure_requires_known_api_stop_without_forging_owner_cleanup(monkeypatch):
    # Controller flow only; actual cgroup/stop/full-volume evidence is VM-only.
    maintenance = object.__new__(lifecycle.Maintenance)
    maintenance.selected = {}
    calls = []

    def unavailable(*_):
        raise OSError("publication unavailable")

    monkeypatch.setattr(lifecycle.availability, "write", unavailable)
    monkeypatch.setattr(
        maintenance, "unit", lambda name: {"ActiveState": "inactive", "MainPID": "0"}
    )
    monkeypatch.setattr(lifecycle.service_install, "systemctl", lambda *args: calls.append(args))
    monkeypatch.setattr(
        maintenance, "empty_cgroup", lambda name: calls.append(("prove_empty", name))
    )
    maintenance.close_failed_admission()
    assert calls == [("--no-block", "stop", "kf-api.service"), ("prove_empty", "api")]


def proxy_catalog_shape():
    # Structural example, not trusted executable bytes or ELF/version evidence.
    names = ["bin/nginx", "lib/libc.so.6", "lib/ld-linux-x86-64.so.2"]
    return {
        "schema_version": 1,
        "format": "kf.nginx.runtime.v1",
        "version": "1.30.5",
        "architecture": "x86_64",
        "package_sha256": "b" * 64,
        "directories": ["bin", "lib"],
        "files": [
            {
                "path": name,
                "bytes": 1,
                "sha256": "b" * 64,
                "mode": "0555" if name == "bin/nginx" else "0444",
            }
            for name in names
        ],
        "payload_bytes": 3,
    }


@pytest.mark.parametrize(
    "field,value", [("version", "latest"), ("schema_version", True), ("architecture", "arm64")]
)
def test_proxy_runtime_cannot_silently_select_another_binary_profile(field, value):
    value0 = proxy_catalog_shape()
    proxy_runtime.validate(value0)
    value0[field] = value
    with pytest.raises(policy.DeliveryError):
        proxy_runtime.validate(value0)


def test_proxy_runtime_cannot_smuggle_tls_or_configuration_into_public_binary_bundle():
    value = proxy_catalog_shape()
    value["files"].append({"path": "lib/key.pem", "bytes": 1, "sha256": "b" * 64, "mode": "0444"})
    value["payload_bytes"] += 1
    with pytest.raises(policy.DeliveryError):
        proxy_runtime.validate(value)
    value = proxy_catalog_shape()
    value["files"][1]["mode"] = "0555"
    with pytest.raises(policy.DeliveryError):
        proxy_runtime.validate(value)


def proxy_process_shape():
    return {
        "uids": ["1205"] * 4,
        "gids": ["1205"] * 4,
        "groups": ["1205"],
        "no_new_privileges": "1",
        "capabilities": ["0000000000000400"] * 5,
        "pid": 1,
        "cgroup": "/sys/fs/cgroup/kfonlinejudge.slice/kf-proxy.service",
        "limits": {
            "memory.max": str(128 * 1024**2),
            "memory.swap.max": "0",
            "pids.max": "16",
            "cpu.max": "50000 100000",
        },
        "node_limits": {
            "memory.max": str(3840 * 1024**2),
            "memory.swap.max": "0",
            "pids.max": "384",
            "cpu.max": "200000 100000",
        },
        "process_limits": {
            "core": [0, 0],
            "file": [8 * 1024**2] * 2,
            "files": [256] * 2,
            "stack": [16 * 1024**2] * 2,
        },
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", True),
        ("pid", 2),
        ("uids", ["0"] * 4),
        ("groups", ["1205", "0"]),
        ("capabilities", ["0000000000200400"] * 5),
    ],
)
def test_proxy_cannot_trade_the_listener_capability_for_other_authority(field, value):
    observed = proxy_process_shape()
    proxy_guard.validate_runtime(observed)
    observed[field] = value
    with pytest.raises(policy.DeliveryError):
        proxy_guard.validate_runtime(observed)


def test_proxy_render_is_fixed_to_a_selected_snapshot_without_business_credentials():
    selected = {
        "release_id": "sd-" + "b" * 12,
        "configuration_id": "cfg-" + "c" * 24,
        "manifest_sha256": "d" * 64,
    }
    text = proxy_service.render(selected).decode()
    assert "User=kf-proxy" in text and "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" in text
    assert "PrivatePIDs=yes" in text and "LogNamespace=kf-onlinejudge" in text
    assert "EnvironmentFile=" not in text and "LoadCredential=" not in text
