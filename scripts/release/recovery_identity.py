"""Ordinary isolated PostgreSQL role-password recovery, never business-state repair."""

import asyncio
import hashlib
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import configuration, database, dependencies, dependency_initialize, preflight
from .policy import DeliveryError, canonical, read_json

UNIT = "kf-postgres-recover.service"
RUNTIME = Path("/run/kf-postgres-recovery")
CREDENTIALS = Path("/run/credentials") / UNIT
PORT = 5440
CLIENT_SETTINGS = {
    **database.SETTINGS,
    "search_path": "pg_catalog",
    "session_preload_libraries": "",
    "local_preload_libraries": "",
    "log_statement": "none",
    "log_min_error_statement": "panic",
    "log_min_duration_statement": "-1",
    "log_min_duration_sample": "-1",
    "log_transaction_sample_rate": "0",
    "log_parameter_max_length_on_error": "0",
}


def properties(name: str) -> tuple:
    login = name in configuration.PROFILES.values()
    if name == "postgres":
        return True, True, True, True, True, True, -1, True, None, None
    return login, False, False, False, False, False, 8 if login else -1, True, None, None


def memberships(name: str) -> list:
    return (
        []
        if name not in database.MEMBERSHIP
        else [(database.MEMBERSHIP[name], False, True, name == "kf_migrator")]
    )


async def authority(connection) -> dict:
    result = {}
    for name in ("postgres", *database.GROUPS, *configuration.PROFILES.values()):
        row = await connection.fetchrow(
            "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,"
            "rolbypassrls,rolconnlimit,rolinherit,rolconfig,rolvaliduntil "
            "FROM pg_roles WHERE rolname=$1",
            name,
        )
        if row is None or tuple(row) != properties(name):
            raise DeliveryError("restored DB role authority differs; no overwrite")
        actual = sorted(
            tuple(row)
            for row in await connection.fetch(
                "SELECT r.rolname,m.admin_option,m.inherit_option,m.set_option "
                "FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid "
                "JOIN pg_roles u ON u.oid=m.member WHERE u.rolname=$1",
                name,
            )
        )
        if actual != memberships(name):
            raise DeliveryError("restored DB membership authority differs; no overwrite")
        result[name] = {"attributes": tuple(row), "memberships": actual}
    logins = await connection.fetch("SELECT rolname FROM pg_roles WHERE rolcanlogin")
    if {row[0] for row in logins} != {"postgres", *configuration.PROFILES.values()}:
        raise DeliveryError("restored DB has unexpected login authority")
    owner = await connection.fetchval(
        "SELECT r.rolname FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=$1",
        database.DATABASE,
    )
    if owner != "kf_migrator":
        raise DeliveryError("restored DB owner differs; no overwrite")
    if await connection.fetchval(
        "SELECT current_user"
    ) != "postgres" or not await connection.fetchval(
        "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
    ):
        raise DeliveryError("local peer recovery authority unavailable")
    return result


def recovery_settings() -> dict:
    return {
        "listen_addresses": "",
        "port": str(PORT),
        "unix_socket_directories": str(RUNTIME),
        "unix_socket_permissions": "0700",
        "hba_file": str(RUNTIME / "pg_hba.conf"),
        "ident_file": str(RUNTIME / "pg_ident.conf"),
        "logging_collector": "off",
        "log_destination": "stderr",
        "log_statement": "none",
        "log_min_error_statement": "panic",
        "log_min_duration_statement": "-1",
        "log_min_duration_sample": "-1",
        "log_transaction_sample_rate": "0",
        "log_connections": "",
        "log_disconnections": "off",
        "log_duration": "off",
        "log_error_verbosity": "terse",
        "log_parameter_max_length": "0",
        "log_parameter_max_length_on_error": "0",
        "debug_print_parse": "off",
        "debug_print_rewritten": "off",
        "debug_print_plan": "off",
        "log_line_prefix": "",
        "shared_preload_libraries": "",
        "session_preload_libraries": "",
        "local_preload_libraries": "",
        "archive_mode": "off",
        "archive_command": "",
        "archive_library": "",
        "ssl": "off",
        "fsync": "on",
        "full_page_writes": "on",
        "restore_command": "",
        "archive_cleanup_command": "",
        "recovery_end_command": "",
        "autovacuum": "off",
        "restart_after_crash": "off",
        "password_encryption": "scram-sha-256",
        "shared_buffers": "64MB",
        "max_connections": "16",
        "work_mem": "1MB",
        "maintenance_work_mem": "32MB",
        "max_wal_senders": "0",
        "max_worker_processes": "0",
        "max_parallel_workers": "0",
        "jit": "off",
        "statement_timeout": "5000",
        "lock_timeout": "1000",
        "idle_in_transaction_session_timeout": "5000",
        "authentication_timeout": "5",
        "temp_file_limit": "16384",
        "max_files_per_process": "128",
    }


