"""Finite local DB/broker composition; installed limits require separate readback."""

import argparse
import base64
import hashlib
import json
import os
import resource
import secrets
import stat
from pathlib import Path
from urllib.parse import urlsplit

from . import configuration, install, preflight, service_install
from .policy import DeliveryError, canonical, digest
from .services import python_environment

QUEUE = "kf.judge.evaluations.v1"
QUEUE_ARGUMENTS = {
    "x-max-length": 64,
    "x-max-length-bytes": 32768,
    "x-overflow": "reject-publish",
    "x-message-ttl": 600000,
}
STORAGE = {
    "postgres": {
        "path": "/var/lib/kf-onlinejudge/postgres",
        "filesystem_bytes": 2 * 1024**3,
    },
    "rabbitmq": {
        "path": "/var/lib/kf-onlinejudge/rabbitmq",
        "filesystem_bytes": 512 * 1024**2,
    },
}
CONFIGURATION_ROOT = Path("/etc/kf-dependencies")


def storage_observation(name: str) -> dict:
    """Read an already provisioned dedicated filesystem, never create/mount one."""
    directory = Path(STORAGE[name]["path"])
    preflight.protected(directory.parent, directory=True)
    if directory.is_symlink() or not directory.is_dir():
        raise DeliveryError("ordinary dependency data directory required")
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    rows = [row for row in mounts if len(row) > 6 and row[4] == str(directory)]
    if len(rows) != 1:
        raise DeliveryError("one dedicated dependency storage mount required")
    row = rows[0]
    separator = row.index("-")
    fs = os.statvfs(directory)
    observation = {
        "root": row[3],
        "mountpoint": row[4],
        "filesystem": row[separator + 1],
        "options": row[5].split(","),
        "device": row[2],
        "shared_elsewhere": any(other[2] == row[2] and other != row for other in mounts),
        "capacity_bytes": fs.f_frsize * fs.f_blocks,
    }
    validate_storage(name, observation)
    return observation


def validate_storage(name: str, observation: dict) -> None:
    required = STORAGE[name]["filesystem_bytes"]
    if (
        observation["root"] != "/"
        or observation["mountpoint"] != STORAGE[name]["path"]
        or observation["filesystem"] != "ext4"
        or observation["shared_elsewhere"] is not False
        or not {"rw", "nosuid", "nodev", "noexec"} <= set(observation["options"])
        or type(observation["capacity_bytes"]) is not int
        or not required * 9 // 10 <= observation["capacity_bytes"] <= required
    ):
        raise DeliveryError("dedicated hard dependency storage budget differs")


def check_runtime(name: str) -> dict:
    uid = {"postgres": 1203, "rabbitmq": 1204}[name]
    if (
        os.getuid() != uid
        or os.geteuid() != uid
        or os.getgid() != uid
        or not set(os.getgroups()) <= {uid}
    ):
        raise DeliveryError("dedicated ordinary dependency identity required")
    status = Path("/proc/self/status").read_text()
    values = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    if values.get("NoNewPrivs", "").strip() != "1" or any(
        not values.get(key, "").strip() or int(values[key].strip(), 16) != 0
        for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    ):
        raise DeliveryError("dependency privilege boundary differs")
    group = preflight.current_cgroup()
    if str(group) != "/sys/fs/cgroup/kfonlinejudge.slice/kf-" + name + ".service":
        raise DeliveryError("dependency cgroup identity differs")
    limits = {
        key: (group / key).read_text().strip()
        for key in (
            "memory.max",
            "memory.swap.max",
            "pids.max",
            "cpu.max",
        )
    }
    if limits != {
        "memory.max": "536870912",
        "memory.swap.max": "0",
        "pids.max": "64",
        "cpu.max": "100000 100000",
    }:
        raise DeliveryError("effective dependency budgets differ")
    node = Path("/sys/fs/cgroup/kfonlinejudge.slice")
    actual_node = {key: (node / key).read_text().strip() for key in limits}
    if actual_node != {
        "memory.max": "4026531840",
        "memory.swap.max": "0",
        "pids.max": "384",
        "cpu.max": "200000 100000",
    }:
        raise DeliveryError("effective dependency node budget differs")
    for key, maximum in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, 256 * 1024**2),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_STACK, 16 * 1024**2),
    ):
        if resource.getrlimit(key) != (maximum, maximum):
            raise DeliveryError("effective dependency process limits differ")
    directory = Path(STORAGE[name]["path"])
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or info.st_gid != uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DeliveryError("private dependency data identity/mode differs")
    storage = storage_observation(name)
    return {
        "dependency": name,
        "actual_cgroup_limits": limits,
        "data_filesystem_bytes": storage["capacity_bytes"],
        "execution_verified": False,
    }


