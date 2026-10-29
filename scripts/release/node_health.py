"""Bounded operator readiness: fixed services, ACLs, broker and gated factory capacity."""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from uuid import UUID

from . import (
    availability,
    configuration,
    database,
    dependencies,
    dependency_install,
    factory_service,
    install,
    journal,
    operator_units,
    preflight,
    proxy_health,
    proxy_install,
    proxy_service,
    service_install,
    services,
)
from .policy import DeliveryError, canonical, digest, fields, read_json

# Table/column privilege introspection only. Business state counters come from
# the execution owner, not a second privileged database state reader/writer.
PRIVILEGES = {
    "api": {
        "accounts:SELECT": True,
        "evaluations:SELECT": True,
        "evaluations:state:UPDATE": False,
        "accounts:is_administrator:UPDATE": False,
    },
    "coordinator": {
        "evaluations:SELECT": True,
        "evaluations:state:UPDATE": True,
        "accounts:SELECT": False,
        "problems:active_version_id:UPDATE": False,
    },
    "import": {
        "problem_import_operations:SELECT": True,
        "problem_import_operations:state:UPDATE": True,
        "evaluations:state:UPDATE": False,
        "accounts:SELECT": False,
    },
    "identity": {
        "accounts:SELECT": False,
        "accounts:is_administrator:UPDATE": False,
        "accounts:password_hash:SELECT": False,
        "sessions:token_hash:SELECT": False,
        "evaluations:SELECT": False,
        "evaluations:state:UPDATE": False,
    },
}
AUTHORITY_FUNCTIONS = (
    "public.kf_manage_account_authority(text,text)",
    "public.kf_manage_account_authority_unlogged(text,text)",
)


def function_privileges(role: str) -> dict:
    return {
        name: role == "identity" and name == AUTHORITY_FUNCTIONS[0] for name in AUTHORITY_FUNCTIONS
    }


def binding() -> tuple[dict, dict, Path]:
    selected = install.selection(check_configuration=False)
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in (
        "availability.py",
        "node_health.py",
        "broker_health.py",
        "broker_snapshot.py",
        "factory_health.py",
        "import_health.py",
        "operator_units.py",
        "dependency_policy.py",
        "layout.py",
        "service_profiles.py",
        "outbox_health.py",
        "journal.py",
        "manifest.py",
        "operation_proof.py",
        "proxy_service.py",
        "proxy_install.py",
        "proxy_guard.py",
        "proxy_runtime.py",
        "proxy_health.py",
    ):
        shipped = payload / "delivery/scripts/release" / name
        preflight.protected(shipped)
        if digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("health tool differs from selected delivery")
    receipt = read_json(
        configuration.installed_receipt_path(selected["release_id"], selected["configuration_id"]),
        16384,
    )
    configuration.validate_receipt(receipt, installed=True)
    paths = {}
    for name in configuration.CONFIG_FILES:
        path = configuration.installed_path(
            selected["release_id"], selected["configuration_id"], name
        )
        preflight.protected(path)
        configuration.private(path)
        if digest(path) != receipt["configuration_files"][str(path)]:
            raise DeliveryError("live configuration changed since selection")
        paths[name] = path
    manifest_path = payload.parent / "manifest.json"
    preflight.protected(manifest_path)
    if digest(manifest_path) != selected["manifest_sha256"]:
        raise DeliveryError("selected manifest changed")
    data = read_json(manifest_path)
    # Immutable payload bytes/images are verified by installation and every
    # actual Sandbox request. A frequent status query is not a rebuild/certifier.
    configuration.validate_contents(
        paths, payload, data, attest=receipt["operator_attested_measurement"]
    )
    return selected, paths, payload


@contextmanager
def _status_helpers(payload: Path):
    """Join at most two fixed read-only helpers before returning any observation."""
    names = ("broker-health", "outbox-health", "import-health", "factory-health")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="kf-health") as executor:
        yield {name: executor.submit(operator_units.run, name, payload) for name in names}