def check_settings(rows) -> dict:
    actual = {row["name"]: (row["setting"], row["unit"]) for row in rows}
    wanted = recovery_settings()
    if set(actual) != set(wanted):
        raise DeliveryError("temporary recovery setting inventory differs")
    byte_values = {
        "shared_buffers": 64 * 1024**2,
        "work_mem": 1024**2,
        "maintenance_work_mem": 32 * 1024**2,
        "log_parameter_max_length": 0,
        "log_parameter_max_length_on_error": 0,
    }
    time_values = {
        "statement_timeout": 5000,
        "lock_timeout": 1000,
        "idle_in_transaction_session_timeout": 5000,
        "authentication_timeout": 5000,
        "log_min_duration_statement": -1,
        "log_min_duration_sample": -1,
    }
    for key, expected in wanted.items():
        value, unit = actual[key]
        if key in byte_values:
            good = (
                unit in {"B", "kB", "8kB"}
                and int(value) * {"B": 1, "kB": 1024, "8kB": 8192}[unit] == byte_values[key]
            )
        elif key in time_values:
            good = (
                unit in {"ms", "s"} and int(value) * {"ms": 1, "s": 1000}[unit] == time_values[key]
            )
        elif key == "unix_socket_permissions":
            good = unit is None and int(value, 8 if value.startswith("0") else 10) == 0o700
        elif key == "temp_file_limit":
            good = unit == "kB" and value == expected
        else:
            good = unit is None and value == expected
        if not good:
            raise DeliveryError("temporary recovery effective setting differs")
    return {key: {"setting": value, "unit": unit} for key, (value, unit) in actual.items()}