def salted_password(password: str) -> str:
    salt = secrets.token_bytes(4)
    return base64.b64encode(salt + hashlib.sha256(salt + password.encode()).digest()).decode()


def broker_definitions(connections: dict) -> dict:
    connections = configuration.validate_connections(connections)
    users = []
    permissions = []
    queue = r"^kf\.judge\.evaluations\.v1$"
    for role in ("outbox", "worker"):
        endpoint = urlsplit(connections["amqp"][role])
        users.append(
            {
                "name": "kf_" + role,
                "password_hash": salted_password(endpoint.password),
                "hashing_algorithm": "rabbit_password_hashing_sha256",
                "tags": [],
                "limits": {"max-connections": 2, "max-channels": 4},
            }
        )
        permissions.append(
            {
                "user": "kf_" + role,
                "vhost": "kf",
                "configure": queue,
                "write": r"^(amq\.default|kf\.judge\.evaluations\.v1)$"
                if role == "outbox"
                else "^$",
                "read": queue if role == "worker" else "^$",
            }
        )
    return {
        "users": users,
        # In 4.3.6 add_vhost does not apply the exported `limits` member.
        # Fixed default_limits.vhosts in rabbitmq.conf applies on creation;
        # existing-vhost limits must be read back before service readiness.
        "vhosts": [{"name": "kf", "metadata": {"default_queue_type": "classic"}}],
        "permissions": permissions,
        "queues": [
            {
                "name": QUEUE,
                "vhost": "kf",
                "durable": True,
                "auto_delete": False,
                "arguments": dict(QUEUE_ARGUMENTS),
            }
        ],
    }


