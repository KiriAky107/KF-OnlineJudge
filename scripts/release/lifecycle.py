"""Fixed Root maintenance orchestration; owners drain work, never forged terminal state."""

import argparse
import fcntl
import json
import os
import re
import secrets
import stat
import subprocess
import time
import urllib.error
from pathlib import Path
from uuid import UUID

from . import (
    availability,
    configuration,
    dependency_install,
    factory_service,
    install,
    node_health,
    operation_proof,
    operator_units,
    preflight,
    proxy_health,
    proxy_install,
    proxy_service,
    service_install,
    services,
)
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256

STATE = Path("/var/lib/kf-onlinejudge/operations")
LAST = STATE / "maintenance.last.json"
APPS = (
    "proxy",
    "api",
    "outbox",
    "worker",
    "problem-import",
    "coordinator",
    "sandbox-factory",
)
DEPENDENCIES = ("postgres", "rabbitmq")
START_ORDER = (
    "sandbox-factory",
    "coordinator",
    "api",
    "outbox",
    "problem-import",
    "worker",
    "proxy",
)
# PG and MQ each allow one CPU during full-bundle startup checks. Start Root
# after them. Wait for Python initialization before dispatching the next worker
# so their imports and first database handshakes do not compete for that budget.
START_GROUPS = (
    DEPENDENCIES,
    ("sandbox-factory",),
    ("coordinator", "api"),
    ("outbox",),
    ("problem-import",),
    ("worker",),
    ("proxy",),
)
DRAIN_SECONDS = 360


def validate_record(value: dict) -> dict:
    required = {
        "schema_version",
        "source_sha",
        "configuration_id",
        "action",
        "phase",
        "success",
        "events",
        "root_cleanup_confirmed",
        "boot_id",
    }
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or not set(value) - required
        <= {
            "drain_projection",
            "failure_code",
            "readiness_rechecks",
            "readiness_recheck_reason",
        }
    ):
        raise DeliveryError("unknown maintenance receipt fields")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["source_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["source_sha"])
        or not isinstance(value["configuration_id"], str)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", value["configuration_id"])
        or not isinstance(value["action"], str)
        or value["action"] not in {"start", "resume", "drain", "stop", "restart"}
        or not isinstance(value["phase"], str)
        or value["phase"]
        not in {
            "prepared",
            "dispatching_start",
            "dispatching_stop",
            "start_confirmed",
            "stop_confirmed",
            "admission_closed",
            "root_stop_proven",
            "quiescent",
            "accepting",
            "complete",
            "failed",
        }
        or type(value["success"]) is not bool
        or type(value["root_cleanup_confirmed"]) is not bool
        or not isinstance(value["events"], list)
        or len(value["events"]) > 32
        or "failure_code" in value
        and value["failure_code"] != "maintenance_unconfirmed"
    ):
        raise DeliveryError("unknown maintenance receipt binding")
    if not isinstance(value["boot_id"], str):
        raise DeliveryError("maintenance boot identity differs")
    identifier = UUID(value["boot_id"])
    if identifier.int == 0 or str(identifier) != value["boot_id"]:
        raise DeliveryError("maintenance boot identity differs")
    for event in value["events"]:
        fields(event, {"unit", "operation", "confirmed"}, "maintenance event")
        if (
            not isinstance(event["unit"], str)
            or event["unit"] not in {*APPS, *DEPENDENCIES}
            or not isinstance(event["operation"], str)
            or event["operation"] not in {"start", "stop"}
            or type(event["confirmed"]) is not bool
        ):
            raise DeliveryError("unknown maintenance event authority")
    if "readiness_rechecks" in value and (
        type(value["readiness_rechecks"]) is not int
        or value["readiness_rechecks"] != 1
        or value["action"] not in {"start", "resume", "restart"}
    ):
        raise DeliveryError("unknown maintenance readiness recheck")
    if "readiness_recheck_reason" in value and (
        value.get("readiness_rechecks") != 1
        or (
            value["readiness_recheck_reason"] != ["broker_query_deadline"]
            and not operator_units.activity_blockers(value["readiness_recheck_reason"])
        )
    ):
        raise DeliveryError("unknown maintenance readiness recheck reason")
    if "drain_projection" in value and not projection_empty(value["drain_projection"]):
        raise DeliveryError("maintenance quiescence proof differs")
    return value