def write_private(path: Path, contents: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


async def rotate(process: subprocess.Popen, endpoints: dict, password: str) -> dict:
    import asyncpg

    deadline = time.monotonic() + 60
    connection = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise DeliveryError("temporary recovery server exited")
        try:
            connection = await asyncpg.connect(
                host=str(RUNTIME),
                port=PORT,
                user="postgres",
                database="postgres",
                timeout=3,
                command_timeout=5,
                server_settings=CLIENT_SETTINGS,
            )
            break
        except (OSError, asyncpg.PostgresError):
            await asyncio.sleep(0.25)
    if connection is None:
        raise DeliveryError("temporary peer recovery readiness deadline")
    try:
        before = await authority(connection)
        effective = check_settings(
            await connection.fetch(
                "SELECT name,setting,unit FROM pg_settings WHERE name=ANY($1::text[])",
                list(recovery_settings()),
            )
        )
        async with connection.transaction():
            await authority(connection)
            for key, name in configuration.PROFILES.items():
                secret = urlsplit(endpoints["database"][key]).password
                # Fixed names and the validated secret alphabet contain no SQL
                # delimiters. Error/statement logging remains disabled above.
                await connection.execute("ALTER ROLE " + name + " PASSWORD '" + secret + "'")
            await connection.execute("ALTER ROLE postgres PASSWORD '" + password + "'")
            if await authority(connection) != before:
                raise DeliveryError("DB authority changed during credential recovery")
        for key, name in configuration.PROFILES.items():
            client = await asyncpg.connect(
                host=str(RUNTIME),
                port=PORT,
                user=name,
                database=database.DATABASE,
                password=urlsplit(endpoints["database"][key]).password,
                timeout=3,
                command_timeout=5,
                server_settings=database.SETTINGS,
            )
            try:
                if tuple(await client.fetchrow("SELECT current_user,current_database()")) != (
                    name,
                    database.DATABASE,
                ):
                    raise DeliveryError("recovered service credential authentication differs")
            finally:
                await client.close(timeout=3)
        client = await asyncpg.connect(
            host=str(RUNTIME),
            port=PORT,
            user="postgres",
            database="template1",
            password=password,
            timeout=3,
            command_timeout=5,
            server_settings=database.SETTINGS,
        )
        try:
            if tuple(await client.fetchrow("SELECT current_user,current_database()")) != (
                "postgres",
                "template1",
            ):
                raise DeliveryError("recovered bootstrap credential authentication differs")
        finally:
            await client.close(timeout=3)
        return {
            "rotated_roles": sorted(["postgres", *configuration.PROFILES.values()]),
            "role_authority_sha256": hashlib.sha256(canonical(before)).hexdigest(),
            "effective_settings": effective,
            "authenticated": True,
            "schema_migrated": False,
            "execution_state_changed": False,
        }
    finally:
        await connection.close(timeout=3)


def run_in_unit() -> dict:
    observed = dependencies.check_runtime("postgres", recovering=True)
    if {
        line.split(":", 1)[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:]
    } != {"lo"}:
        raise DeliveryError("network-isolated recovery identity process required")
    if dependency_initialize.DATA.joinpath("postmaster.pid").exists():
        raise DeliveryError("restored PostgreSQL cluster is already active")
    if any(
        (dependency_initialize.DATA / name).exists()
        for name in ("standby.signal", "recovery.signal")
    ):
        raise DeliveryError("identity recovery refuses standby/replay signal files")
    dependency_initialize.cluster_identity()
    for name, maximum in (("connections", 16384), ("bootstrap", 65)):
        info = (CREDENTIALS / name).lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in {0, 1203}
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            or info.st_size > maximum
        ):
            raise DeliveryError("private recovery credential identity differs")
    endpoints = configuration.validate_connections(read_json(CREDENTIALS / "connections", 16384))
    raw = (CREDENTIALS / "bootstrap").read_bytes()
    dependency_initialize.validate_password(raw)
    password = raw.decode().strip()
    info = RUNTIME.lstat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1203, 1203, 0o700) or next(
        RUNTIME.iterdir(), None
    ) is not None:
        raise DeliveryError("private empty recovery runtime directory required")
    write_private(
        RUNTIME / "pg_hba.conf",
        (
            "local postgres postgres peer map=kf_recovery\n"
            "local template1 postgres scram-sha-256\n"
            "local kf_onlinejudge " + ",".join(configuration.PROFILES.values()) + " scram-sha-256\n"
            "local all all reject\n"
        ).encode(),
    )
    write_private(RUNTIME / "pg_ident.conf", b"kf_recovery kf-postgres postgres\n")
    preflight.protected(Path("/usr/bin/postgres"))
    version = subprocess.run(
        ["/usr/bin/postgres", "--version"],
        capture_output=True,
        timeout=5,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
    )
    if version.returncode or version.stdout.strip() != b"postgres (PostgreSQL) 18.6":
        raise DeliveryError("recovery PostgreSQL binary version differs")
    command = ["/usr/bin/postgres", "-D", str(dependency_initialize.DATA)]
    for key, value in recovery_settings().items():
        command += ["-c", key + "=" + value]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent"},
    )
    try:
        result = asyncio.run(asyncio.wait_for(rotate(process, endpoints, password), timeout=155))
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        try:
            status = process.wait(timeout=40)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=5)
            raise DeliveryError("temporary recovery shutdown unconfirmed") from error
        if status or dependency_initialize.DATA.joinpath("postmaster.pid").exists():
            raise DeliveryError("temporary recovery PostgreSQL clean shutdown unconfirmed")
    return {
        **observed,
        **result,
        "temporary_server_stopped": True,
        "services_started": False,
        "execution_verified": False,
    }


def main() -> None:
    import asyncpg

    try:
        print(json.dumps(run_in_unit(), sort_keys=True))
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
    ):
        raise SystemExit(
            "PostgreSQL identity recovery refused; target remains stopped; "
            "no schema/grade repair performed."
        ) from None


if __name__ == "__main__":
    main()
