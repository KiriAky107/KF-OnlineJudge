"""Trusted checkout + hash-bound offline runtime inputs -> directory bundle."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

from . import manifest
from .policy import DeliveryError, contract, digest, fields, read_json, relative_name, sha256

ENTRYPOINTS = {
    "kf-api": (
        "uvicorn kf_oj.api.app:create_app --factory --host 127.0.0.1 --port 8000 "
        "--no-proxy-headers --limit-concurrency 32 --backlog 64"
    ),
    "kf-outbox-publisher": "kf_oj.workers.outbox",
    "kf-problem-import-worker": "kf_oj.workers.problem_import",
    "kf-accounts": "kf_oj.modules.identity.cli",
    "kf-problems": "kf_oj.modules.problems.cli",
    "kf-artifacts": "kf_oj.infra.artifacts.cli",
    "kf-migrate": "alembic -c backend/alembic.ini",
}


def command(argv: list[str], cwd: Path, log: Path, env: dict | None = None) -> None:
    with log.open("ab") as output:
        output.write(("$ " + " ".join(argv) + "\n").encode())
        output.flush()
        subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=1200,
        )


def copy(source: Path, target: Path) -> None:
    info = source.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > manifest.MAX_FILE_BYTES:
        raise DeliveryError("unsupported source payload file")
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
    target.chmod(0o555 if info.st_mode & 0o111 else 0o444)


def tree(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise DeliveryError("ordinary source directory required")
    for directory, directories, names in os.walk(source, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in {"__pycache__", ".git"})
        for name in directories:
            if (Path(directory) / name).is_symlink():
                raise DeliveryError("source directory link refused")
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink():
                raise DeliveryError("source file link refused")
            if name.endswith((".pyc", ".pyo")) or name in {"_virtualenv.py", "_virtualenv.pth"}:
                continue
            if name.endswith(".pth"):
                raise DeliveryError("dependency startup path hook refused")
            relative = path.relative_to(source)
            relative_name(
                (target.name + "/" + relative.as_posix())
                if target.name in {"site", "frontend"}
                else "backend/" + relative.as_posix()
            )
            copy(path, target / relative)


def tracked(repo: Path, source_prefix: str, payload: Path) -> None:
    """Only tracked checkout members; never scan operator environment files."""
    names = subprocess.check_output(["git", "ls-files", "-z", source_prefix], cwd=repo)
    for raw in names.split(b"\0"):
        if not raw:
            continue
        name = raw.decode()
        relative_name(name)
        source = repo / name
        if source.is_symlink():
            raise DeliveryError("tracked payload link refused")
        copy(source, payload / name)


def curated(root: Path, records: dict, name: str, target: Path) -> None:
    """Resolve only links explicitly bound by the separate trusted inventory."""
    current = root / name
    for _ in range(9):
        relative = current.relative_to(root).as_posix()
        entry = records.get(relative)
        if entry is None:
            raise DeliveryError("runtime input absent from trusted inventory")
        if "link" not in entry:
            if (
                current.is_symlink()
                or current.stat().st_size != entry["bytes"]
                or digest(current) != entry["sha256"]
            ):
                raise DeliveryError("runtime input fingerprint differs")
            copy(current, target)
            return
        if not current.is_symlink() or os.readlink(current) != entry["link"]:
            raise DeliveryError("runtime input link differs")
        if entry["link"].startswith("/"):
            # Public Arch libraries may contain /usr/lib aliases. Interpret
            # them within the fingerprinted guest tree, never the build host.
            if not relative.startswith("services/usr/lib/") or not entry["link"].startswith(
                "/usr/lib/"
            ):
                raise DeliveryError("absolute runtime input link refused")
            current = Path(os.path.normpath(root / "services" / entry["link"].lstrip("/")))
        else:
            current = Path(os.path.normpath(current.parent / entry["link"]))
        if not current.is_relative_to(root):
            raise DeliveryError("runtime input escapes trusted root")
    raise DeliveryError("runtime input link depth limit")


def image(source: Path, expected: str, target: Path) -> dict:
    sha256(expected)
    if source.is_symlink() or digest(source / "manifest.json") != expected:
        raise DeliveryError("runtime catalog fingerprint differs")
    catalog = read_json(source / "manifest.json")
    identity = {
        name: catalog[name]
        for name in ("toolchain_id", "image_kind", "image_revision", "runtime_version")
    }
    identity["image_digest"] = expected
    if catalog.get("format_version") != 2 or catalog.get("architecture") != "x86_64_v3":
        raise DeliveryError("unsupported image catalog")
    records = catalog.get("files")
    directories = catalog.get("directories")
    if (
        not isinstance(records, list)
        or not 1 <= len(records) <= 10000
        or not isinstance(directories, list)
        or len(directories) > 10000
    ):
        raise DeliveryError("image catalog bounds")
    target.mkdir(parents=True)
    for name in directories:
        relative_name("images/catalog/" + name + "/.__directory__")
        original = source / name
        if original.is_symlink() or not original.is_dir():
            raise DeliveryError("runtime directory differs from catalog")
        (target / name).mkdir(parents=True, exist_ok=True)
    for item in records:
        name = item["path"]
        relative_name("images/catalog/" + name)
        original = source / name
        if (
            original.is_symlink()
            or original.stat().st_size != item["bytes"]
            or digest(original) != item["sha256"]
        ):
            raise DeliveryError("runtime file differs from catalog")
        copy(original, target / name)
    copy(source / "manifest.json", target / "manifest.json")
    return identity


def build(repo: Path, inputs: Path, output: Path, work: Path) -> dict:
    repo = repo.resolve(strict=True)
    inputs = inputs.resolve(strict=True)
    output = output.resolve()
    work = work.resolve()
    if output == work or output.is_relative_to(work) or work.is_relative_to(output):
        raise DeliveryError("build output and work must be separate")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=repo):
        raise DeliveryError("build requires a clean committed checkout")
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    contract(repo / "deploy/release/contract.v1.json")
    fields_spec = {
        "schema_version",
        "runtime_root",
        "runtime_manifest",
        "runtime_manifest_sha256",
        "images",
    }
    spec = fields(read_json(inputs, 16384), fields_spec, "build inputs")
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise DeliveryError("unsupported build inputs")
    root = Path(spec["runtime_root"]).resolve(strict=True)
    trusted_manifest = Path(spec["runtime_manifest"])
    if digest(trusted_manifest) != sha256(spec["runtime_manifest_sha256"]):
        raise DeliveryError("runtime inventory fingerprint differs")
    runtime_records = read_json(trusted_manifest).get("files")
    if not isinstance(runtime_records, list) or len(runtime_records) > 50000:
        raise DeliveryError("runtime inventory bounds")
    records = {entry["path"]: entry for entry in runtime_records}
    if len(records) != len(runtime_records):
        raise DeliveryError("duplicate runtime inventory member")
    fields(spec["images"], {"gnu", "python313", "java21"}, "build images")
    if output.exists() or output.is_symlink() or work.exists() or work.is_symlink():
        raise DeliveryError("build output/work directories must be new")
    if shutil.disk_usage(output.parent).free < 8 * 1024**3:
        raise DeliveryError("build disk reserve below 8 GiB")
    output.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    payload = output / "payload"
    payload.mkdir()
    locks = {
        name: digest(repo / name)
        for name in (
            "backend/uv.lock",
            "judge/Cargo.lock",
            "frontend/pnpm-lock.yaml",
            "deploy/release/contract.v1.json",
        )
    }
    log = work / "build.log"
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(work / "venv")
    env["CARGO_BUILD_JOBS"] = "2"
    for argv, cwd in [
        (
            [
                "uv",
                "sync",
                "--locked",
                "--no-dev",
                "--no-install-project",
                "--no-build",
                "--offline",
                "--no-python-downloads",
                "--python",
                "3.13.16",
            ],
            repo / "backend",
        ),
        (
            ["pnpm", "install", "--frozen-lockfile", "--offline", "--ignore-scripts"],
            repo / "frontend",
        ),
        (["pnpm", "build"], repo / "frontend"),
        (
            [
                str(Path.home() / ".cargo/bin/cargo"),
                "build",
                "--release",
                "--workspace",
                "--locked",
                "--offline",
            ],
            repo / "judge",
        ),
    ]:
        command(argv, cwd, log, env)
    command([str(work / "venv/bin/python"), "--version"], repo, log)
    command(["node", "--version"], repo / "frontend", log)
    command(["pnpm", "--version"], repo / "frontend", log)
    command([str(Path.home() / ".cargo/bin/rustc"), "--version"], repo / "judge", log)
    actual = {
        "python": subprocess.check_output([str(work / "venv/bin/python"), "--version"], text=True)
        .strip()
        .removeprefix("Python "),
        "node": subprocess.check_output(["node", "--version"], cwd=repo / "frontend", text=True)
        .strip()
        .removeprefix("v"),
        "pnpm": subprocess.check_output(
            ["pnpm", "--version"], cwd=repo / "frontend", text=True
        ).strip(),
        "rust": subprocess.check_output(
            [str(Path.home() / ".cargo/bin/rustc"), "--version"], cwd=repo / "judge", text=True
        ).split()[1],
    }
    if actual != manifest.TOOLCHAINS:
        raise DeliveryError("actual build toolchain differs from contract")
    tracked(repo, "backend/src", payload)
    tracked(repo, "backend/migrations", payload)
    copy(repo / "backend/alembic.ini", payload / "backend/alembic.ini")
    tree(work / "venv/lib/python3.13/site-packages", payload / "site")
    tree(repo / "frontend/dist", payload / "frontend")
    tracked(repo, "deploy", payload)
    for name in ("kf-sandbox", "kf-worker", "kf-coordinator"):
        copy(repo / "judge/target/release" / name, payload / "bin" / name)
    for name in sorted(records):
        if name.startswith("python/lib/") or name == "python/bin/python3.13":
            if name.endswith((".pyc", ".pyo", ".a")) or any(
                p
                in {
                    "site-packages",
                    "__pycache__",
                    "test",
                    "tests",
                    "ensurepip",
                    "tkinter",
                    "idlelib",
                }
                for p in Path(name).parts
            ):
                continue
            relative_name(name)
            curated(root, records, name, payload / name)
        elif name.startswith("services/usr/lib/"):
            relative = name.removeprefix("services/usr/lib/")
            if "/" not in relative and (".so" in relative or relative == "ld-linux-x86-64.so.2"):
                relative_name("lib/" + relative)
                curated(root, records, name, payload / "lib" / relative)
    images = {}
    for name, entry in spec["images"].items():
        fields(entry, {"root", "catalog_sha256"}, "image build input")
        images[name] = image(
            Path(entry["root"]), entry["catalog_sha256"], payload / "images" / name
        )
    # The service inventory is an adjunct to the base GNU userland. Bind the
    # latter's libc/loader/compiler runtime closure too, rather than relying on
    # undeclared build-host libraries. The image was byte-verified above.
    for library in sorted((payload / "images/gnu/usr/lib").iterdir()):
        if not library.is_file() or ".so" not in library.name:
            continue
        target = payload / "lib" / library.name
        if target.exists():
            if digest(target) != digest(library):
                raise DeliveryError("conflicting verified service library")
        else:
            copy(library, target)
    for name, module in ENTRYPOINTS.items():
        wrapper = (
            "#!/bin/sh\nset -eu\n"
            'APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\n'
            'export PYTHONHOME="$APP_ROOT/python"\n'
            'export PYTHONPATH="$APP_ROOT/backend/src:$APP_ROOT/site"\n'
            "export PYTHONDONTWRITEBYTECODE=1\n"
            'export LD_LIBRARY_PATH="$APP_ROOT/lib:$APP_ROOT/python/lib"\n'
            'cd "$APP_ROOT"\n'
            'exec "$APP_ROOT/python/bin/python3.13" -s -P -m ' + module + ' "$@"\n'
        )
        target = payload / "bin" / name
        target.write_text(wrapper)
        target.chmod(0o555)
    for directory, directories, _ in os.walk(payload, topdown=False):
        for name in directories:
            (Path(directory) / name).chmod(0o555)
    payload.chmod(0o555)
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=repo):
        raise DeliveryError("source changed during build")
    data = manifest.write(output, source_sha, locks, images)
    manifest.verify(output, digest(output / "manifest.json"))
    return {
        "source_sha": source_sha,
        "manifest_sha256": digest(output / "manifest.json"),
        "payload_bytes": data["payload_bytes"],
        "files": len(data["files"]),
        "installed": False,
        "execution_verified": False,
    }
