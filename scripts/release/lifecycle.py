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
from pathlib import Path
from uuid import UUID

from . import (
    availability,
    configuration,
    dependency_install,
    factory_service,
    install,
    node_health,
    operator_units,
    preflight,
    service_install,
    services,
)
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256

STATE = Path("/var/lib/kf-onlinejudge/operations")
LAST = STATE / "maintenance.last.json"
APPS = ("api", "outbox", "worker", "problem-import", "coordinator", "sandbox-factory")
DEPENDENCIES = ("postgres", "rabbitmq")
START_ORDER = (
    "sandbox-factory",
    "coordinator",
    "api",
    "outbox",
    "problem-import",
    "worker",
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
        or not set(value) - required <= {"drain_projection", "failure_code"}
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
        if name not in {*APPS, *DEPENDENCIES}:
            raise DeliveryError("unknown lifecycle target")
        path = service_install.SYSTEM_UNITS / ("kf-" + name + ".service")
        if name in services.PROFILES:
            directory = service_install.known_link(path)
            base = service_install.UNITS
        elif name == "sandbox-factory":
            directory = factory_service.known_link(path)
            base = factory_service.UNITS
        else:
            directory = dependency_install.known_link(path)
            base = dependency_install.ROOT
        if directory != base / (
            self.selected["release_id"] + "." + self.selected["configuration_id"]
        ):
            raise DeliveryError("lifecycle target is not the selected snapshot")
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
            return
        if operation == "stop" and values["ActiveState"] == "inactive" and values["MainPID"] == "0":
            self.empty_cgroup(name)
            return
        if operation not in {"start", "stop"}:
            raise DeliveryError("unexpected lifecycle operation")
        self.record["events"].append({"unit": name, "operation": operation, "confirmed": False})
        self.save("dispatching_" + operation)
        service_install.systemctl("--no-block", operation, "kf-" + name + ".service")
        deadline = time.monotonic() + (130 if operation == "start" else 50)
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
        for name in ("api", "outbox", "problem-import", "worker"):
            self.change(name, "stop")
        final = self.snapshot()
        if not projection_empty(final):
            raise DeliveryError("work changed during final stop; quiescence unconfirmed")
        self.change("coordinator", "stop")
        self.change("sandbox-factory", "stop")
        self.root_stopped()
        availability.write(self.selected, "quiescent")
        self.save("quiescent")

    def start(self, *, resume: bool) -> None:
        if not resume:
            for name in APPS:
                value = self.unit(name)
                if value["ActiveState"] != "inactive" or value["MainPID"] != "0":
                    raise DeliveryError("fresh start requires stopped application units")
                self.empty_cgroup(name)
            self.root_stopped()
        availability.write(self.selected, "draining")
        for name in DEPENDENCIES:
            self.change(name, "start")
        for name in START_ORDER:
            self.change(name, "start")
        ready = operator_units.run("node-health", self.payload)
        if ready.get("service_preconditions_met") is not True:
            raise DeliveryError("service preconditions unavailable; admission remains closed")
        availability.write(self.selected, "open")
        try:
            ready = operator_units.run("node-health", self.payload)
            if ready.get("ready_for_admission") is not True:
                raise DeliveryError("admission readiness readback unconfirmed")
        except (OSError, ValueError, subprocess.SubprocessError):
            availability.write(self.selected, "draining")
            raise
        self.save("accepting")


def run(action: str) -> dict:
    operator_units.guarded("node-maintenance")
    selected, paths, payload = node_health.binding()
    for name in ("lifecycle.py", "availability.py"):
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
    fd = os.open(lockpath, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        maintenance = Maintenance(selected, paths, payload, action)
        try:
            if action in {"drain", "stop", "restart"}:
                maintenance.drain()
            if action == "stop":
                for name in ("rabbitmq", "postgres"):
                    maintenance.change(name, "stop")
            if action in {"start", "resume", "restart"}:
                maintenance.start(resume=action == "resume")
            maintenance.record["success"] = True
            maintenance.save("complete")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            maintenance.record["failure_code"] = "maintenance_unconfirmed"
            # Publication errors are not hidden by a successful-looking receipt.
            maintenance.save("failed")
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
            result = operator_units.run(
                "node-maintenance",
                install.RELEASES / selected["release_id"] / "payload",
                action=args.action,
            )
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Lifecycle refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