def service_profile(name: str) -> tuple[int, int, int, int, int]:
    if name == "proxy":
        return 1205, 128, 16, 50, 8
    if name in services.PROFILES:
        user, memory, pids, cpu, _, _ = services.PROFILES[name]
        return preflight.ACCOUNTS[user], memory, pids, cpu, 64
    if name in {"postgres", "rabbitmq"}:
        return (1203 if name == "postgres" else 1204), 512, 64, 100, 256
    if name == "sandbox-factory":
        return 0, 256, 32, 50, 64
    raise DeliveryError("unknown health service")


def inspect_unit(name: str, selected: dict) -> dict:
    uid, memory, pids, cpu, file_mib = service_profile(name)
    unit = "kf-" + name + ".service"
    link = service_install.SYSTEM_UNITS / unit
    if name in services.PROFILES:
        directory = service_install.known_link(link)
        expected = service_install.UNITS / (
            selected["release_id"] + "." + selected["configuration_id"]
        )
    elif name == "proxy":
        directory = proxy_install.known_link(link)
        expected = proxy_service.UNITS / (
            selected["release_id"] + "." + selected["configuration_id"]
        )
    elif name == "sandbox-factory":
        directory = factory_service.known_link(link)
        expected = factory_service.UNITS / (
            selected["release_id"] + "." + selected["configuration_id"]
        )
    else:
        directory = dependency_install.known_link(link)
        expected = dependency_install.ROOT / (
            selected["release_id"] + "." + selected["configuration_id"]
        )
    if directory != expected:
        raise DeliveryError("service snapshot is not selected")
    value = dict(
        line.split("=", 1)
        for line in service_install.systemctl(
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,MainPID,FragmentPath,ControlGroup,LogNamespace",
        ).splitlines()
        if "=" in line
    )
    if (
        value.get("LoadState") != "loaded"
        or value.get("ActiveState") != "active"
        or value.get("SubState") != "running"
        or value.get("FragmentPath") not in {str(link), str(directory / unit)}
        or value.get("ControlGroup") != "/kfonlinejudge.slice/" + unit
        or value.get("LogNamespace") != journal.NAMESPACE
    ):
        raise DeliveryError("selected service is not running")
    pid = int(value.get("MainPID", "0"))
    if not 1 < pid < 2**31:
        raise DeliveryError("running service PID differs")
    info = dict(
        line.split(":", 1)
        for line in Path(f"/proc/{pid}/status").read_text().splitlines()
        if ":" in line
    )
    if (
        info.get("Uid", "").split() != [str(uid)] * 4
        or info.get("Gid", "").split() != [str(uid)] * 4
    ):
        raise DeliveryError("effective service identity differs")
    if not set(info.get("Groups", "").split()) <= {str(uid)}:
        raise DeliveryError("unexpected service supplementary authority")
    if name not in {"sandbox-factory", "proxy"} and (
        info.get("NoNewPrivs", "").strip() != "1"
        or any(
            not info.get(k, "").strip() or int(info[k].strip(), 16) != 0
            for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
    ):
        raise DeliveryError("effective ordinary privilege boundary differs")
    if name == "proxy" and (
        info.get("NoNewPrivs", "").strip() != "1"
        or any(
            not info.get(k, "").strip() or int(info[k].strip(), 16) != proxy_service.CAPABILITY
            for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
    ):
        raise DeliveryError("proxy acquired excess or missing listener capability")
    if name == "sandbox-factory" and (
        info.get("NoNewPrivs", "").strip() != "0" or info.get("Seccomp", "").strip() != "0"
    ):
        raise DeliveryError("factory inherited incompatible hardening")
    if name in {"api", "outbox", "problem-import", "proxy"} and info.get("NSpid", "").split()[
        -1:
    ] != ["1"]:
        raise DeliveryError("Python service private PID namespace missing")
    group = Path("/sys/fs/cgroup" + value["ControlGroup"])
    actual = {
        k: (group / k).read_text().strip()
        for k in ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
    }
    if actual != {
        "memory.max": str(memory * 1024**2),
        "memory.swap.max": "0",
        "pids.max": str(pids),
        "cpu.max": f"{cpu * 1000} 100000",
    }:
        raise DeliveryError("effective service limits differ")
    if Path(f"/proc/{pid}/cgroup").read_text().strip() != "0::" + value["ControlGroup"]:
        raise DeliveryError("effective service cgroup differs")
    expected_file = str(file_mib * 1024**2)
    # /proc format has names with spaces; fixed relevant prefixes are unambiguous.
    limits = Path(f"/proc/{pid}/limits").read_text().splitlines()
    expected = {
        "Max core file size": "0",
        "Max file size": expected_file,
        "Max open files": "64" if name == "problem-import" else "256",
        "Max stack size": str(16 * 1024**2),
    }
    for prefix, bound in expected.items():
        rows = [line[len(prefix) :].split() for line in limits if line.startswith(prefix)]
        if len(rows) != 1 or rows[0][:2] != [bound, bound]:
            raise DeliveryError("effective service process limits differ")
    return {
        "active": True,
        "uid": uid,
        "limits": actual,
        "memory_current": int((group / "memory.current").read_text()),
        "pids_current": int((group / "pids.current").read_text()),
    }


def validate_role(role: str, observed: dict) -> None:
    expected = {
        "login": configuration.PROFILES[role],
        "database": database.DATABASE,
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
        "privileges": PRIVILEGES[role],
        "functions": function_privileges(role),
    }
    if canonical(observed) != canonical(expected):
        raise DeliveryError("database role authority differs")


async def roles(endpoints: dict) -> dict:
    import asyncpg

    result = {}
    for role, requirements in PRIVILEGES.items():
        connection = await asyncpg.connect(
            endpoints["database"][role],
            # Connections are restricted to numeric loopback or a private Unix
            # socket; PostgreSQL has TLS disabled. Avoid home-certificate lookup
            # inside ProtectHome and allow the bounded local SCRAM handshake.
            ssl=False,
            timeout=5,
            command_timeout=2,
            server_settings={
                "application_name": "kf-node-health",
                "default_transaction_read_only": "on",
                "statement_timeout": "2000",
                "lock_timeout": "500",
                "idle_in_transaction_session_timeout": "3000",
            },
        )
        try:
            row = await connection.fetchrow(
                "SELECT session_user login,current_database() database,rolcanlogin canlogin,"
                "rolsuper superuser,rolcreatedb createdb,rolcreaterole createrole,"
                "rolreplication replication,"
                "rolbypassrls bypassrls,rolconnlimit connection_limit,"
                "pg_has_role(session_user,$1,'MEMBER') member,"
                "pg_has_role(session_user,$1,'USAGE') inherited,"
                "pg_has_role(session_user,$1,'SET') set_role "
                "FROM pg_roles WHERE rolname=session_user",
                database.MEMBERSHIP[configuration.PROFILES[role]],
            )
            observed = dict(row)
            observed["privileges"] = {}
            for key in requirements:
                parts = key.split(":")
                observed["privileges"][key] = await connection.fetchval(
                    "SELECT has_table_privilege(current_user,$1,$2)"
                    if len(parts) == 2
                    else "SELECT has_column_privilege(current_user,$1,$2,$3)",
                    *parts,
                )
            observed["functions"] = {
                name: await connection.fetchval(
                    "SELECT has_function_privilege(current_user,$1,'EXECUTE')", name
                )
                for name in AUTHORITY_FUNCTIONS
            }
            validate_role(role, observed)
            result[role] = {"authority_verified": True}
        finally:
            await connection.close(timeout=1)
    return result


def http(url: str, *, token: str | None = None) -> dict:
    # Fixed numeric loopback paths only; inherited proxy settings and redirects
    # may not turn an operator query into a request to another service/host.
    if url not in {
        "http://127.0.0.1:8000/api/v1/health/live",
        "http://127.0.0.1:8100/health/ready",
        "http://127.0.0.1:8100/internal/operations/status",
        "http://127.0.0.1:8100/internal/operations/probe-id",
    }:
        raise DeliveryError("unknown health HTTP target")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token} if token else {}
    )
    with opener.open(request, timeout=5) as response:
        if response.status != 200:
            raise DeliveryError("health HTTP unavailable")
        result = operator_units.decode(response.read(32769))
    if not isinstance(result, dict):
        raise DeliveryError("health HTTP object required")
    return result


