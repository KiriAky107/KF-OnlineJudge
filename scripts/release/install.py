"""Root operator file installation, independent of migrations and service start."""

import os
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

from . import manifest, preflight
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256

RELEASES = Path("/opt/kf-onlinejudge/releases")
CURRENT = Path("/opt/kf-onlinejudge/current")
IMAGES = Path("/var/lib/kf-judge/images")
ACTIVE = Path("/etc/kf-onlinejudge/active.json")
APPLICATION_UNITS = (
    "kf-api.service",
    "kf-coordinator.service",
    "kf-outbox.service",
    "kf-problem-import.service",
    "kf-worker.service",
    "kf-sandbox-factory.service",
    "kf-proxy.service",
)
LEGACY_HEADER = {"image_kind": "linux_gnu_cxx_x86_64_v1", "architecture": "x86_64", "revision": 1}


def directory(path: Path, mode: int) -> None:
    if not path.exists() and not path.is_symlink():
        preflight.protected(path.parent, directory=True)
        path.mkdir(mode=mode)
    preflight.protected(path, directory=True)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        raise DeliveryError("existing directory mode differs; operator review required")


def exclusive_file(path: Path, data: bytes, mode: int) -> None:
    preflight.protected(path.parent, directory=True)
    if path.exists() or path.is_symlink():
        preflight.protected(path)
        if stat.S_IMODE(path.stat().st_mode) != mode or path.read_bytes() != data:
            raise DeliveryError("existing installed file differs; refused overwrite")
        return
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_verified(source: Path, target: Path, expected: str, mode: int) -> None:
    preflight.protected(source)
    if digest(source) != sha256(expected):
        raise DeliveryError("source changed after bundle verification")
    if target.exists() or target.is_symlink():
        preflight.protected(target)
        if digest(target) != expected or stat.S_IMODE(target.stat().st_mode) != mode:
            raise DeliveryError("existing release differs; refused overwrite")
        return
    preflight.protected(target.parent, directory=True)
    incoming = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    outgoing = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with (
            os.fdopen(incoming, "rb", closefd=False) as src,
            os.fdopen(outgoing, "wb", closefd=False) as dst,
        ):
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(outgoing)
        if digest(target) != expected:
            raise DeliveryError("installed bytes differ; incomplete release retained")
        os.fchmod(outgoing, mode)
    finally:
        os.close(incoming)
        os.close(outgoing)


def ensure_accounts() -> None:
    if preflight.identity_conflicts():
        raise DeliveryError("OS identity conflicts")
    import grp
    import pwd

    for name, identity in preflight.ACCOUNTS.items():
        try:
            grp.getgrnam(name)
        except KeyError:
            subprocess.run(
                ["/usr/bin/groupadd", "--gid", str(identity), name],
                check=True,
                timeout=10,
                capture_output=True,
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
                    "/nonexistent",
                    "--shell",
                    "/usr/bin/nologin",
                    name,
                ],
                check=True,
                timeout=10,
                capture_output=True,
            )
    if preflight.identity_conflicts():
        raise DeliveryError("OS identity readback differs")


def data_directory(path: Path, owner: int) -> None:
    preflight.protected(path.parent, directory=True)
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        os.chown(path, owner, owner)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner
        or info.st_gid != owner
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DeliveryError("existing private data directory identity/mode differs")


def frozen_copy(source: Path, target: Path, *, path_prefix: str = "") -> None:
    """For already verified release images, never accept external archive members."""
    records = manifest.inventory(source, path_prefix=path_prefix)
    directories = manifest.directories(source, path_prefix=path_prefix)
    if target.exists() or target.is_symlink():
        preflight.protected(target, directory=True)
        if (
            manifest.inventory(target, path_prefix=path_prefix) != records
            or manifest.directories(target, path_prefix=path_prefix) != directories
        ):
            raise DeliveryError("existing protected image differs")
        for record in records:
            preflight.protected(target / record["path"])
        return
    target.mkdir(mode=0o700)
    for name in sorted(directories, key=lambda name: (name.count("/"), name)):
        (target / name).mkdir(mode=0o700)
    for record in records:
        copy_verified(
            source / record["path"],
            target / record["path"],
            record["sha256"],
            int(record["mode"], 8),
        )
    for name in sorted(directories, key=lambda name: name.count("/"), reverse=True):
        (target / name).chmod(0o555)
        sync_directory(target / name)
    target.chmod(0o555)
    sync_directory(target)
    if (
        manifest.inventory(target, path_prefix=path_prefix) != records
        or manifest.directories(target, path_prefix=path_prefix) != directories
    ):
        raise DeliveryError("installed image readback differs")