def projection_empty(snapshot: dict) -> bool:
    fields(
        snapshot,
        {"execution", "outbox", "imports", "broker", "factory"},
        "drain projection",
    )
    execution = fields(
        snapshot["execution"],
        {"queued", "in_flight", "cleanup_unconfirmed", "database_clock_unix_ms"},
        "execution drain projection",
    )
    if (
        type(execution["database_clock_unix_ms"]) is not int
        or execution["database_clock_unix_ms"] <= 0
    ):
        raise DeliveryError("execution drain clock unavailable")
    for name in ("queued", "in_flight", "cleanup_unconfirmed"):
        item = fields(execution[name], {"count_capped", "truncated"}, "execution drain counter")
        if (
            type(item["count_capped"]) is not int
            or not 0 <= item["count_capped"] <= 128
            or type(item["truncated"]) is not bool
        ):
            raise DeliveryError("execution drain counter unavailable")
        if execution[name] != {"count_capped": 0, "truncated": False}:
            return False
    node_health.validate_outbox(snapshot["outbox"])
    outbox = snapshot["outbox"]
    imports = snapshot["imports"]
    fields(
        imports,
        {
            "schema_version",
            "worker_ready",
            "reserved_bytes",
            "uploaded_capped",
            "validating_capped",
            "truncated",
        },
        "import drain projection",
    )
    if (
        type(imports["schema_version"]) is not int
        or imports["schema_version"] != 1
        or type(imports["worker_ready"]) is not bool
        or type(imports["truncated"]) is not bool
        or any(
            type(imports[k]) is not int or not 0 <= imports[k] <= maximum
            for k, maximum in (
                ("reserved_bytes", 268435456),
                ("uploaded_capped", 128),
                ("validating_capped", 128),
            )
        )
    ):
        raise DeliveryError("import drain counters unavailable")
    broker, root = snapshot["broker"], snapshot["factory"]
    if not isinstance(root, dict):
        raise DeliveryError("factory drain proof unavailable")
    node_health.validate_factory(root, sha256(root.get("configuration_sha256")))
    fields(
        broker,
        {
            "schema_version",
            "metadata_verified",
            "messages_ready",
            "messages_unacknowledged",
            "message_bytes",
            "consumers",
        },
        "broker drain projection",
    )
    if (
        type(broker["schema_version"]) is not int
        or broker["schema_version"] != 1
        or broker["metadata_verified"] is not True
        or any(
            type(broker[k]) is not int or not 0 <= broker[k] <= maximum
            for k, maximum in (
                ("messages_ready", 64),
                ("messages_unacknowledged", 1),
                ("message_bytes", 32768 + 4096),
                ("consumers", 1),
            )
        )
    ):
        raise DeliveryError("broker drain projection unavailable")
    if (
        type(root["active_helpers"]) is not int
        or not 0 <= root["active_helpers"] <= 1
        or type(root["cleanup_complete"]) is not bool
    ):
        raise DeliveryError("factory drain proof unavailable")
    return bool(
        not outbox["truncated"]
        and outbox["pending_capped"] == outbox["locked_capped"] == 0
        and imports["truncated"] is False
        and imports["uploaded_capped"] == imports["validating_capped"] == 0
        and broker["messages_ready"] == broker["messages_unacknowledged"] == 0
        and root["active_helpers"] == 0
        and root["cleanup_complete"] is True
    )


def _selected_unit_target(selected: dict, name: str) -> tuple[Path, Path]:
    if name not in {*APPS, *DEPENDENCIES}:
        raise DeliveryError("unknown lifecycle target")
    path = service_install.SYSTEM_UNITS / ("kf-" + name + ".service")
    if name in services.PROFILES:
        directory = service_install.known_link(path)
        base = service_install.UNITS
    elif name == "proxy":
        directory = proxy_install.known_link(path)
        base = proxy_service.UNITS
    elif name == "sandbox-factory":
        directory = factory_service.known_link(path)
        base = factory_service.UNITS
    else:
        directory = dependency_install.known_link(path)
        base = dependency_install.ROOT
    if directory != base / (selected["release_id"] + "." + selected["configuration_id"]):
        raise DeliveryError("lifecycle target is not the selected snapshot")
    return path, directory


def selected_unit(selected: dict, name: str) -> dict:
    path, directory = _selected_unit_target(selected, name)
    values = dict(
        line.split("=", 1)
        for line in service_install.systemctl(
            "show",
            path.name,
            "--property=LoadState,ActiveState,SubState,MainPID,FragmentPath",
        ).splitlines()
        if "=" in line
    )
    if values.get("LoadState") != "loaded" or values.get("FragmentPath") not in {
        str(path),
        str(directory / path.name),
    }:
        raise DeliveryError("lifecycle manager target differs")
    return values


