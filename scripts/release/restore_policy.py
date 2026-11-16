"""Restore compatibility and fresh operational identities; no data/state writes."""

import json
from pathlib import Path
from urllib.parse import urlsplit

from . import backup_manifest, configuration, install, manifest
from .policy import DeliveryError, digest, fields, read_json, sha256


def snapshot(paths: dict[str, Path]) -> dict:
    """Extract fixed credential roles without serializing any secret to a receipt."""
    if set(paths) != configuration.CONFIG_FILES:
        raise DeliveryError("complete recovery configuration snapshot required")
    env = {
        name: configuration.environment_read(paths[name].read_bytes())
        for name in paths
        if name.endswith(".env")
    }
    coordinator = read_json(paths["coordinator.json"], 32768)
    worker = read_json(paths["worker.json"], 32768)
    measurement = fields(
        read_json(paths["measurement.json"], 32768),
        {"binding", "epoch", "operator_attested"},
        "recovery measurement",
    )

    def database(name: str, key: str = "KF_OJ_DATABASE_URL") -> str:
        value = env[name].get(key)
        if not isinstance(value, str) or not value.startswith("postgresql+asyncpg:"):
            raise DeliveryError("recovery backend driver differs")
        return value.replace("postgresql+asyncpg:", "postgresql:", 1)

    endpoints = configuration.validate_connections(
        {
            "schema_version": 1,
            "database": {
                "api": database("api.env"),
                "coordinator": coordinator.get("database_url"),
                "import": database("import.env"),
                "identity": database("identity.env", "KF_OJ_IDENTITY_OPERATOR_DATABASE_URL"),
                "migration": database("migration.env"),
            },
            "amqp": {
                "outbox": env["outbox.env"].get("KF_OJ_AMQP_URL"),
                "worker": worker.get("amqp_url"),
            },
        }
    )
    binding = measurement["binding"]
    node = configuration.validate_profile(
        {
            "schema_version": 1,
            "origin": json.loads(env["api.env"]["KF_OJ_BROWSER_ORIGINS"])[0],
            "worker_id": binding["worker_id"],
            "measurement_nonce": binding["measurement_nonce"],
        }
    )
    if worker.get("worker_id") != node["worker_id"]:
        raise DeliveryError("recovery worker binding differs")
    return {
        "endpoints": endpoints,
        "worker_id": node["worker_id"],
        "measurement_nonce": node["measurement_nonce"],
        "measurement_epoch": measurement["epoch"],
        "operator_attested": measurement["operator_attested"],
        "worker_token": sha256(worker.get("service_token")),
        "platform_token": sha256(coordinator.get("platform_token")),
    }


def fresh_identities(previous: dict, current: dict) -> None:
    if (
        current["operator_attested"] is not True
        or current["measurement_nonce"] is None
        or current["measurement_epoch"] is None
        or current["worker_id"] == previous["worker_id"]
        or current["measurement_nonce"] == previous["measurement_nonce"]
        or current["measurement_epoch"] == previous["measurement_epoch"]
        or {current["worker_token"], current["platform_token"]}
        & {previous["worker_token"], previous["platform_token"]}
    ):
        raise DeliveryError("fresh recovery worker/tokens/attested measurement epoch required")
    sha256(current["measurement_nonce"])
    sha256(current["measurement_epoch"])
    old_secrets = {
        urlsplit(value).password
        for kind in ("database", "amqp")
        for value in previous["endpoints"][kind].values()
    }
    new_secrets = {
        urlsplit(value).password
        for kind in ("database", "amqp")
        for value in current["endpoints"][kind].values()
    }
    if old_secrets & new_secrets:
        raise DeliveryError("recovery database/broker credentials must all be fresh")


def compatible(backup: dict, payload: Path, target_data: dict, private_root: Path) -> None:
    """Cold data restoration requires unchanged migration bytes and runtime ABI.

    A migration-changing upgrade uses the dedicated Alembic upgrade procedure,
    never a raw cold copy into an incompatible target schema/runtime.
    """
    selected = backup["selected"]
    old_manifest = backup_manifest.named_member(
        private_root, backup, "releases/" + selected["release_id"] + "/manifest.json"
    )
    old = manifest.validate(read_json(old_manifest))
    if (
        digest(old_manifest) != backup["manifest_sha256"]
        or old["source_sha"] != backup["source_sha"]
        or old["images"] != backup["images"]
        or old["images"] != target_data["images"]
        or old["platform"] != target_data["platform"]
        or old["toolchains"] != target_data["toolchains"]
    ):
        raise DeliveryError("cold recovery runtime/platform/toolchain identity differs")

    def migrations(value: dict) -> dict:
        return {
            row["path"]: (row["sha256"], row["bytes"])
            for row in value["files"]
            if row["path"].startswith("backend/migrations/")
        }

    if not migrations(old) or migrations(old) != migrations(target_data):
        raise DeliveryError("cold recovery requires identical Backend migration inventory")
    if (
        backup_manifest.named_member(private_root, backup, "postgres/PG_VERSION").read_bytes()
        != b"18\n"
    ):
        raise DeliveryError("cold recovery PostgreSQL major version differs")
    # Every old data byte was verified by the closed private inventory. This
    # check also binds the independently verified target public payload.
    if payload != install.RELEASES / target_data["release_id"] / "payload":
        raise DeliveryError("recovery target payload is not the installed release")


def identity_check(backup: dict, selected: dict, root: Path) -> dict:
    paths = {
        name: configuration.installed_path(
            selected["release_id"], selected["configuration_id"], name
        )
        for name in configuration.CONFIG_FILES
    }
    old = backup["selected"]
    prior = {
        name: ("judge-config" if name == "runtime.json" else "app-config")
        + "/"
        + configuration.installed_path(old["release_id"], old["configuration_id"], name).name
        for name in configuration.CONFIG_FILES
    }
    inventory = {item["path"]: item for item in backup["files"]}
    for relative in prior.values():
        if (
            relative not in inventory
            or digest(backup_manifest.member_path(root, backup, inventory[relative]))
            != inventory[relative]["sha256"]
        ):
            raise DeliveryError("old protected recovery configuration is missing")
    previous, current = (
        snapshot(
            {
                name: backup_manifest.member_path(root, backup, inventory[relative])
                for name, relative in prior.items()
            }
        ),
        snapshot(paths),
    )
    fresh_identities(previous, current)
    cookie = Path("/var/lib/kf-onlinejudge/rabbitmq/.erlang.cookie")
    if digest(cookie) == backup["broker_cookie_sha256"]:
        raise DeliveryError("recovery broker distribution cookie must be fresh")
    return current
