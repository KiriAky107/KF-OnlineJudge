"""Root dependency snapshots on pre-provisioned storage; never initialize/start DBs."""

import argparse
import base64
import grp
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from . import configuration, dependencies, install, preflight, service_install
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256

ROOT = dependencies.CONFIGURATION_ROOT
NAMES = {"kf-postgres.service", "kf-rabbitmq.service"}
FILES = {
    "postgresql.conf": (0o640, 1203),
    "pg_hba.conf": (0o640, 1203),
    "rabbitmq.conf": (0o640, 1204),
    "enabled_plugins": (0o640, 1204),
    "definitions.json": (0o640, 1204),
    **{name: (0o644, 0) for name in NAMES},
}
IDENTITIES = {
    "postgres": (1203, "/var/lib/postgres"),
    "rabbitmq": (1204, "/var/lib/rabbitmq"),
}


def validate_receipt(value: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "source_sha",
            "configuration_id",
            "configuration_receipt_sha256",
            "files",
            "required_storage_mounts",
            "installed",
            "storage_limits_verified",
            "services_started",
            "execution_verified",
        },
        "dependency receipt",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["source_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["source_sha"])
        or not isinstance(value["configuration_id"], str)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", value["configuration_id"])
        or value["required_storage_mounts"] != dependencies.STORAGE
        or any(
            value[key] is not False
            for key in (
                "installed",
                "storage_limits_verified",
                "services_started",
                "execution_verified",
            )
        )
    ):
        raise DeliveryError("dependency receipt binding differs")
    sha256(value["configuration_receipt_sha256"])
    fields(value["files"], set(FILES), "dependency file inventory")
    for checksum in value["files"].values():
        sha256(checksum)
    return value


def batch_directory(receipt: dict) -> Path:
    validate_receipt(receipt)
    return ROOT / ("sd-" + receipt["source_sha"][:12] + "." + receipt["configuration_id"])


def previous_batch(target: str, name: str) -> Path:
    path = Path(target)
    if (
        name not in NAMES
        or path.name != name
        or path.parent.parent != ROOT
        or not re.fullmatch(r"sd-[0-9a-f]{12}\.cfg-[0-9a-f]{24}", path.parent.name)
        or path.as_posix() != target
    ):
        raise DeliveryError("unknown dependency unit target; operator review required")
    return path.parent


def check_group(directory: Path, receipt: dict) -> None:
    preflight.protected(directory, directory=True)
    if stat.S_IMODE(directory.stat().st_mode) != 0o711:
        raise DeliveryError("protected searchable dependency batch required")
    if {p.name for p in directory.iterdir()} != set(FILES) | {"receipt.json"}:
        raise DeliveryError("unexpected installed dependency member")
    path = directory / "receipt.json"
    preflight.protected(path)
    configuration.private(path)
    if canonical(validate_receipt(read_json(path, 16384))) != canonical(receipt):
        raise DeliveryError("installed dependency receipt differs")
    for name, (mode, group) in FILES.items():
        path = directory / name
        preflight.protected(path)
        info = path.stat()
        if (
            info.st_size > 65536
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_gid != group
            or digest(path) != receipt["files"][name]
        ):
            raise DeliveryError("installed dependency bytes/permissions differ")


def known_link(path: Path) -> Path | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise DeliveryError("unknown dependency system unit; refused overwrite")
    directory = previous_batch(os.readlink(path), path.name)
    receipt_path = directory / "receipt.json"
    preflight.protected(receipt_path)
    configuration.private(receipt_path)
    receipt = validate_receipt(read_json(receipt_path, 16384))
    if batch_directory(receipt) != directory:
        raise DeliveryError("old dependency unit binding differs")
    check_group(directory, receipt)
    return directory


