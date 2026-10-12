"""Pure delivery-contract tests; these do not claim installation or VM safety."""

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
policy = importlib.import_module("scripts.release.policy")
manifest = importlib.import_module("scripts.release.manifest")
architecture = importlib.import_module("scripts.check_architecture")


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