def render(release: str, batch: str) -> dict[str, bytes]:
    # These identities are normally supplied by an installed selected receipt.
    import re

    if not re.fullmatch(r"sd-[0-9a-f]{12}", release) or not re.fullmatch(
        r"cfg-[0-9a-f]{24}", batch
    ):
        raise DeliveryError("invalid dependency configuration binding")
    # The application's /etc/kf-onlinejudge is 0700: dependency identities
    # must use a separate root-owned searchable tree with group-readable files.
    base = str(CONFIGURATION_ROOT / f"{release}.{batch}")
    pg = (
        "\n".join(
            [
                "data_directory = '/var/lib/kf-onlinejudge/postgres/data'",
                f"hba_file = '{base}/pg_hba.conf'",
                "listen_addresses = '127.0.0.1'",
                "port = 5432",
                "unix_socket_directories = '/run/postgresql'",
                "unix_socket_permissions = 0777",
                "password_encryption = 'scram-sha-256'",
                "max_connections = 32",
                "superuser_reserved_connections = 3",
                "shared_buffers = '64MB'",
                "work_mem = '1MB'",
                "hash_mem_multiplier = 1",
                "maintenance_work_mem = '32MB'",
                "autovacuum_work_mem = '32MB'",
                "autovacuum_max_workers = 2",
                "max_worker_processes = 4",
                "max_parallel_workers = 0",
                "max_wal_senders = 0",
                "temp_file_limit = '16MB'",
                "max_files_per_process = 128",
                "statement_timeout = '10s'",
                "lock_timeout = '2s'",
                "idle_in_transaction_session_timeout = '60s'",
                "max_wal_size = '64MB'",
                "min_wal_size = '32MB'",
                "logging_collector = off",
                "log_statement = 'none'",
                "log_min_duration_statement = -1",
                "log_min_error_statement = 'panic'",
                "log_connections = ''",
                "log_disconnections = off",
            ]
        )
        + "\n"
    )
    hba = (
        "local all all scram-sha-256\n"
        "host all all 127.0.0.1/32 scram-sha-256\n"
        "host all all 0.0.0.0/0 reject\n"
        "host all all ::0/0 reject\n"
    )
    broker = (
        "\n".join(
            [
                "listeners.tcp.1 = 127.0.0.1:5672",
                "connection_max = 8",
                "channel_max = 8",
                "channel_max_per_node = 16",
                "ranch_connection_max = 8",
                "max_message_size = 4096",
                "handshake_timeout = 5000",
                "heartbeat = 30",
                "consumer_timeout = 1800000",
                "vm_memory_high_watermark.absolute = 256MiB",
                "total_memory_available_override_value = 512MiB",
                "disk_free_limit.absolute = 64MiB",
                "log.console = true",
                "log.console.level = warning",
                "log.file = false",
                "definitions.import_backend = local_filesystem",
                f"definitions.local.path = {base}/definitions.json",
                "default_limits.vhosts.1.pattern = ^kf$",
                "default_limits.vhosts.1.max_connections = 4",
                "default_limits.vhosts.1.max_queues = 1",
            ]
        )
        + "\n"
    )
    files = {
        "postgresql.conf": pg.encode(),
        "pg_hba.conf": hba.encode(),
        "rabbitmq.conf": broker.encode(),
        "enabled_plugins": b"[].\n",
    }
    for name, user in {"postgres": "postgres", "rabbitmq": "rabbitmq"}.items():
        payload = install.RELEASES / release / "payload"
        values = [
            "[Unit]",
            "Description=KF bounded local " + name,
            "After=local-fs.target",
            "StartLimitIntervalSec=600",
            "StartLimitBurst=3",
            "[Service]",
            "Type=exec",
            "User=" + user,
            "Group=" + user,
            "Slice=kfonlinejudge.slice",
            "UMask=0077",
            "MemoryMax=512M",
            "MemorySwapMax=0",
            "TasksMax=64",
            "CPUQuota=100%",
            "LimitCORE=0",
            "LimitFSIZE=256M",
            "LimitNOFILE=256",
            "LimitSTACK=16M",
            "TimeoutStartSec=120",
            "TimeoutStopSec=30",
            "Restart=on-failure",
            "RestartSec=30",
            "RuntimeMaxSec=24h",
            "KillMode=control-group",
            "NoNewPrivileges=yes",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "ProtectControlGroups=yes",
            "PrivateTmp=yes",
            "PrivateDevices=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "ReadWritePaths=" + STORAGE[name]["path"],
            "RequiresMountsFor=" + STORAGE[name]["path"],
            "StandardOutput=journal",
            "StandardError=journal",
            "LogRateLimitIntervalSec=30s",
            "LogRateLimitBurst=100",
            "ExecStartPre=+/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C "
            + python_environment(payload)
            + f" {payload}/python/bin/python3.13"
            + " -s -P -m scripts.release.services check-binding"
            + f" --release-id {release} --configuration-id {batch}",
            "ExecStartPre=/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C "
            + python_environment(payload)
            + f" {payload}/python/bin/python3.13"
            + " -s -P -m scripts.release.dependencies --check-service "
            + name,
        ]
        if name == "postgres":
            values += [
                "RuntimeDirectory=postgresql",
                "RuntimeDirectoryMode=0755",
                (
                    "ExecStart=/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8"
                    f" /usr/bin/postgres -c config_file={base}/postgresql.conf"
                ),
            ]
        else:
            values += [
                "WorkingDirectory=/var/lib/kf-onlinejudge/rabbitmq",
                (
                    "ExecStart=/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8"
                    " HOME=/var/lib/kf-onlinejudge/rabbitmq ERL_EPMD_ADDRESS=127.0.0.1"
                    " RABBITMQ_NODENAME=kf@localhost RABBITMQ_DIST_PORT=25672"
                    f" RABBITMQ_CONFIG_FILE={base}/rabbitmq.conf"
                    f" RABBITMQ_ENABLED_PLUGINS_FILE={base}/enabled_plugins"
                    " RABBITMQ_MNESIA_BASE=/var/lib/kf-onlinejudge/rabbitmq/data"
                    ' RABBITMQ_LOGS=- "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS='
                    "+S 2:2 +A 4 +P 1024 +Q 1024"
                    ' -kernel inet_dist_use_interface {127,0,0,1}"'
                    " /usr/lib/rabbitmq/sbin/rabbitmq-server"
                ),
            ]
        values += ["[Install]", "WantedBy=multi-user.target"]
        files["kf-" + name + ".service"] = ("\n".join(values) + "\n").encode()
    return files