def check_accounts() -> None:
    for name, (identity, home) in IDENTITIES.items():
        try:
            if pwd.getpwuid(identity).pw_name != name:
                raise DeliveryError("dependency uid already assigned")
        except KeyError:
            pass
        try:
            if grp.getgrgid(identity).gr_name != name:
                raise DeliveryError("dependency gid already assigned")
        except KeyError:
            pass
        try:
            account = pwd.getpwnam(name)
            if (account.pw_uid, account.pw_gid, account.pw_dir, account.pw_shell) != (
                identity,
                identity,
                home,
                "/usr/bin/nologin",
            ):
                raise DeliveryError("existing ordinary dependency identity differs")
            preflight.protected(Path("/etc/shadow"))
            shadow = Path("/etc/shadow")
            if shadow.stat().st_size > 1024 * 1024:
                raise DeliveryError("shadow identity observation exceeds bound")
            rows = [line.split(":") for line in shadow.read_text().splitlines()]
            passwords = [row[1] for row in rows if len(row) == 9 and row[0] == name]
            if len(passwords) != 1 or not passwords[0].startswith(("!", "*")):
                raise DeliveryError("dependency login identity must be locked")
        except KeyError:
            pass
        try:
            group = grp.getgrnam(name)
            if group.gr_gid != identity or group.gr_mem:
                raise DeliveryError("dependency group authority differs")
        except KeyError:
            pass
        if any(name in group.gr_mem for group in grp.getgrall()):
            raise DeliveryError("dependency supplementary authority refused")


def ensure_accounts() -> None:
    check_accounts()
    for name, (identity, home) in IDENTITIES.items():
        try:
            grp.getgrnam(name)
        except KeyError:
            subprocess.run(
                ["/usr/bin/groupadd", "--gid", str(identity), name],
                check=True,
                timeout=10,
                capture_output=True,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        try:
            pwd.getpwnam(name)
        except KeyError:
            subprocess.run(
                [
                    "/usr/bin/useradd",
                    "--uid",
                    str(identity),
                    "--gid",
                    str(identity),
                    "--no-create-home",
                    "--no-user-group",
                    "--key",
                    "SUB_UID_COUNT=0",
                    "--key",
                    "SUB_GID_COUNT=0",
                    "--home-dir",
                    home,
                    "--shell",
                    "/usr/bin/nologin",
                    name,
                ],
                check=True,
                timeout=10,
                capture_output=True,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
    check_accounts()
    for name in IDENTITIES:
        pwd.getpwnam(name)
        grp.getgrnam(name)


def require_stopped() -> None:
    install.require_stopped()
    for name in sorted(NAMES):
        values = dict(
            line.split("=", 1)
            for line in service_install.systemctl(
                "show", name, "--property=LoadState,ActiveState,MainPID"
            ).splitlines()
            if "=" in line
        )
        if (
            values.get("LoadState") not in {"loaded", "not-found"}
            or values.get("ActiveState") != "inactive"
            or values.get("MainPID") != "0"
        ):
            raise DeliveryError("all dependency units must be stopped")
    for port in (5432, 5672, 25672, 4369):
        import socket

        with socket.socket() as check:
            try:
                check.bind(("127.0.0.1", port))
            except OSError as error:
                raise DeliveryError("dependency port belongs to a running process") from error
    if Path("/run/postgresql/.s.PGSQL.5432").exists():
        raise DeliveryError("dependency DB socket remains")


def validate_definitions(value: dict, endpoints: dict) -> None:
    """Match exact broker privileges and independently check salted credentials."""
    expected = dependencies.broker_definitions(endpoints)
    fields(value, set(expected), "broker definitions")
    if not isinstance(value["users"], list) or len(value["users"]) != 2:
        raise DeliveryError("broker user inventory differs")
    for actual, wanted in zip(value["users"], expected["users"], strict=True):
        fields(actual, set(wanted), "broker user")
        encoded = actual["password_hash"]
        if not isinstance(encoded, str) or len(encoded) != 48:
            raise DeliveryError("broker password fingerprint encoding differs")
        try:
            packed = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise DeliveryError("broker password fingerprint encoding differs") from error
        password = urlsplit(endpoints["amqp"][wanted["name"].removeprefix("kf_")]).password
        if len(packed) != 36 or not hmac.compare_digest(
            packed[4:], hashlib.sha256(packed[:4] + password.encode()).digest()
        ):
            raise DeliveryError("broker password differs from selected batch")
        wanted["password_hash"] = encoded
    if canonical(value) != canonical(expected):
        raise DeliveryError("broker privileges, queue or budget differ")


def installed_endpoints(selected: dict) -> dict:
    def env(name: str) -> dict:
        return configuration.environment_read(
            configuration.installed_path(
                selected["release_id"], selected["configuration_id"], name
            ).read_bytes()
        )

    def cfg(name: str) -> dict:
        return read_json(
            configuration.installed_path(
                selected["release_id"], selected["configuration_id"], name
            ),
            32768,
        )

    def db(name: str, key: str = "KF_OJ_DATABASE_URL") -> str:
        return env(name)[key].replace("postgresql+asyncpg:", "postgresql:", 1)

    return configuration.validate_connections(
        {
            "schema_version": 1,
            "database": {
                "api": db("api.env"),
                "coordinator": cfg("coordinator.json")["database_url"],
                "import": db("import.env"),
                "migration": db("migration.env"),
                "identity": db("identity.env", "KF_OJ_IDENTITY_OPERATOR_DATABASE_URL"),
            },
            "amqp": {
                "outbox": env("outbox.env")["KF_OJ_AMQP_URL"],
                "worker": cfg("worker.json")["amqp_url"],
            },
        }
    )


def write_group_file(path: Path, contents: bytes, mode: int, group: int) -> None:
    # Publish only root-owned immutable bytes, with one dedicated read group.
    if path.exists() or path.is_symlink():
        preflight.protected(path)
        info = path.stat()
        if (stat.S_IMODE(info.st_mode), info.st_gid) != (
            mode,
            group,
        ) or path.read_bytes() != contents:
            raise DeliveryError("existing dependency file differs; refused overwrite")
        return
    install.exclusive_file(path, contents, 0o600)
    os.chown(path, 0, group)
    path.chmod(mode)
    install.sync_directory(path.parent)


def storage_ownership(name: str) -> None:
    path = Path(dependencies.STORAGE[name]["path"])
    identity = IDENTITIES[name][0]
    info = path.lstat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
        identity,
        identity,
        0o700,
    ):
        return
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 0, 0o700):
        raise DeliveryError("dependency mount ownership conflicts")
    # An empty, deliberately provisioned ext4 volume can contain only its
    # private, empty recovery directory. Never recursively chown existing data.
    for member in path.iterdir():
        if member.name != "lost+found":
            raise DeliveryError("unrecognized data on root-owned dependency mount")
        preflight.protected(member, directory=True)
        if stat.S_IMODE(member.stat().st_mode) != 0o700 or next(member.iterdir(), None) is not None:
            raise DeliveryError("dependency recovery directory is not empty/private")
    os.chown(path, identity, identity)