def validate_factory(value: dict, expected: str) -> None:
    fields(
        value,
        {
            "schema_version",
            "configuration_sha256",
            "supervisor_authorized",
            "execution_gate",
            "isolation_verified",
            "max_concurrent_runs",
            "active_helpers",
            "cleanup_complete",
            "remaining_requests",
            "remaining_lifetime_seconds",
        },
        "factory health",
    )
    if (
        value["schema_version"] != 1
        or type(value["schema_version"]) is not int
        or value["configuration_sha256"] != expected
        or value["supervisor_authorized"] is not True
        or value["execution_gate"] != "required_per_execution"
        or value["isolation_verified"] is not False
        or type(value["max_concurrent_runs"]) is not int
        or value["max_concurrent_runs"] != 1
        or type(value["active_helpers"]) is not int
        or not 0 <= value["active_helpers"] <= 1
        or type(value["cleanup_complete"]) is not bool
        or (value["cleanup_complete"] and value["active_helpers"])
        or type(value["remaining_requests"]) is not int
        or not 0 <= value["remaining_requests"] <= 4096
        or type(value["remaining_lifetime_seconds"]) is not int
        or not 0 <= value["remaining_lifetime_seconds"] <= 86400
    ):
        raise DeliveryError("factory authorization/binding status differs")


