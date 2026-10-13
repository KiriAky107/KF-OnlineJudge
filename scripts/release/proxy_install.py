"""Protected HTTPS installation; no default keys, schema migration or server start."""

import argparse
import grp
import json
import os
import pwd
import secrets
import ssl
import stat
import subprocess
from pathlib import Path

from . import (
    configuration,
    install,
    journal,
    preflight,
    proxy_runtime,
    proxy_service,
    service_install,
)
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256


def account(*, create: bool = False, check_lock: bool = False) -> None:
    name, uid = proxy_service.USER, proxy_service.UID
    matches = [user for user in pwd.getpwall() if user.pw_name == name or user.pw_uid == uid]
    groups = [group for group in grp.getgrall() if group.gr_name == name or group.gr_gid == uid]
    if not matches and not groups and create:
        for args in (
            ["/usr/bin/groupadd", "--system", "--gid", str(uid), name],
            [
                "/usr/bin/useradd",
                "--system",
                "--uid",
                str(uid),
                "--gid",
                str(uid),
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/bin/nologin",
                "--no-user-group",
                name,
            ],
        ):
            p = subprocess.run(
                args, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, capture_output=True, timeout=10
            )
            if p.returncode:
                raise DeliveryError("proxy account provisioning refused")
        return account(check_lock=True)
    if (
        len(matches) != 1
        or len(groups) != 1
        or matches[0].pw_name != name
        or matches[0].pw_uid != uid
        or matches[0].pw_gid != uid
        or matches[0].pw_dir != "/nonexistent"
        or matches[0].pw_shell != "/usr/bin/nologin"
        or groups[0].gr_name != name
        or groups[0].gr_gid != uid
        or groups[0].gr_mem
    ):
        raise DeliveryError("conflicting ordinary proxy account")
    if os.getgrouplist(name, uid) != [uid]:
        raise DeliveryError("proxy supplementary authority refused")
    if create or check_lock:
        preflight.protected(Path("/etc/shadow"))
        rows = [
            line.split(":")
            for line in journal.bounded_text(Path("/etc/shadow"), 1024**2).splitlines()
        ]
        selected = [row for row in rows if row[0] == name]
        if len(selected) != 1 or len(selected[0]) != 9 or selected[0][1] not in ("!", "!!", "*"):
            raise DeliveryError("proxy OS login must remain locked")


