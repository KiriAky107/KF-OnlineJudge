"""Finite ordinary-cookie metadata queries; no queue/user mutation or message reads."""

import argparse
import json
import stat
import subprocess
from pathlib import Path

from . import broker_snapshot, operator_units, preflight
from . import dependency_policy as dependencies
from .policy import DeliveryError, canonical, fields, integer

CTL = broker_snapshot.CTL


def queue_arguments(value) -> dict:
    if isinstance(value, dict):
        result = dict(value)
    elif isinstance(value, list) and len(value) in (4, 5):
        result = {}
        for row in value:
            if not isinstance(row, list) or len(row) != 3 or row[0] in result:
                raise DeliveryError("duplicate/unknown broker argument")
            name, kind, argument = row
            if name not in {*dependencies.QUEUE_ARGUMENTS, "x-queue-type"} or kind != (
                "longstr" if name in {"x-overflow", "x-queue-type"} else "long"
            ):
                raise DeliveryError("broker argument type differs")
            result[name] = argument
    else:
        raise DeliveryError("broker argument shape differs")
    if "x-queue-type" in result and result.pop("x-queue-type") != "classic":
        raise DeliveryError("actual broker queue type differs")
    if canonical(result) != canonical(dependencies.QUEUE_ARGUMENTS):
        raise DeliveryError("actual broker queue budgets differ")
    return result


def validate_snapshot(value: dict) -> dict:
    fields(value, {"queues", "vhost_limits", "users", "permissions"}, "broker snapshot")
    rows = value["queues"]
    if not isinstance(rows, list) or len(rows) != 1:
        raise DeliveryError("actual broker queue inventory differs")
    queue = fields(
        rows[0],
        {
            "name",
            "durable",
            "auto_delete",
            "exclusive",
            "type",
            "state",
            "arguments",
            "messages_ready",
            "messages_unacknowledged",
            "message_bytes",
            "consumers",
            "policy",
            "operator_policy",
            "effective_policy_definition",
        },
        "broker queue",
    )
    if (
        queue["name"] != dependencies.QUEUE
        or queue["durable"] is not True
        or queue["auto_delete"] is not False
        or queue["exclusive"] is not False
        or queue["type"] != "classic"
        or queue["state"] != "running"
        or queue["policy"] not in (None, "")
        or queue["operator_policy"] not in (None, "")
        or queue["effective_policy_definition"] not in ({}, [])
    ):
        raise DeliveryError("actual broker queue semantics differ")
    queue_arguments(queue["arguments"])
    limits = value["vhost_limits"]
    if not isinstance(limits, list) or len(limits) != 1:
        raise DeliveryError("actual broker vhost limits unavailable")
    row = fields(limits[0], {"vhost", "limits"}, "broker vhost limits")
    if row["vhost"] != "kf" or not isinstance(row["limits"], str):
        raise DeliveryError("actual broker vhost binding differs")
    if canonical(operator_units.decode(row["limits"].encode(), 1024)) != canonical(
        {"max-connections": 4, "max-queues": 1}
    ):
        raise DeliveryError("actual broker vhost budgets differ")
    users = value["users"]
    if (
        not isinstance(users, list)
        or len(users) != 2
        or canonical(sorted(users, key=lambda u: u.get("user", "")))
        != canonical(
            [
                {"user": "kf_outbox", "tags": []},
                {"user": "kf_worker", "tags": []},
            ]
        )
    ):
        raise DeliveryError("actual broker users/tags differ")
    permissions = value["permissions"]
    expected = [
        {
            "user": "kf_" + role,
            "configure": r"^kf\.judge\.evaluations\.v1$",
            "write": r"^(amq\.default|kf\.judge\.evaluations\.v1)$" if role == "outbox" else "^$",
            "read": r"^kf\.judge\.evaluations\.v1$" if role == "worker" else "^$",
        }
        for role in ("outbox", "worker")
    ]
    if (
        not isinstance(permissions, list)
        or len(permissions) != 2
        or canonical(sorted(permissions, key=lambda u: u.get("user", ""))) != canonical(expected)
    ):
        raise DeliveryError("actual broker authority differs")
    return {
        "schema_version": 1,
        "metadata_verified": True,
        "messages_ready": integer(queue["messages_ready"], 0, 64, "broker ready count"),
        "messages_unacknowledged": integer(
            queue["messages_unacknowledged"], 0, 1, "broker unacknowledged count"
        ),
        "message_bytes": integer(queue["message_bytes"], 0, 32768 + 4096, "broker bytes"),
        "consumers": integer(queue["consumers"], 0, 1, "broker consumer count"),
    }


def observe() -> dict:
    operator_units.guarded("broker-health")
    preflight.protected(CTL)
    cookie = Path(dependencies.STORAGE["rabbitmq"]["path"]) / ".erlang.cookie"
    info = cookie.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1204, 1204, 0o400)
    ):
        raise DeliveryError("ordinary private broker cookie required")
    return validate_snapshot(broker_snapshot.command())


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded ordinary broker metadata")
    parser.add_argument("--inside", action="store_true", required=True)
    parser.parse_args()
    try:
        print(json.dumps(observe(), sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Broker health refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