def validate_execution(value: dict, admission: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "admission",
            "configured_workers",
            "lease_seconds",
            "counters",
        },
        "execution snapshot",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["configured_workers"]) is not int
        or value["configured_workers"] != 1
        or value["lease_seconds"] != 120
        or type(value["lease_seconds"]) is not int
        or canonical(value["admission"]) != canonical(admission)
    ):
        raise DeliveryError("live coordinator admission differs")
    counters = fields(
        value["counters"],
        {"queued", "in_flight", "cleanup_unconfirmed", "database_clock_unix_ms"},
        "execution counters",
    )
    for name in ("queued", "in_flight", "cleanup_unconfirmed"):
        item = fields(counters[name], {"count_capped", "truncated"}, "bounded execution counter")
        if (
            type(item["count_capped"]) is not int
            or not 0 <= item["count_capped"] <= 128
            or type(item["truncated"]) is not bool
            or (item["truncated"] and item["count_capped"] != 128)
        ):
            raise DeliveryError("execution counter bound differs")
    if (
        type(counters["database_clock_unix_ms"]) is not int
        or counters["database_clock_unix_ms"] <= 0
    ):
        raise DeliveryError("execution counter clock unavailable")
    return counters


def admission_ready(root: dict, counters: dict, broker: dict, admission: dict) -> bool:
    reserve_seconds = (
        max(
            admission["legacy"]["max_job_wall_time_us"],
            admission["multi_runtime"]["max_job_wall_time_us"],
        )
        + 999999
    ) // 1000000 + 60
    return bool(
        root["cleanup_complete"]
        and root["active_helpers"] == 0
        and root["remaining_requests"] >= 257
        and root["remaining_lifetime_seconds"] >= reserve_seconds
        and counters["in_flight"] == {"count_capped": 0, "truncated": False}
        and counters["cleanup_unconfirmed"] == {"count_capped": 0, "truncated": False}
        and broker["consumers"] == 1
    )