def storage(*, allow_read_only: bool = False) -> dict:
    path = proxy_service.DATA
    preflight.protected(path.parent, directory=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != proxy_service.UID
        or info.st_gid != proxy_service.UID
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DeliveryError("private ordinary proxy hard volume required")
    rows = [
        line.split()
        for line in journal.bounded_text(Path("/proc/self/mountinfo"), 1024**2).splitlines()
    ]
    found = [row for row in rows if len(row) > 6 and row[4] == str(path)]
    if len(found) != 1:
        raise DeliveryError("dedicated proxy volume required")
    row = found[0]
    index = row.index("-")
    vfs = os.statvfs(path)
    if (
        row[3] != "/"
        or row[index + 1] != "ext4"
        or not {"nosuid", "nodev", "noexec"} <= set(row[5].split(","))
        or set(row[5].split(",")) & {"ro", "rw"}
        not in (({"ro"}, {"rw"}) if allow_read_only else ({"rw"},))
        or not proxy_service.CAPACITY * 9 // 10
        <= vfs.f_frsize * vfs.f_blocks
        <= proxy_service.CAPACITY
        or any(other[2] == row[2] and other != row for other in rows)
    ):
        raise DeliveryError("proxy hard storage capacity/mount differs")
    return {
        "capacity_bytes": vfs.f_frsize * vfs.f_blocks,
        "available_bytes": vfs.f_frsize * vfs.f_bavail,
    }


def known_link(path: Path) -> Path | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise DeliveryError("unknown proxy system unit")
    target = Path(os.readlink(path))
    if (
        target.name != proxy_service.UNIT
        or target.parent.parent != proxy_service.UNITS
        or target.as_posix() != os.readlink(path)
    ):
        raise DeliveryError("unknown proxy unit pointer")
    preflight.protected(target.parent / "binding.json")
    configuration.private(target.parent / "binding.json")
    selected = read_json(target.parent / "binding.json", 4096)
    fields(
        selected,
        {"schema_version", "release_id", "configuration_id", "manifest_sha256", "receipt_sha256"},
        "proxy unit binding",
    )
    if type(selected["schema_version"]) is not int or selected["schema_version"] != 1:
        raise DeliveryError("proxy unit binding version differs")
    sha256(selected["manifest_sha256"])
    sha256(selected["receipt_sha256"])
    if target.parent.name != proxy_service.batch(selected).name:
        raise DeliveryError("proxy unit snapshot binding differs")
    preflight.protected(target)
    preflight.protected(target.parent / "binding.json")
    if (
        target.read_bytes() != proxy_service.render(selected)
        or stat.S_IMODE(target.stat().st_mode) != 0o644
    ):
        raise DeliveryError("proxy unit snapshot changed")
    return target.parent


def check_binding(release: str, batch: str) -> dict:
    if os.geteuid() != 0:
        raise DeliveryError("protected proxy operator required")
    from .node_health import binding

    # Reuse the protected live configuration and source binding. Proxy startup
    # does not recertify unrelated judge images; Sandbox checks each execution.
    selected, _, _ = binding()
    if (release, batch) != (selected["release_id"], selected["configuration_id"]):
        raise DeliveryError("proxy unit belongs to another selected configuration")
    directory = proxy_service.batch(selected)
    spec = read_json(directory / "launch.json", 4096)
    for member in (directory / "launch.json", directory / "nginx.conf"):
        preflight.protected(member)
        if member.stat().st_gid != 0 or stat.S_IMODE(member.stat().st_mode) != 0o644:
            raise DeliveryError("protected public proxy configuration required")
    fields(
        spec,
        {
            "schema_version",
            "release_id",
            "configuration_id",
            "origin",
            "configuration_sha256",
            "certificate_sha256",
            "key_sha256",
        },
        "proxy launch spec",
    )
    if (
        spec["schema_version"] != 1
        or type(spec["schema_version"]) is not int
        or (spec["release_id"], spec["configuration_id"]) != (release, batch)
        or digest(directory / "nginx.conf") != spec["configuration_sha256"]
    ):
        raise DeliveryError("proxy configuration binding changed")
    from .proxy import validate_profile

    validate_profile(
        {
            "schema_version": 1,
            "origin": spec["origin"],
            "certificate_sha256": spec["certificate_sha256"],
            "key_sha256": spec["key_sha256"],
        }
    )
    for name, key, mode, gid in [
        ("fullchain.pem", "certificate_sha256", 0o644, 0),
        ("key.pem", "key_sha256", 0o640, proxy_service.UID),
    ]:
        p = proxy_service.ROOT / "tls" / name
        preflight.protected(p)
        i = p.stat()
        if (
            i.st_gid != gid
            or stat.S_IMODE(i.st_mode) != mode
            or i.st_size > 65536
            or digest(p) != spec[key]
        ):
            raise DeliveryError("proxy TLS input changed")
    journal.check_profile(allow_read_only=True)
    storage(allow_read_only=True)
    account(check_lock=True)
    if install.selection(check_configuration=False) != selected:
        raise DeliveryError("selected proxy binding changed during startup check")
    return {"binding_valid": True, "tls_verified": False, "execution_verified": False}


def install_proxy(source: Path, expected: str, certificate: Path, key: Path) -> dict:
    if os.getuid() != 0 or os.geteuid() != 0 or os.getgid() != 0:
        raise DeliveryError("root proxy installer required")
    preflight.protected(source, directory=True)
    receipt_path = source / "receipt.json"
    preflight.protected(receipt_path)
    configuration.private(receipt_path)
    if digest(receipt_path) != sha256(expected):
        raise DeliveryError("proxy receipt fingerprint differs")
    receipt = proxy_service.validate_receipt(read_json(receipt_path, 8192))
    selected = install.selection()
    spec = proxy_service.launch_spec(selected, receipt)
    payload = install.RELEASES / selected["release_id"] / "payload"
    for name in (
        "proxy.py",
        "proxy_service.py",
        "proxy_install.py",
        "proxy_guard.py",
        "proxy_runtime.py",
    ):
        p = payload / "delivery/scripts/release" / name
        preflight.protected(p)
        if digest(p) != digest(Path(__file__).parent / name):
            raise DeliveryError("proxy installer differs from selected delivery")
    runtime = payload / "proxy"
    preflight.protected(runtime / "manifest.json")
    data = proxy_runtime.verify(runtime, digest(runtime / "manifest.json"))
    # The selected manifest already bound every installed byte; local catalog
    # verification here is an additional runtime-tree check, not a trust origin.
    inventory = read_json(payload.parent / "manifest.json")["files"]
    item = next((entry for entry in inventory if entry["path"] == "proxy/manifest.json"), None)
    if item is None or item["sha256"] != digest(runtime / "manifest.json"):
        raise DeliveryError("proxy catalog absent from selected manifest")
    env = configuration.environment_read(
        configuration.installed_path(
            selected["release_id"], selected["configuration_id"], "api.env"
        ).read_bytes()
    )
    if json.loads(env["KF_OJ_BROWSER_ORIGINS"]) != [receipt["origin"]]:
        raise DeliveryError("proxy browser origin differs from API batch")
    if {p.name for p in source.iterdir()} != {"receipt.json", "nginx.conf"}:
        raise DeliveryError("unknown proxy source member")
    preflight.protected(source / "nginx.conf")
    configuration.private(source / "nginx.conf")
    contents = proxy_service.configuration(receipt, selected["release_id"])
    if (source / "nginx.conf").read_bytes() != contents or digest(source / "nginx.conf") != spec[
        "configuration_sha256"
    ]:
        raise DeliveryError("proxy is not fixed trusted composition")
    for p, fingerprint in ((certificate, spec["certificate_sha256"]), (key, spec["key_sha256"])):
        preflight.protected(p)
        configuration.private(p)
        if not 1 <= p.stat().st_size <= 65536 or digest(p) != fingerprint:
            raise DeliveryError("explicit bounded TLS material differs")
    install.require_stopped()
    account(create=True)
    # Storage provisioning is an explicit operator step; no format/mount and no
    # recursive ownership change of existing data are performed here.
    storage()
    journal.check_profile()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    def no_password_prompt():
        raise DeliveryError("explicit unencrypted private TLS file required; no interactive prompt")

    ctx.load_cert_chain(certificate, key, password=no_password_prompt)
    system = service_install.SYSTEM_UNITS / proxy_service.UNIT
    known_link(system)
    install.directory(proxy_service.ROOT, 0o711)
    install.directory(proxy_service.ROOT / "tls", 0o711)
    directory = proxy_service.batch(selected)
    install.directory(directory, 0o755)
    if not {p.name for p in directory.iterdir()} <= {"nginx.conf", "launch.json"}:
        raise DeliveryError("unknown proxy configuration snapshot")
    install.exclusive_file(
        proxy_service.ROOT / "tls" / "fullchain.pem", certificate.read_bytes(), 0o644
    )
    private = proxy_service.ROOT / "tls" / "key.pem"
    if private.exists() or private.is_symlink():
        preflight.protected(private)
        i = private.stat()
        if (
            stat.S_IMODE(i.st_mode) != 0o640
            or i.st_gid != proxy_service.UID
            or digest(private) != spec["key_sha256"]
        ):
            raise DeliveryError("unknown existing TLS key; refused rotation")
    else:
        install.exclusive_file(private, key.read_bytes(), 0o640)
        os.chown(private, 0, proxy_service.UID)
    install.exclusive_file(directory / "nginx.conf", contents, 0o644)
    install.exclusive_file(directory / "launch.json", canonical(spec), 0o644)
    install.directory(proxy_service.UNITS, 0o700)
    unitdir = proxy_service.UNITS / directory.name
    install.directory(unitdir, 0o700)
    install.exclusive_file(unitdir / "binding.json", canonical(selected), 0o600)
    install.exclusive_file(unitdir / proxy_service.UNIT, proxy_service.render(selected), 0o644)
    body = proxy_service.DATA / "body"
    if body.exists() or body.is_symlink():
        i = body.lstat()
        if (
            not stat.S_ISDIR(i.st_mode)
            or i.st_uid != proxy_service.UID
            or i.st_gid != proxy_service.UID
            or stat.S_IMODE(i.st_mode) != 0o700
        ):
            raise DeliveryError("unknown proxy temporary directory")
    else:
        body.mkdir(mode=0o700)
        os.chown(body, proxy_service.UID, proxy_service.UID)
    check_binding(selected["release_id"], selected["configuration_id"])
    install.require_stopped()
    if install.selection() != selected:
        raise DeliveryError("proxy selection changed before unit switch")
    old = known_link(system)
    if old != unitdir:
        temporary = service_install.SYSTEM_UNITS / (".kf-proxy-" + secrets.token_hex(16))
        temporary.symlink_to(unitdir / proxy_service.UNIT)
        try:
            os.replace(temporary, system)
            install.sync_directory(service_install.SYSTEM_UNITS)
        finally:
            if temporary.is_symlink():
                temporary.unlink()
    service_install.systemctl("daemon-reload")
    props = dict(
        line.split("=", 1)
        for line in service_install.systemctl(
            "show", proxy_service.UNIT, "--property=LoadState,ActiveState,MainPID,FragmentPath"
        ).splitlines()
        if "=" in line
    )
    if (
        props.get("LoadState") != "loaded"
        or props.get("ActiveState") != "inactive"
        or props.get("MainPID") != "0"
        or props.get("FragmentPath") not in {str(system), str(unitdir / proxy_service.UNIT)}
    ):
        raise DeliveryError("proxy loaded/inactive readback unconfirmed")
    return {
        "installed": True,
        "runtime_version": data["version"],
        "source_sha": receipt["source_sha"],
        "configuration_id": selected["configuration_id"],
        "tls_material_parsed": True,
        "tls_verified": False,
        "services_started": False,
        "execution_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed protected HTTPS installation")
    parser.add_argument("--check-binding", action="store_true")
    parser.add_argument("--release-id")
    parser.add_argument("--configuration-id")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--certificate", type=Path)
    parser.add_argument("--private-key", type=Path)
    args = parser.parse_args()
    try:
        if args.check_binding:
            if (
                not args.release_id
                or not args.configuration_id
                or any((args.source, args.receipt_sha256, args.certificate, args.private_key))
            ):
                raise DeliveryError("explicit binding-only arguments required")
            result = check_binding(args.release_id, args.configuration_id)
        else:
            if any((args.release_id, args.configuration_id)) or not all(
                (args.source, args.receipt_sha256, args.certificate, args.private_key)
            ):
                raise DeliveryError("explicit installation inputs required")
            result = install_proxy(
                args.source, args.receipt_sha256, args.certificate, args.private_key
            )
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            "Proxy installation refused: "
            + (str(error) if isinstance(error, DeliveryError) else type(error).__name__)
            + "\n",
        )


if __name__ == "__main__":
    main()