def install_cookie(source: Path, expected: str) -> None:
    preflight.protected(source)
    configuration.private(source)
    if source.stat().st_size != 65 or digest(source) != sha256(expected):
        raise DeliveryError("explicit private broker cookie fingerprint differs")
    contents = source.read_bytes()
    if re.fullmatch(rb"[0-9a-f]{64}\n", contents) is None:
        raise DeliveryError("broker cookie must be an explicit 256-bit hexadecimal value")
    path = Path(dependencies.STORAGE["rabbitmq"]["path"]) / ".erlang.cookie"
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1204, 1204, 0o400)
            or info.st_size != 65
            or path.read_bytes() != contents
        ):
            raise DeliveryError("existing broker cookie differs; refused rotation")
        return
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(contents)
            output.flush()
            os.fsync(fd)
        os.fchown(fd, 1204, 1204)
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)
    install.sync_directory(path.parent)


def install_snapshot(source: Path, expected: str, cookie: Path, cookie_sha: str) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0:
        raise DeliveryError("dependency installation requires root operator")
    preflight.protected(source, directory=True)
    path = source / "receipt.json"
    preflight.protected(path)
    configuration.private(path)
    if digest(path) != sha256(expected):
        raise DeliveryError("dependency receipt fingerprint differs")
    receipt = validate_receipt(read_json(path, 16384))
    selected, original = service_install.current_receipt()
    if (
        receipt["source_sha"] != original["source_sha"]
        or receipt["configuration_id"] != selected["configuration_id"]
        or receipt["configuration_receipt_sha256"] != selected["receipt_sha256"]
    ):
        raise DeliveryError("dependency snapshot is not the selected binding")
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in ("dependencies.py", "dependency_install.py"):
        shipped = payload / "delivery/scripts/release" / name
        preflight.protected(shipped)
        if digest(shipped) != digest(Path(__file__).parent / name):
            raise DeliveryError("dependency installer differs from selected delivery")
    if {p.name for p in source.iterdir()} != set(FILES) | {"receipt.json"}:
        raise DeliveryError("unexpected dependency source member")
    contents = dependencies.render(selected["release_id"], selected["configuration_id"])
    for name in FILES:
        path = source / name
        preflight.protected(path)
        configuration.private(path)
        if path.stat().st_size > 65536 or digest(path) != receipt["files"][name]:
            raise DeliveryError("dependency source fingerprint differs")
        if name != "definitions.json" and path.read_bytes() != contents[name]:
            raise DeliveryError("dependency file is not the fixed trusted composition")
    validate_definitions(
        read_json(source / "definitions.json", 65536), installed_endpoints(selected)
    )
    contents["definitions.json"] = (source / "definitions.json").read_bytes()
    require_stopped()
    check_accounts()
    storage = {name: dependencies.storage_observation(name) for name in IDENTITIES}
    preflight.protected(service_install.SYSTEM_UNITS, directory=True)
    for name in NAMES:
        known_link(service_install.SYSTEM_UNITS / name)
    ensure_accounts()
    for name in IDENTITIES:
        storage_ownership(name)
    install_cookie(cookie, cookie_sha)
    install.directory(ROOT, 0o711)
    directory = batch_directory(receipt)
    install.directory(directory, 0o711)
    if not {p.name for p in directory.iterdir()} <= set(FILES) | {"receipt.json"}:
        raise DeliveryError("unexpected partial dependency batch member")
    for member in directory.iterdir():
        preflight.protected(member)
        if member.stat().st_size > 65536:
            raise DeliveryError("partial dependency member exceeds bound")
    for name, (mode, group) in FILES.items():
        write_group_file(directory / name, contents[name], mode, group)
    install.exclusive_file(directory / "receipt.json", canonical(receipt), 0o600)
    check_group(directory, receipt)
    if install.selection(check_configuration=False) != selected:
        raise DeliveryError("selected binding changed during dependency installation")
    require_stopped()
    for name in sorted(NAMES):
        path = service_install.SYSTEM_UNITS / name
        if known_link(path) == directory:
            continue
        temporary = path.parent / ("." + name + ".dependency-" + secrets.token_hex(8))
        created = None
        try:
            temporary.symlink_to(directory / name)
            info = temporary.lstat()
            created = (info.st_dev, info.st_ino)
            known_link(path)
            os.replace(temporary, path)
            install.sync_directory(path.parent)
        finally:
            if created is not None and temporary.is_symlink():
                info = temporary.lstat()
                if (info.st_dev, info.st_ino) == created:
                    temporary.unlink()
    service_install.systemctl("daemon-reload")
    loaded = {}
    for name in sorted(NAMES):
        values = dict(
            line.split("=", 1)
            for line in service_install.systemctl(
                "show", name, "--property=LoadState,ActiveState,MainPID,FragmentPath"
            ).splitlines()
            if "=" in line
        )
        if (
            values.get("LoadState") != "loaded"
            or values.get("ActiveState") != "inactive"
            or values.get("MainPID") != "0"
            or values.get("FragmentPath")
            not in {str(service_install.SYSTEM_UNITS / name), str(directory / name)}
            or known_link(service_install.SYSTEM_UNITS / name) != directory
        ):
            raise DeliveryError("dependency unit load readback differs")
        loaded[name] = values
    if install.selection() != selected:
        raise DeliveryError("selected binding changed during dependency loading")
    return {
        "source_sha": receipt["source_sha"],
        "configuration_id": receipt["configuration_id"],
        "units_installed": True,
        "storage_mount_readback": storage,
        "unit_manager_readback": loaded,
        "cluster_initialized": False,
        "services_started": False,
        "execution_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Protected dependency snapshot installation")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--receipt-sha256", required=True)
    parser.add_argument("--erlang-cookie", required=True, type=Path)
    parser.add_argument("--cookie-sha256", required=True)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                install_snapshot(
                    args.directory,
                    args.receipt_sha256,
                    args.erlang_cookie,
                    args.cookie_sha256,
                ),
                sort_keys=True,
            )
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Dependency installation refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