def selected_units(selected: dict, names: tuple[str, ...]) -> dict[str, dict]:
    """Read one bounded manager snapshot, retaining each selected unit binding."""
    if (
        type(names) is not tuple
        or not 1 <= len(names) <= len(APPS) + len(DEPENDENCIES)
        or any(type(name) is not str or name not in (*APPS, *DEPENDENCIES) for name in names)
        or len(set(names)) != len(names)
    ):
        raise DeliveryError("unknown or duplicate lifecycle targets")
    if len(names) == 1:
        return {names[0]: selected_unit(selected, names[0])}
    targets = {name: _selected_unit_target(selected, name) for name in names}
    identifiers = {path.name: name for name, (path, _) in targets.items()}
    contents = service_install.systemctl(
        "show",
        *identifiers,
        "--property=Id,LoadState,ActiveState,SubState,MainPID,FragmentPath",
    )
    observed = {}
    for block in contents.strip().split("\n\n"):
        values = {}
        for line in block.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise DeliveryError("lifecycle manager snapshot fields differ")
            values[key] = value
        if set(values) != {
            "Id",
            "LoadState",
            "ActiveState",
            "SubState",
            "MainPID",
            "FragmentPath",
        }:
            raise DeliveryError("lifecycle manager snapshot fields differ")
        identifier = values.pop("Id")
        if identifier not in identifiers or identifiers[identifier] in observed:
            raise DeliveryError("lifecycle manager snapshot identities differ")
        name = identifiers[identifier]
        path, directory = targets[name]
        if values["LoadState"] != "loaded" or values["FragmentPath"] not in {
            str(path),
            str(directory / path.name),
        }:
            raise DeliveryError("lifecycle manager target differs")
        observed[name] = values
    if set(observed) != set(names):
        raise DeliveryError("lifecycle manager snapshot identities differ")
    return observed


def close_failed_admission(selected: dict) -> None:
    try:
        availability.write(selected, "draining")
        return
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    # A full/read-only availability filesystem must not leave a successful
    # earlier publication accepting mutations. Stop only the verified API
    # target without depending on another receipt write. Owners retain work
    # and capacity; this is not a factory cleanup or terminal-state proof.
    selected_unit(selected, "api")
    service_install.systemctl("--no-block", "stop", "kf-api.service")
    deadline = time.monotonic() + 50
    while True:
        values = selected_unit(selected, "api")
        if values["ActiveState"] == "inactive" and values["MainPID"] == "0":
            Maintenance.empty_cgroup("api")
            return
        if time.monotonic() >= deadline or values["ActiveState"] == "failed":
            raise DeliveryError("admission closure/API stop unconfirmed; operator required")
        time.sleep(0.5)