async def notification_probe(url: str, identifier: str) -> None:
    import aio_pika
    from pamqp.commands import Basic

    for name in ("aio_pika", "aiormq", "pamqp"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    connection = await aio_pika.connect(url, timeout=2, heartbeat=30)
    try:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        await channel.declare_queue(dependencies.QUEUE, passive=True, timeout=2)
        # A synthetically absent evaluation notification cannot create a grade or
        # execute a program: Coordinator must find and lease an existing row.
        body = canonical(
            {
                "schema_version": 1,
                "kind": "judge.evaluation",
                "evaluation_id": identifier,
            }
        ).strip()
        reply = await channel.default_exchange.publish(
            aio_pika.Message(
                body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=dependencies.QUEUE,
            mandatory=True,
            timeout=2,
        )
        if not isinstance(reply, Basic.Ack):
            raise DeliveryError("broker delivery not confirmed")
    finally:
        await asyncio.wait_for(connection.close(), timeout=1)


def observe(*, probe: bool) -> dict:
    operator_units.guarded("node-health")
    selected, paths, payload = binding()
    result = {
        "schema_version": 1,
        "source_sha": read_json(payload.parent / "manifest.json")["source_sha"],
        "configuration_id": selected["configuration_id"],
        "ready_for_admission": False,
        "service_preconditions_met": False,
        "isolation_verified": False,
        "components": {},
        "errors": [],
    }
    components, errors = result["components"], result["errors"]
    helper_failures = []

    def remember_helper(error):
        if isinstance(error, operator_units.OperatorExecutionError):
            helper_failures.extend(error.failures[: max(0, 2 - len(helper_failures))])
            result["operator_failures"] = helper_failures

    try:
        components["journal"] = journal.check_ready()
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append("bounded_journal_unavailable")
    try:
        proxy_install.check_binding(selected["release_id"], selected["configuration_id"])
        components["https_entry"] = proxy_health.observe(selected)
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append("https_entry_unavailable")
    services_ready = True
    for name in [
        *services.PROFILES,
        "postgres",
        "rabbitmq",
        "sandbox-factory",
        "proxy",
    ]:
        try:
            components[name] = inspect_unit(name, selected)
        except (OSError, ValueError):
            services_ready = False
            errors.append(name + "_unavailable")
    if not services_ready:
        # Missing or invalid service profiles already close admission. Do not
        # spend the outer helper deadline starting queries of those dependencies.
        if install.selection(check_configuration=False) != selected:
            errors.append("selected_binding_changed")
        return result
    endpoints = dependency_install.installed_endpoints(selected)
    try:
        components["db_authority"] = asyncio.run(roles(endpoints))
    except Exception:
        errors.append("db_authority_unavailable")
    try:
        live = http("http://127.0.0.1:8000/api/v1/health/live")
        if live.get("status") != "ok" or live.get("service") != "platform":
            raise DeliveryError("API live shape differs")
        components["api_live"] = True
    except (OSError, ValueError):
        errors.append("api_live_unavailable")
    try:
        coordinator = read_json(paths["coordinator.json"], 32768)
        counters = validate_execution(
            http(
                "http://127.0.0.1:8100/internal/operations/status",
                token=coordinator["platform_token"],
            ),
            coordinator["admission"],
        )
        components["execution"] = counters
    except (OSError, ValueError):
        errors.append("execution_owner_unavailable")
    with _status_helpers(payload) as pending:
        try:
            components["broker"] = pending["broker-health"].result()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            remember_helper(error)
            errors.append("broker_metadata_unavailable")
        try:
            state = pending["outbox-health"].result()
            validate_outbox(state)
            components["outbox_pipeline"] = state
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            remember_helper(error)
            errors.append("outbox_pipeline_unavailable")
        try:
            state = pending["import-health"].result()
            fields(
                state,
                {
                    "schema_version",
                    "worker_ready",
                    "reserved_bytes",
                    "uploaded_capped",
                    "validating_capped",
                    "truncated",
                },
                "import status",
            )
            if (
                type(state["schema_version"]) is not int
                or state["schema_version"] != 1
                or state["worker_ready"] is not True
                or state["truncated"] is not False
            ):
                raise DeliveryError("import heartbeat or counter unavailable")
            for key, maximum in (
                ("reserved_bytes", 268435456),
                ("uploaded_capped", 128),
                ("validating_capped", 1),
            ):
                if type(state[key]) is not int or not 0 <= state[key] <= maximum:
                    raise DeliveryError("import status bound differs")
            components["import_pipeline"] = state
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            remember_helper(error)
            errors.append("import_pipeline_unavailable")
        try:
            status = pending["factory-health"].result()
            validate_factory(status, digest(paths["runtime.json"]))
            components["factory_status"] = status
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            remember_helper(error)
            errors.append("factory_status_unavailable")
    if probe:
        try:
            # Blocking module initialization belongs to the fixed 300-second
            # helper budget. Load the packaged client before starting the
            # cancellable six-second network operation.
            import_module("aio_pika")
            import_module("pamqp.commands")
            value = fields(
                http(
                    "http://127.0.0.1:8100/internal/operations/probe-id",
                    token=coordinator["platform_token"],
                ),
                {"schema_version", "evaluation_id", "absent"},
                "notification probe identity",
            )
            identifier = UUID(value["evaluation_id"])
            if (
                type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or value["absent"] is not True
                or identifier.int == 0
                or str(identifier) != value["evaluation_id"]
            ):
                raise DeliveryError("notification probe authority unavailable")
            asyncio.run(
                asyncio.wait_for(
                    notification_probe(endpoints["amqp"]["outbox"], str(identifier)),
                    timeout=6,
                )
            )
            components["notification_delivery_confirmed"] = True
        except Exception:
            errors.append("notification_delivery_unconfirmed")
    if install.selection(check_configuration=False) != selected:
        errors.append("selected_binding_changed")
    if not errors:
        root = components["factory_status"]
        counters = components["execution"]
        result["service_preconditions_met"] = admission_ready(
            root, counters, components["broker"], coordinator["admission"]
        )
        record = availability.read()
        result["ready_for_admission"] = bool(
            result["service_preconditions_met"]
            and record is not None
            and record.release_id == selected["release_id"]
            and record.configuration_id == selected["configuration_id"]
            and record.state == "open"
        )
    return result


def validate_outbox(state: dict) -> None:
    fields(
        state,
        {"schema_version", "pending_capped", "locked_capped", "truncated"},
        "outbox status",
    )
    if (
        type(state["schema_version"]) is not int
        or state["schema_version"] != 1
        or type(state["truncated"]) is not bool
    ):
        raise DeliveryError("outbox projection binding differs")
    for name in ("pending_capped", "locked_capped"):
        if type(state[name]) is not int or not 0 <= state[name] <= 128:
            raise DeliveryError("outbox projection bound differs")
    if state["locked_capped"] > state["pending_capped"] or (
        state["truncated"] and state["pending_capped"] != 128
    ):
        raise DeliveryError("outbox projection counters inconsistent")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded installed service readiness")
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--probe-notification", action="store_true")
    args = parser.parse_args()
    try:
        if args.inside:
            result = observe(probe=args.probe_notification)
        else:
            if os.getuid() != 0 or os.geteuid() != 0:
                raise DeliveryError("root operator required")
            selected = install.selection(check_configuration=False)
            result = operator_units.run(
                "node-health",
                install.RELEASES / selected["release_id"] / "payload",
                notification_probe=args.probe_notification,
            )
        print(json.dumps(result, sort_keys=True))
        if not args.inside and not result["ready_for_admission"]:
            raise SystemExit(2)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Node health refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n"
            + operator_units.diagnostic_suffix(error),
        )


if __name__ == "__main__":
    main()
