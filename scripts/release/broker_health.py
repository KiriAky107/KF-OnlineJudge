"""Finite ordinary-cookie metadata queries; no queue/user mutation or message reads."""

import argparse
import json
import stat
import subprocess
import tempfile
from pathlib import Path

from . import dependency_policy as dependencies
from . import operator_units, preflight
from .policy import DeliveryError, canonical, fields, integer

CTL = Path("/usr/lib/rabbitmq/sbin/rabbitmqctl")
QUERIES = {
    "queues": [
        "list_queues",
        "-p",
        "kf",
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
    ],
    "vhost_limits": ["list_vhost_limits", "--global"],
    "users": ["list_users"],
    "permissions": ["list_permissions", "-p", "kf"],
}


def queue_arguments(value) -> dict:
    if isinstance(value, dict):
        result = value
    elif isinstance(value, list) and len(value) == 4:
        result = {}
        for row in value:
            if not isinstance(row, list) or len(row) != 3 or row[0] in result:
                raise DeliveryError("duplicate/unknown broker argument")
            name, kind, argument = row
            if name not in dependencies.QUEUE_ARGUMENTS or kind != (
                "longstr" if name == "x-overflow" else "long"
            ):
                raise DeliveryError("broker argument type differs")
            result[name] = argument
    else:
        raise DeliveryError("broker argument shape differs")
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


def command(name: str):
    if name not in QUERIES:
        raise DeliveryError("unknown broker metadata query")
    # A file-backed capped stream prevents malicious metadata/log output from
    # allocating an unbounded capture buffer. The unit adds a hard file limit.
    env = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "HOME": dependencies.STORAGE["rabbitmq"]["path"],
        "ERL_EPMD_ADDRESS": "127.0.0.1",
        "RABBITMQ_NODENAME": "kf@localhost",
        "RABBITMQ_CTL_ERL_ARGS": (
            "+S 1:1 +A 2 +P 1024 +Q 1024 -kernel inet_dist_use_interface {127,0,0,1}"
        ),
    }
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                [
                    str(CTL),
                    "-q",
                    "-n",
                    "kf@localhost",
                    "--timeout",
                    "5",
                    *QUERIES[name],
                    "--formatter=json",
                ],
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=8,
                env=env,
            )
        except subprocess.TimeoutExpired as error:
            raise DeliveryError("broker " + name + " query exceeded deadline") from error
        if result.returncode:
            raise DeliveryError("broker " + name + " query failed")
        if output.tell() > 32768:
            raise DeliveryError("broker " + name + " query output exceeds bound")
        output.seek(0)
        return operator_units.decode(output.read(32769))


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
    data = {name: command(name) for name in QUERIES}
    return validate_snapshot(data)


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