def files(bundle: Path, expected: str) -> dict:
    if os.geteuid() != 0:
        raise DeliveryError("installer requires root")
    preflight.protected(bundle, directory=True)
    data = manifest.verify(bundle, expected)
    observation = preflight.observe(bundle / "payload", bundle_data=data)
    if not observation["eligible"]:
        raise DeliveryError("installation preflight refused: " + "; ".join(observation["errors"]))
    # Per-entry ownership matters even when the inventory is identical.
    for record in data["files"]:
        preflight.protected(bundle / "payload" / record["path"])
    for name in data["directories"]:
        preflight.protected(bundle / "payload" / name, directory=True)
    ensure_accounts()
    for name, mode in (
        ("/opt/kf-onlinejudge", 0o755),
        (str(RELEASES), 0o755),
        ("/etc/kf-onlinejudge", 0o700),
        ("/etc/kf-judge", 0o700),
        ("/var/lib/kf-onlinejudge", 0o755),
        ("/var/lib/kf-judge", 0o755),
        ("/var/lib/kf-judge/tasks", 0o755),
        (str(IMAGES), 0o755),
    ):
        directory(Path(name), mode)
    for name in ("artifacts", "imports"):
        data_directory(Path("/var/lib/kf-onlinejudge") / name, preflight.ACCOUNTS["kf-oj"])
    data_directory(Path("/var/lib/kf-onlinejudge/worker"), preflight.ACCOUNTS["kf-worker"])
    node_unit = bundle / "payload/deploy/release/kfonlinejudge.slice"
    exclusive_file(Path("/etc/systemd/system/kfonlinejudge.slice"), node_unit.read_bytes(), 0o644)
    release = RELEASES / data["release_id"]
    if release.exists():
        preflight.protected(release, directory=True)
        manifest.verify(release, expected)
        for record in data["files"]:
            preflight.protected(release / "payload" / record["path"])
    else:
        stage = RELEASES / ("." + data["release_id"] + ".installing")
        if stage.exists() or stage.is_symlink():
            raise DeliveryError("incomplete release requires explicit operator inspection")
        stage.mkdir(mode=0o700)
        # `frozen_copy` accepts names through the same bounded allowlist.
        frozen_copy(bundle / "payload", stage / "payload")
        copy_verified(bundle / "manifest.json", stage / "manifest.json", expected, 0o444)
        manifest.verify(stage, expected)
        stage.chmod(0o555)
        os.rename(stage, release)
        sync_directory(RELEASES)
    image_base = IMAGES / data["release_id"]
    directory(image_base, 0o755)
    for name in data["images"]:
        frozen_copy(
            release / "payload/images" / name, image_base / name, path_prefix="images/catalog/"
        )
    legacy = image_base / "legacy-gnu"
    if legacy.exists():
        preflight.protected(legacy, directory=True)
        if read_json(legacy / "manifest.json") != LEGACY_HEADER:
            raise DeliveryError("legacy compatibility image header differs")
        wanted = {
            r["path"]: r
            for r in manifest.inventory(image_base / "gnu", path_prefix="images/catalog/")
            if r["path"] != "manifest.json"
        }
        actual = {
            r["path"]: r
            for r in manifest.inventory(legacy, path_prefix="images/catalog/")
            if r["path"] != "manifest.json"
        }
        if wanted != actual or (
            manifest.directories(legacy, path_prefix="images/catalog/")
            != manifest.directories(image_base / "gnu", path_prefix="images/catalog/")
        ):
            raise DeliveryError("legacy compatibility image bytes differ")
    else:
        # This is an explicit v1 compatibility image, not a catalog rewrite.
        # All executable/library bytes are identical to the verified GNU image.
        frozen_copy(image_base / "gnu", legacy, path_prefix="images/catalog/")
        legacy.chmod(0o700)
        old = legacy / "manifest.json"
        old.chmod(0o600)
        old.write_bytes(canonical(LEGACY_HEADER))
        old.chmod(0o444)
        legacy.chmod(0o555)
    receipt = {
        "schema_version": 1,
        "release_id": data["release_id"],
        "source_sha": data["source_sha"],
        "manifest_sha256": expected,
        "images": data["images"],
        "legacy_header_sha256": digest(legacy / "manifest.json"),
        "files_installed": True,
        "activated": False,
        "migrated": False,
        "execution_verified": False,
    }
    exclusive_file(
        Path("/etc/kf-onlinejudge") / (data["release_id"] + ".files.json"),
        canonical(receipt),
        0o600,
    )
    return receipt