def generate(connections_path: Path, output: Path) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0:
        raise DeliveryError("dependency composition requires root operator")
    selected, original = service_install.current_receipt()
    payload = install.RELEASES / selected["release_id"] / "payload"
    installed = payload / "delivery/scripts/release/dependencies.py"
    preflight.protected(installed)
    if digest(installed) != digest(Path(__file__)):
        raise DeliveryError("dependency composition differs from selected delivery")
    configuration.private(connections_path)
    from .policy import read_json

    connections = configuration.validate_connections(read_json(connections_path, 16384))
    for role in ("outbox", "worker"):
        # Bound to the same already-installed credential batch, not a second
        # set of broker identities hidden in the dependency definition file.
        if role == "outbox":
            env = configuration.environment_read(
                configuration.installed_path(
                    selected["release_id"],
                    selected["configuration_id"],
                    "outbox.env",
                ).read_bytes()
            )
            expected = env["KF_OJ_AMQP_URL"]
        else:
            expected = read_json(
                configuration.installed_path(
                    selected["release_id"],
                    selected["configuration_id"],
                    "worker.json",
                ),
                32768,
            )["amqp_url"]
        if connections["amqp"][role] != expected:
            raise DeliveryError("dependency broker credentials differ from selected batch")
    files = render(selected["release_id"], selected["configuration_id"])
    files["definitions.json"] = canonical(broker_definitions(connections))
    if output.exists() or output.is_symlink():
        raise DeliveryError("dependency output must be new")
    preflight.protected(output.parent, directory=True)
    output.mkdir(mode=0o700)
    hashes = {}
    for name, contents in files.items():
        path = output / name
        path.write_bytes(contents)
        path.chmod(0o600)
        hashes[name] = digest(path)
    result = {
        "schema_version": 1,
        "source_sha": original["source_sha"],
        "configuration_id": selected["configuration_id"],
        "configuration_receipt_sha256": selected["receipt_sha256"],
        "files": hashes,
        "required_storage_mounts": STORAGE,
        "installed": False,
        "storage_limits_verified": False,
        "services_started": False,
        "execution_verified": False,
    }
    path = output / "receipt.json"
    path.write_bytes(canonical(result))
    path.chmod(0o600)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded local dependency composition")
    parser.add_argument("--connections", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check-service", choices=STORAGE)
    args = parser.parse_args()
    try:
        if args.check_service is not None:
            if args.connections is not None or args.output is not None:
                raise DeliveryError("unexpected runtime check override")
            result = check_runtime(args.check_service)
        else:
            if args.connections is None or args.output is None:
                raise DeliveryError("explicit dependency composition inputs required")
            result = generate(args.connections, args.output)
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError) as error:
        parser.exit(
            1,
            "Dependency composition refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