class Maintenance:
    def __init__(self, selected: dict, paths: dict, payload: Path, action: str):
        self.selected, self.paths, self.payload = selected, paths, payload
        self.coordinator = read_json(paths["coordinator.json"], 32768)
        self.record = {
            "schema_version": 1,
            "source_sha": read_json(payload.parent / "manifest.json")["source_sha"],
            "configuration_id": selected["configuration_id"],
            "action": action,
            "phase": "prepared",
            "success": False,
            "events": [],
            "root_cleanup_confirmed": False,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }

    def save(self, phase: str) -> None:
        self.record["phase"] = phase
        if len(self.record["events"]) > 32:
            raise DeliveryError("maintenance event bound")
        contents = canonical(validate_record(self.record))
        if len(contents) > 32768:
            raise DeliveryError("maintenance receipt bound")
        if LAST.exists() or LAST.is_symlink():
            preflight.protected(LAST)
            configuration.private(LAST)
            validate_record(read_json(LAST, 32768))
        temporary = STATE / (".maintenance-" + secrets.token_hex(8))
        created = None
        try:
            install.exclusive_file(temporary, contents, 0o600)
            info = temporary.stat()
            created = (info.st_dev, info.st_ino)
            os.replace(temporary, LAST)
            install.sync_directory(STATE)
        finally:
            if created is not None and temporary.exists():
                info = temporary.lstat()
                if (info.st_dev, info.st_ino) == created:
                    temporary.unlink()

    def unit(self, name: str) -> dict:
        return selected_unit(self.selected, name)

    def units(self, names: tuple[str, ...]) -> dict[str, dict]:
        if len(names) == 1:
            return {names[0]: self.unit(names[0])}
        return selected_units(self.selected, names)

    @staticmethod
    def empty_cgroup(name: str) -> None:
        directory = Path("/sys/fs/cgroup/kfonlinejudge.slice/kf-" + name + ".service")
        try:
            directory.lstat()
        except FileNotFoundError:
            return
        if directory.is_symlink() or not directory.is_dir():
            raise DeliveryError("unknown residual service cgroup")
        pending = [directory]
        seen = 0
        while pending:
            group = pending.pop()
            seen += 1
            if seen > 64:
                raise DeliveryError("service cgroup tree exceeds bound")
            processes = group / "cgroup.procs"
            with processes.open() as source:
                members = source.read(4097)
            if len(members) > 4096 or members.strip():
                raise DeliveryError("service processes remain; no capacity release")
            for child in group.iterdir():
                if child.is_symlink():
                    raise DeliveryError("unknown cgroup entry")
                if child.is_dir():
                    pending.append(child)

    def change(self, name: str, operation: str) -> None:
        if name == "sandbox-factory" and operation == "start":
            self.record["root_cleanup_confirmed"] = False
        values = self.unit(name)
        if operation == "start" and values["ActiveState"] == "active":
            node_health.inspect_unit(name, self.selected)
            if name == "proxy":
                self._wait_for_proxy(time.monotonic() + 130)
            elif name == "api":
                self._wait_for_api(time.monotonic() + 130)
            return
        if operation == "stop" and values["ActiveState"] == "inactive" and values["MainPID"] == "0":
            self.empty_cgroup(name)
            return
        if operation not in {"start", "stop"}:
            raise DeliveryError("unexpected lifecycle operation")
        self.record["events"].append({"unit": name, "operation": operation, "confirmed": False})
        self.save("dispatching_" + operation)
        service_install.systemctl("--no-block", operation, "kf-" + name + ".service")
        # Root preparation validates the protected image registry before exec.
        # Its declared unit timeout also applies here; the enclosing maintenance
        # helper still enforces its independent overall runtime ceiling.
        start_seconds = factory_service.START_TIMEOUT_SECONDS if name == "sandbox-factory" else 120
        deadline = time.monotonic() + (start_seconds + 10 if operation == "start" else 50)
        while True:
            if install.selection(check_configuration=False) != self.selected:
                raise DeliveryError("selected binding changed during maintenance")
            values = self.unit(name)
            if (
                operation == "start"
                and values["ActiveState"] == "active"
                and values["SubState"] == "running"
            ):
                node_health.inspect_unit(name, self.selected)
                if name == "proxy":
                    self._wait_for_proxy(deadline)
                elif name == "api":
                    self._wait_for_api(deadline)
                break
            if (
                operation == "stop"
                and values["ActiveState"] == "inactive"
                and values["MainPID"] == "0"
            ):
                self.empty_cgroup(name)
                break
            if time.monotonic() >= deadline or values["ActiveState"] == "failed":
                raise DeliveryError("bounded lifecycle operation unconfirmed")
            time.sleep(0.5)
        self.record["events"][-1]["confirmed"] = True
        self.save(operation + "_confirmed")

    def _wait_for_api(self, deadline: float) -> None:
        """Confirm the fixed live endpoint within the existing start deadline."""
        while True:
            if time.monotonic() >= deadline:
                raise DeliveryError("bounded API startup unconfirmed")
            if install.selection(check_configuration=False) != self.selected:
                raise DeliveryError("selected binding changed during maintenance")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeliveryError("bounded API startup unconfirmed")
            try:
                ready = node_health.http(
                    "http://127.0.0.1:8000/api/v1/health/live",
                    timeout=min(5.0, remaining),
                )
            except urllib.error.HTTPError as error:
                raise DeliveryError("API startup HTTP response differs") from error
            except OSError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeliveryError("bounded API startup unconfirmed") from error
                time.sleep(min(0.5, remaining))
                continue
            if time.monotonic() >= deadline:
                raise DeliveryError("bounded API startup unconfirmed")
            if install.selection(check_configuration=False) != self.selected:
                raise DeliveryError("selected binding changed during maintenance")
            if ready.get("status") != "ok" or ready.get("service") != "platform":
                raise DeliveryError("API startup liveness response differs")
            return

    def _wait_for_proxy(self, deadline: float) -> None:
        """Type=exec is process startup; confirm actual HTTPS before admission."""
        while True:
            if time.monotonic() >= deadline:
                raise DeliveryError("bounded HTTPS startup unconfirmed")
            if install.selection(check_configuration=False) != self.selected:
                raise DeliveryError("selected binding changed during maintenance")
            # Invalid selected TLS/configuration is fatal, never a retry reason.
            proxy_install.check_binding(
                self.selected["release_id"], self.selected["configuration_id"]
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeliveryError("bounded HTTPS startup unconfirmed")
            try:
                ready = proxy_health.observe(self.selected, timeout=min(5.0, remaining))
            except proxy_health.HTTPSNotReady as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeliveryError("bounded HTTPS startup unconfirmed") from error
                time.sleep(min(0.5, remaining))
                continue
            if time.monotonic() >= deadline:
                raise DeliveryError("bounded HTTPS startup unconfirmed")
            if install.selection(check_configuration=False) != self.selected:
                raise DeliveryError("selected binding changed during maintenance")
            if ready != {
                "reachable": True,
                "pinned_certificate_and_hostname_verified": True,
                "public_browser_trust_verified": False,
            }:
                raise DeliveryError("HTTPS startup proof differs")
            return

    @staticmethod
    def factory_not_listening(error: operator_units.OperatorExecutionError) -> bool:
        """Only the fixed CLI's typed connect failure permits another observation."""
        expected = operator_units.failure_record(
            "factory-health",
            "exit",
            subprocess.CompletedProcess(
                [],
                2,
                stdout=b"",
                stderr=b'{"details":null,"error":"factory_health_not_listening"}\n',
            ),
            DeliveryError("operator helper unavailable"),
        )
        try:
            frame = operator_units.DIAGNOSTIC_PREFIX + json.dumps(
                {"schema_version": 1, "failures": error.failures}
            )
        except (TypeError, ValueError):
            return False
        return operator_units.diagnostic_chain(frame.encode()) == [expected]

    def _wait_for_factory(self, deadline: float, main_pid: str) -> None:
        # Type=exec confirms execve, while Root still validates every image and
        # recovers its journals before binding the authenticated status socket.
        # Keep the original unit deadline and outer900s maintenance ceiling.
        # This observes the same process; it never restarts Root or opens admission.
        if (
            type(main_pid) is not str
            or not re.fullmatch(r"[1-9][0-9]{0,9}", main_pid)
            or int(main_pid) <= 1
        ):
            raise DeliveryError("factory startup process identity required")
        identity = {"ActiveState": "active", "SubState": "running", "MainPID": main_pid}
        for _ in range(120):
            if time.monotonic() >= deadline:
                raise DeliveryError("bounded factory readiness unconfirmed")
            if install.selection(check_configuration=False) != self.selected:
                raise DeliveryError("selected binding changed during maintenance")
            gate = availability.read()
            if (
                gate is None
                or gate.state != "draining"
                or gate.release_id != self.selected["release_id"]
                or gate.configuration_id != self.selected["configuration_id"]
            ):
                raise DeliveryError("factory readiness requires selected closed admission")
            values = self.unit("sandbox-factory")
            if any(values[key] != value for key, value in identity.items()):
                raise DeliveryError("factory process changed during startup")
            node_health.inspect_unit("sandbox-factory", self.selected)
            try:
                status = operator_units.run("factory-health", self.payload)
            except operator_units.OperatorExecutionError as error:
                if not self.factory_not_listening(error):
                    raise
            else:
                current = self.unit("sandbox-factory")
                if any(current[key] != value for key, value in identity.items()):
                    raise DeliveryError("factory process changed during startup")
                if time.monotonic() >= deadline:
                    raise DeliveryError("bounded factory readiness unconfirmed")
                node_health.validate_factory(status, digest(self.paths["runtime.json"]))
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeliveryError("bounded factory readiness unconfirmed")
            time.sleep(min(1.0, remaining))
        raise DeliveryError("bounded factory readiness unconfirmed")

    def start_group(self, names: tuple[str, ...]) -> None:
        """Dispatch one fixed dependency stage; confirm each real unit separately."""
        if names not in START_GROUPS:
            raise DeliveryError("unknown startup dependency group")
        if "sandbox-factory" in names:
            self.record["root_cleanup_confirmed"] = False
        if install.selection(check_configuration=False) != self.selected:
            raise DeliveryError("selected binding changed during maintenance")
        # Validate every target before dispatching any member of this stage.
        inactive = []
        initial = self.units(names)
        for name in names:
            values = initial[name]
            if values["ActiveState"] == "active":
                node_health.inspect_unit(name, self.selected)
                if name == "proxy":
                    self._wait_for_proxy(time.monotonic() + 130)
                elif name == "api":
                    self._wait_for_api(time.monotonic() + 130)
            else:
                inactive.append(name)
        pending = {}
        for name in inactive:
            if any(time.monotonic() >= deadline for _, deadline in pending.values()):
                raise DeliveryError("bounded lifecycle operation unconfirmed")
            if install.selection(check_configuration=False) != self.selected:
                raise DeliveryError("selected binding changed during maintenance")
            event = {"unit": name, "operation": "start", "confirmed": False}
            self.record["events"].append(event)
            self.save("dispatching_start")
            service_install.systemctl("--no-block", "start", "kf-" + name + ".service")
            seconds = factory_service.START_TIMEOUT_SECONDS if name == "sandbox-factory" else 120
            # A later dispatch or wait cannot reset an earlier unit's deadline.
            pending[name] = (event, time.monotonic() + seconds + 10)
        while pending:
            if install.selection(check_configuration=False) != self.selected:
                raise DeliveryError("selected binding changed during maintenance")
            current = self.units(tuple(pending))
            for name, (event, deadline) in list(pending.items()):
                if install.selection(check_configuration=False) != self.selected:
                    raise DeliveryError("selected binding changed during maintenance")
                values = current[name]
                if time.monotonic() >= deadline or values["ActiveState"] == "failed":
                    raise DeliveryError("bounded lifecycle operation unconfirmed")
                if values["ActiveState"] == "active" and values["SubState"] == "running":
                    node_health.inspect_unit(name, self.selected)
                    if name == "proxy":
                        self._wait_for_proxy(deadline)
                    elif name == "api":
                        self._wait_for_api(deadline)
                    elif name == "sandbox-factory":
                        self._wait_for_factory(deadline, values["MainPID"])
                    event["confirmed"] = True
                    self.save("start_confirmed")
                    del pending[name]
            if pending:
                time.sleep(0.5)

    def snapshot(self) -> dict:
        owner = node_health.http(
            "http://127.0.0.1:8100/internal/operations/status",
            token=self.coordinator["platform_token"],
        )
        counters = node_health.validate_execution(owner, self.coordinator["admission"])
        result = {
            "execution": counters,
            "outbox": operator_units.run("outbox-health", self.payload),
            "imports": operator_units.run("import-health", self.payload),
            "broker": operator_units.run("broker-health", self.payload),
            "factory": operator_units.run("factory-health", self.payload),
        }
        node_health.validate_factory(result["factory"], digest(self.paths["runtime.json"]))
        return result

    def root_stopped(self) -> None:
        values = self.unit("sandbox-factory")
        if values["ActiveState"] != "inactive" or values["MainPID"] != "0":
            raise DeliveryError("Root factory stop unconfirmed")
        self.empty_cgroup("sandbox-factory")
        for name in ("tasks", "identities", "captures"):
            path = Path("/var/lib/kf-judge") / name
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            preflight.protected(path, directory=True)
            if next(path.iterdir(), None) is not None:
                raise DeliveryError("task/identity/capture resources remain")
        delegated = Path("/sys/fs/cgroup/kfonlinejudge.slice/kf-judge-tasks")
        if delegated.exists():
            preflight.protected(delegated, directory=True)
            if (delegated / "cgroup.procs").read_text().strip() or any(
                p.is_dir() or p.is_symlink() for p in delegated.iterdir()
            ):
                raise DeliveryError("delegated task resources remain")
        socket = Path("/var/lib/kf-judge/factory/control.sock")
        try:
            socket.lstat()
        except FileNotFoundError:
            pass
        else:
            raise DeliveryError("factory socket remains; explicit recovery required")
        self.record["root_cleanup_confirmed"] = True
        self.save("root_stop_proven")

    def drain(self) -> None:
        availability.write(self.selected, "draining")
        self.save("admission_closed")
        # API remains available for accepted artifact/source reads, cancellation
        # and receipt-bound uploads. No new submission/import/write is admitted.
        deadline = time.monotonic() + DRAIN_SECONDS
        while True:
            snapshot = self.snapshot()
            if projection_empty(snapshot):
                self.record["drain_projection"] = snapshot
                break
            if time.monotonic() >= deadline:
                raise DeliveryError("drain deadline expired; owners retain unfinished work")
            time.sleep(2)
        # Every process/child and final Root resource proof remains required.
        # No row, notification, identity lease or replay journal is removed here.
        for name in ("proxy", "api", "outbox", "problem-import", "worker"):
            self.change(name, "stop")
        final = self.snapshot()
        if not projection_empty(final):
            raise DeliveryError("work changed during final stop; quiescence unconfirmed")
        self.record["drain_projection"] = final
        self.change("coordinator", "stop")
        self.change("sandbox-factory", "stop")
        self.root_stopped()
        availability.write(self.selected, "quiescent")
        self.save("quiescent")

    def reuse_drained(self, previous: dict | None) -> bool:
        """Continue a confirmed drain without querying owners already stopped."""
        gate = availability.read()
        if gate is None or gate.state != "quiescent":
            return False
        if previous is None:
            raise DeliveryError("quiescent node has no confirmed drain receipt")
        validate_record(previous)
        if (
            gate.release_id != self.selected["release_id"]
            or gate.configuration_id != self.selected["configuration_id"]
            or any(
                previous[key] != self.record[key]
                for key in ("source_sha", "configuration_id", "boot_id")
            )
            or previous["action"] not in {"drain", "stop"}
            or previous["phase"] != "complete"
            or previous["success"] is not True
            or previous["root_cleanup_confirmed"] is not True
            or any(event["confirmed"] is not True for event in previous["events"])
            or "drain_projection" not in previous
            or not projection_empty(previous["drain_projection"])
        ):
            raise DeliveryError("prior drain is not this boot's selected owner-confirmed cut")
        for name in APPS:
            values = self.unit(name)
            if values["ActiveState"] != "inactive" or values["MainPID"] != "0":
                raise DeliveryError("prior drain has a running application service")
            self.empty_cgroup(name)
        self.record["drain_projection"] = previous["drain_projection"]
        self.root_stopped()
        return True

    def start(self, *, resume: bool) -> None:
        from .recovery_database import admission_guard

        with admission_guard():
            if not resume:
                # Preserve the ordinary refusal of start on an active node,
                # before publishing a proof or changing its admission state.
                self.require_fresh_start()
            try:
                availability.write(self.selected, "draining")
                with operation_proof.publish(self.selected, self.payload.parent):
                    self.start_locked(resume=resume)
            except (OSError, ValueError, subprocess.SubprocessError):
                # A final proof/cleanup failure can occur after readiness opened
                # admission. Close it before the caller records this failure.
                self.close_failed_admission()
                raise

    def require_fresh_start(self) -> None:
        current = self.units(APPS)
        for name in APPS:
            value = current[name]
            if value["ActiveState"] != "inactive" or value["MainPID"] != "0":
                raise DeliveryError("fresh start requires stopped application units")
            self.empty_cgroup(name)
        self.root_stopped()

    def broker_query_timeout_only(self, ready: dict) -> bool:
        """Recognize only one fully cleaned-up helper's fixed query timeout."""
        if (
            not isinstance(ready, dict)
            or type(ready.get("schema_version")) is not int
            or ready["schema_version"] != 1
            or ready.get("source_sha") != self.record["source_sha"]
            or ready.get("configuration_id") != self.selected["configuration_id"]
            or ready.get("service_preconditions_met") is not False
            or ready.get("ready_for_admission") is not False
            or ready.get("isolation_verified") is not False
            or ready.get("errors") != ["broker_metadata_unavailable"]
        ):
            return False
        expected = operator_units.failure_record(
            "broker-health",
            "exit",
            subprocess.CompletedProcess(
                [],
                1,
                stdout=b"",
                stderr=b"Broker health refused: broker snapshot query exceeded deadline\n",
            ),
            DeliveryError("operator helper unavailable"),
        )
        try:
            frame = operator_units.DIAGNOSTIC_PREFIX + json.dumps(
                {"schema_version": 1, "failures": ready.get("operator_failures", [])}
            )
        except (TypeError, ValueError):
            return False
        # The codec rejects unknown fields, booleans-as-integers and oversized
        # records. Cleanup, authority, RPC/query errors and unknown failures
        # cannot become a transient timeout by matching only an error label.
        return operator_units.diagnostic_chain(frame.encode()) == [expected]

    def owner_activity_only(self, ready: dict) -> bool:
        """A valid single-slot owner observation may precede its completion."""
        if (
            not isinstance(ready, dict)
            or type(ready.get("schema_version")) is not int
            or ready["schema_version"] != 1
            or ready.get("source_sha") != self.record["source_sha"]
            or ready.get("configuration_id") != self.selected["configuration_id"]
            or ready.get("service_preconditions_met") is not False
            or ready.get("ready_for_admission") is not False
            or ready.get("isolation_verified") is not False
            or ready.get("errors") != []
            or ready.get("operator_failures", []) != []
            or not operator_units.activity_blockers(ready.get("readiness_blockers"))
        ):
            return False
        try:
            components = ready["components"]
            root = components["factory_status"]
            counters = components["execution"]
            if (
                type(root["active_helpers"]) is not int
                or root["active_helpers"] not in (0, 1)
                or type(root["cleanup_complete"]) is not bool
                or root["cleanup_complete"] != (root["active_helpers"] == 0)
            ):
                return False
            observed = ["factory_active"] if root["active_helpers"] else []
            for key, code in (
                ("in_flight", "execution_in_flight"),
                ("cleanup_unconfirmed", "execution_cleanup_unconfirmed"),
            ):
                count = counters[key]
                if (
                    not isinstance(count, dict)
                    or set(count) != {"count_capped", "truncated"}
                    or type(count["count_capped"]) is not int
                    or count["count_capped"] not in (0, 1)
                    or count["truncated"] is not False
                ):
                    return False
                if count["count_capped"]:
                    observed.append(code)
            return ready["readiness_blockers"] == observed
        except (KeyError, TypeError):
            return False

    def start_locked(self, *, resume: bool) -> None:
        if not resume:
            self.require_fresh_start()
        availability.write(self.selected, "draining")
        for group in START_GROUPS:
            self.start_group(group)
        ready = operator_units.run("node-health", self.payload)
        reason = None
        if self.broker_query_timeout_only(ready):
            reason = ["broker_query_deadline"]
        elif self.owner_activity_only(ready):
            reason = ready["readiness_blockers"]
        if reason is not None and "readiness_rechecks" not in self.record:
            gate = availability.read()
            if (
                gate is None
                or gate.state != "draining"
                or gate.release_id != self.selected["release_id"]
                or gate.configuration_id != self.selected["configuration_id"]
            ):
                raise DeliveryError("readiness recheck requires the selected closed admission")
            # Persist the one attempted recheck before dispatch. The enclosing
            # 900s maintenance unit is unchanged and is never restarted; each
            # health helper keeps 300s and its broker query keeps 30s. No service
            # start/restart or admission opening occurs between observations.
            self.record["readiness_rechecks"] = 1
            self.record["readiness_recheck_reason"] = reason
            self.save(self.record["phase"])
            ready = operator_units.run("node-health", self.payload)
        if ready.get("service_preconditions_met") is not True:
            raise operator_units.readiness_failure(
                "service preconditions unavailable; admission remains closed", ready
            )
        try:
            availability.write(self.selected, "open")
            ready = operator_units.run("node-health", self.payload)
            if ready.get("ready_for_admission") is not True:
                raise operator_units.readiness_failure(
                    "admission readiness readback unconfirmed", ready, admission=True
                )
            self.save("accepting")
        except (OSError, ValueError, subprocess.SubprocessError):
            self.close_failed_admission()
            raise

    def close_failed_admission(self) -> None:
        close_failed_admission(self.selected)


def run(action: str) -> dict:
    operator_units.guarded("node-maintenance")
    selected, paths, payload = node_health.binding()
    for name in (
        "lifecycle.py",
        "availability.py",
        "recovery_database.py",
        "recovery_identity.py",
        "restore.py",
        "dependency_initialize.py",
        "manifest.py",
        "operation_proof.py",
    ):
        shipped = payload / "delivery/scripts/release" / name
        preflight.protected(shipped)
        if digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("maintenance tool differs from selected delivery")
    preflight.protected(STATE, directory=True)
    if stat.S_IMODE(STATE.stat().st_mode) != 0o700:
        raise DeliveryError("private operator state required")
    lockpath = STATE / "maintenance.lock"
    if not lockpath.exists():
        install.exclusive_file(lockpath, b"", 0o600)
    preflight.protected(lockpath)
    configuration.private(lockpath)
    if lockpath.stat().st_size != 0:
        raise DeliveryError("unrecorded maintenance lock contents")
    fd = os.open(lockpath, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Refuse unknown history before publishing availability, including
        # resume where every known service could already be running.
        previous = None
        if LAST.exists() or LAST.is_symlink():
            preflight.protected(LAST)
            configuration.private(LAST)
            previous = validate_record(read_json(LAST, 32768))
        maintenance = Maintenance(selected, paths, payload, action)
        try:
            if action in {"drain", "stop", "restart"}:
                if not maintenance.reuse_drained(previous):
                    maintenance.drain()
            if action == "stop":
                for name in ("rabbitmq", "postgres"):
                    maintenance.change(name, "stop")
            if action in {"start", "resume", "restart"}:
                maintenance.start(resume=action == "resume")
            maintenance.record["success"] = True
            maintenance.save("complete")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            maintenance.record["success"] = False
            maintenance.record["failure_code"] = "maintenance_unconfirmed"
            # Publication errors are not hidden by a successful-looking receipt.
            maintenance.save("failed")
            if isinstance(error, operator_units.OperatorExecutionError):
                raise operator_units.OperatorExecutionError(
                    "maintenance failed; admission not reopened automatically",
                    error.failures,
                ) from error
            raise DeliveryError(
                "maintenance failed; admission not reopened automatically"
            ) from error
        return maintenance.record
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed bounded single-node lifecycle")
    parser.add_argument(
        "--action",
        choices=("start", "resume", "drain", "stop", "restart"),
        required=True,
    )
    parser.add_argument("--inside", action="store_true")
    args = parser.parse_args()
    try:
        if args.inside:
            result = run(args.action)
        else:
            if os.getuid() != 0 or os.geteuid() != 0:
                raise DeliveryError("root operator required")
            selected = install.selection(check_configuration=False)
            install.directory(availability.GATE.parent, 0o711)
            install.directory(STATE, 0o700)
            try:
                result = operator_units.run(
                    "node-maintenance",
                    install.RELEASES / selected["release_id"] / "payload",
                    action=args.action,
                )
            except operator_units.OperatorExecutionError:
                # A killed helper cannot run its own exception handler. The
                # outer operator closes admission after the fixed helper exits.
                close_failed_admission(selected)
                raise
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Lifecycle refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n"
            + operator_units.diagnostic_suffix(error),
        )


if __name__ == "__main__":
    main()