def selection(*, check_configuration: bool = True) -> dict:
    """One bounded protected binding; no process or health claim."""
    from .configuration import check_installed, installed_receipt_path, private, validate_receipt

    preflight.protected(ACTIVE)
    private(ACTIVE)
    value = fields(
        read_json(ACTIVE, 4096),
        {"schema_version", "release_id", "manifest_sha256", "configuration_id", "receipt_sha256"},
        "active binding",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["release_id"], str)
        or not re.fullmatch(r"sd-[0-9a-f]{12}", value["release_id"])
        or not isinstance(value["configuration_id"], str)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", value["configuration_id"])
    ):
        raise DeliveryError("invalid active release/configuration binding")
    sha256(value["manifest_sha256"])
    sha256(value["receipt_sha256"])
    receipt = installed_receipt_path(value["release_id"], value["configuration_id"])
    preflight.protected(receipt)
    private(receipt)
    if digest(receipt) != value["receipt_sha256"]:
        raise DeliveryError("active configuration receipt differs")
    binding = validate_receipt(read_json(receipt, 16384), installed=True)
    if any(
        binding[key] != value[key] for key in ("release_id", "configuration_id", "manifest_sha256")
    ):
        raise DeliveryError("active configuration identity differs")
    target = RELEASES / value["release_id"] / "payload"
    info = CURRENT.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or os.readlink(CURRENT) != str(target):
        raise DeliveryError("active code pointer differs from configuration binding")
    preflight.protected(target, directory=True)
    if check_configuration:
        check_installed(receipt)
    return value


def require_stopped() -> None:
    """Do not select different private identities while any app unit is alive."""
    for unit in APPLICATION_UNITS:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", unit, "--property=LoadState,ActiveState,MainPID"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": "/usr/bin", "LC_ALL": "C"},
        )
        if len(result.stdout) > 4096:
            raise DeliveryError("application stop observation exceeds limit")
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if (
            result.returncode
            or values.get("LoadState") not in {"loaded", "not-found"}
            or values.get("ActiveState") != "inactive"
            or values.get("MainPID") != "0"
        ):
            raise DeliveryError("all application units must be stopped before selection")
    delegated = Path("/sys/fs/cgroup/kfonlinejudge.slice/kf-judge-tasks")
    if delegated.exists():
        if delegated.is_symlink() or (delegated / "cgroup.procs").read_text().strip():
            raise DeliveryError("delegated execution processes remain")
        if any(path.is_dir() for path in delegated.iterdir()):
            raise DeliveryError("delegated task groups remain; cleanup proof required")


def activate(release_id: str, expected: str, batch: str) -> dict:
    """Only files + separately installed exact configuration may be selected.

    This command starts no service, migrates no table, and removes no old version.
    Runtime admission/isolation/health remains a separate bounded operation.
    """
    if (
        os.geteuid() != 0
        or not re.fullmatch(r"sd-[0-9a-f]{12}", release_id)
        or not re.fullmatch(r"cfg-[0-9a-f]{24}", batch)
    ):
        raise DeliveryError("invalid activation identity or privilege")
    release = RELEASES / release_id
    preflight.protected(release, directory=True)
    data = manifest.verify(release, expected)
    from .configuration import check_installed, installed_receipt_path

    config_receipt = installed_receipt_path(release_id, batch)
    binding = check_installed(config_receipt)
    if (
        binding.get("manifest_sha256") != expected
        or binding.get("source_sha") != data["source_sha"]
    ):
        raise DeliveryError("configuration not bound to selected release")
    require_stopped()
    for port in (8000, 8100):
        import socket

        with socket.socket() as check:
            try:
                check.bind(("127.0.0.1", port))
            except OSError as error:
                raise DeliveryError("stop and drain services before activation") from error
    if Path("/var/lib/kf-judge/factory/control.sock").exists():
        raise DeliveryError("factory socket present; explicit stop/cleanup required")
    for name in ("tasks", "identities", "captures"):
        path = Path("/var/lib/kf-judge") / name
        if path.exists():
            preflight.protected(path, directory=True)
            if next(path.iterdir(), None) is not None:
                raise DeliveryError("execution leases or task resources remain; activation refused")
    if ACTIVE.exists() or ACTIVE.is_symlink() or CURRENT.exists() or CURRENT.is_symlink():
        # Retiring a byte-bound old batch is allowed after a kernel change.
        # It is never accepted for execution under that changed environment.
        selection(check_configuration=False)
    active = {
        "schema_version": 1,
        "release_id": release_id,
        "manifest_sha256": expected,
        "configuration_id": batch,
        "receipt_sha256": digest(config_receipt),
    }
    incoming = ACTIVE.parent / (".active-" + secrets.token_hex(16))
    exclusive_file(incoming, canonical(active), 0o600)
    temporary = CURRENT.parent / (".current-" + secrets.token_hex(16))
    temporary.symlink_to(release / "payload")
    os.replace(temporary, CURRENT)
    sync_directory(CURRENT.parent)
    os.replace(incoming, ACTIVE)
    sync_directory(ACTIVE.parent)
    # A power loss between the two atomic writes leaves a detectable binding
    # mismatch. No service is started and check-active refuses that state.
    selection()
    return {
        "selected_source_sha": data["source_sha"],
        "configuration_id": batch,
        "activated": True,
        "services_started": False,
        "execution_verified": False,
    }
