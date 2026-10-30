"""Ordinary fixed manager-response models; no host units or VM acceptance."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
lifecycle = importlib.import_module("scripts.release.lifecycle")
DeliveryError = importlib.import_module("scripts.release.policy").DeliveryError


@pytest.fixture
def manager_snapshot(monkeypatch):
    selected = {"release_id": "sd-" + "a" * 12, "configuration_id": "cfg-" + "b" * 24}
    reads, queries = [], []

    def directory(base):
        def known(path):
            reads.append(path.name)
            return base / (selected["release_id"] + "." + selected["configuration_id"])

        return known

    for module, base in [
        (lifecycle.service_install, lifecycle.service_install.UNITS),
        (lifecycle.dependency_install, lifecycle.dependency_install.ROOT),
        (lifecycle.proxy_install, lifecycle.proxy_service.UNITS),
        (lifecycle.factory_service, lifecycle.factory_service.UNITS),
    ]:
        monkeypatch.setattr(module, "known_link", directory(base))

    def block(name, **overrides):
        values = {
            "Id": "kf-" + name + ".service",
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "MainPID": "0",
            "FragmentPath": str(
                lifecycle.service_install.SYSTEM_UNITS / ("kf-" + name + ".service")
            ),
        }
        values.update(overrides)
        return "\n".join(key + "=" + value for key, value in values.items())

    output = [block("postgres") + "\n\n" + block("rabbitmq") + "\n"]

    def systemctl(*args):
        queries.append(args)
        return output[0]

    monkeypatch.setattr(lifecycle.service_install, "systemctl", systemctl)
    return selected, reads, queries, output, block


def test_fixed_state_batch_checks_all_targets_and_uses_one_manager_query(manager_snapshot):
    selected, reads, queries, _, _ = manager_snapshot
    result = lifecycle.selected_units(selected, lifecycle.DEPENDENCIES)
    assert reads == ["kf-postgres.service", "kf-rabbitmq.service"]
    assert queries == [
        (
            "show",
            "kf-postgres.service",
            "kf-rabbitmq.service",
            "--property=Id,LoadState,ActiveState,SubState,MainPID,FragmentPath",
        )
    ]
    assert set(result) == set(lifecycle.DEPENDENCIES)
    assert all(value["MainPID"] == "0" and "Id" not in value for value in result.values())


def test_state_batch_binds_canonical_ids_independently_of_response_order(manager_snapshot):
    selected, _, _, output, block = manager_snapshot
    output[0] = block("rabbitmq", MainPID="20", ActiveState="active") + "\n\n" + block("postgres")
    result = lifecycle.selected_units(selected, lifecycle.DEPENDENCIES)
    assert result["rabbitmq"]["MainPID"] == "20"
    assert result["postgres"]["MainPID"] == "0"


@pytest.mark.parametrize(
    "names", [(), [], ("postgres", "postgres"), ("postgres", "unknown"), ("postgres", None)]
)
def test_bad_batch_inventory_is_refused_before_links_or_manager(manager_snapshot, names):
    selected, reads, queries, _, _ = manager_snapshot
    with pytest.raises(DeliveryError, match="lifecycle targets"):
        lifecycle.selected_units(selected, names)
    assert reads == queries == []


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate_unit",
        "foreign_unit",
        "duplicate_field",
        "missing_field",
        "extra_field",
        "foreign_path",
        "not_loaded",
        "empty",
    ],
)
def test_partial_ambiguous_or_foreign_manager_batch_never_returns_states(
    manager_snapshot, mutation
):
    selected, _, queries, output, block = manager_snapshot
    first, second = block("postgres"), block("rabbitmq")
    if mutation == "missing":
        output[0] = first
    elif mutation == "duplicate_unit":
        output[0] = first + "\n\n" + first
    elif mutation == "foreign_unit":
        output[0] = first + "\n\n" + block("api")
    elif mutation == "duplicate_field":
        output[0] = first + "\nMainPID=123\n\n" + second
    elif mutation == "missing_field":
        output[0] = first.replace("MainPID=0\n", "") + "\n\n" + second
    elif mutation == "extra_field":
        output[0] = first + "\nUnknown=value\n\n" + second
    elif mutation == "foreign_path":
        output[0] = first + "\n\n" + block("rabbitmq", FragmentPath="/foreign/kf-rabbitmq.service")
    elif mutation == "not_loaded":
        output[0] = first + "\n\n" + block("rabbitmq", LoadState="not-found")
    else:
        output[0] = ""
    with pytest.raises(DeliveryError):
        lifecycle.selected_units(selected, lifecycle.DEPENDENCIES)
    assert len(queries) == 1


def test_foreign_second_selected_directory_prevents_the_entire_query(manager_snapshot, monkeypatch):
    selected, _, queries, _, _ = manager_snapshot
    original = lifecycle.dependency_install.known_link

    def foreign(path):
        return Path("/foreign") if path.name == "kf-rabbitmq.service" else original(path)

    monkeypatch.setattr(lifecycle.dependency_install, "known_link", foreign)
    with pytest.raises(DeliveryError, match="selected snapshot"):
        lifecycle.selected_units(selected, lifecycle.DEPENDENCIES)
    assert queries == []


def test_single_unit_keeps_original_manager_properties_and_shape(manager_snapshot):
    selected, _, queries, output, block = manager_snapshot
    output[0] = block("postgres").removeprefix("Id=kf-postgres.service\n")
    result = lifecycle.selected_units(selected, ("postgres",))
    assert result["postgres"]["MainPID"] == "0"
    assert queries == [
        (
            "show",
            "kf-postgres.service",
            "--property=LoadState,ActiveState,SubState,MainPID,FragmentPath",
        )
    ]
