"""Offline curated directory images from pinned public archives and verified libraries.

Build as an ordinary user. Publish as root with 0555 directories / 0444 data /
0555 executables in a private VM; the resulting catalog fingerprint belongs in
the protected runtime config. This tool never executes the target runtimes,
submitted source, package hooks, or network installers.
"""

import argparse
import gzip
import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

PINS = {
    "python313": {
        "sha256": "4595c5589fff7bf0cb158d9a88a797e0d791fa33830770fcb7bf3f4b104feeae",
        "version": "3.13.16",
        "prefix": "python/",
        "toolchain_id": "cpython313-v1",
        "kind": "linux_cpython313_x86_64_v1",
        "source": "https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.13.16%2B20261003-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
    },
    "java21": {
        "sha256": "ce79869e1307ed8ee1e2baa86a412b1eb5b75d10a01006d788a6f968bcfaee94",
        "version": "21.0.12.1+1-LTS",
        "prefix": "jdk-21.0.12.1+1/",
        "toolchain_id": "openjdk21-v1",
        "kind": "linux_openjdk21_x86_64_v1",
        "source": "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-jdk_x64_linux_hotspot_21.0.12.1_1.tar.gz",
    },
}


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def canonical(name):
    return (
        len(name) <= 4096
        and not name.startswith("/")
        and all(
            re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", p) and p not in (".", "..")
            for p in name.split("/")
        )
    )


def member_bytes(archive, members, name, seen=()):
    if name in seen or len(seen) > 8 or name not in members:
        raise ValueError("public archive link outside bounded member set")
    item = members[name]
    if item.issym() or item.islnk():
        parts = []
        target = PurePosixPath(item.linkname)
        if target.is_absolute():
            raise ValueError("absolute public archive link")
        target = target if item.islnk() else PurePosixPath(name).parent / target
        for part in target.parts:
            if part == "..":
                if not parts:
                    raise ValueError("public archive link escapes root")
                parts.pop()
            elif part != ".":
                parts.append(part)
        return member_bytes(archive, members, "/".join(parts), (*seen, name))
    if not item.isfile() or item.size > 256 * 1024 * 1024:
        raise ValueError("unsupported public archive member")
    with archive.extractfile(item) as source:
        data = source.read(item.size + 1)
    if len(data) != item.size:
        raise ValueError("public archive length")
    return data, bool(item.mode & 0o111)


def put(root, name, data, executable=False):
    if not canonical(name):
        raise ValueError("noncanonical image path")
    target = root / name
    if target.exists():
        raise ValueError("duplicate image path")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o555 if executable else 0o444)


def selected(language, relative):
    if language == "python313":
        if relative == "bin/python3.13":
            return "usr/bin/python3.13"
        if relative.startswith("lib/"):
            if not relative.startswith("lib/python3.13/") and relative not in {
                "lib/libpython3.so",
                "lib/libpython3.13.so",
                "lib/libpython3.13.so.1.0",
            }:
                return None
            parts = PurePosixPath(relative).parts
            if any(
                p
                in {
                    "site-packages",
                    "__pycache__",
                    "ensurepip",
                    "test",
                    "tests",
                    "idlelib",
                    "tkinter",
                }
                for p in parts
            ):
                return None
            if relative.endswith((".pyc", ".pth", ".a")) or "_tkinter" in relative:
                return None
            if relative.endswith(("/sitecustomize.py", "/usercustomize.py")):
                return None
            return "usr/" + relative
        return None
    if relative in {"bin/java", "bin/javac", "release", "NOTICE"}:
        return "usr/lib/jvm/" + relative
    if relative.startswith(("lib/", "conf/", "legal/")):
        if relative.endswith("src.zip") or PurePosixPath(relative).name in {
            "libawt_xawt.so",
            "libjawt.so",
            "libsplashscreen.so",
            "libjsound.so",
        }:
            return None
        return "usr/lib/jvm/" + relative
    return None


def support_file(args, name, records):
    root = args.support_dir.resolve(strict=True)
    source = (root / "usr/lib" / name).resolve(strict=True)
    relative = source.relative_to(root).as_posix()
    expected = records.get(relative)
    if expected is None or expected.get("kind") != "file" or sha(source) != expected["sha256"]:
        raise ValueError("support library differs from verified public manifest")
    return source.read_bytes()


def dependencies(path):
    with path.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            return []
    result = subprocess.run(
        ["readelf", "-d", str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return re.findall(r"\(NEEDED\).*\[([^\]]+)\]", result.stdout)


def build(args):
    pin = PINS[args.language]
    if sha(args.archive) != pin["sha256"]:
        raise ValueError("runtime archive does not match fixed upstream digest")
    if sha(args.support_manifest) != args.support_manifest_sha256:
        raise ValueError("support manifest fingerprint")
    support = json.loads(args.support_manifest.read_text())
    records = {v["path"]: v for v in support["files"]}
    root = args.output
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    # Inflate once into a bounded anonymous file for random member/link reads.
    # No archive member path is extracted during this conversion.
    with tempfile.TemporaryFile() as unpacked:
        with gzip.open(args.archive, "rb") as compressed:
            total = 0
            while data := compressed.read(1024 * 1024):
                total += len(data)
                if total > 512 * 1024 * 1024:
                    raise ValueError("public archive inflated byte ceiling")
                unpacked.write(data)
        unpacked.seek(0)
        with tarfile.open(fileobj=unpacked, mode="r:") as archive:
            copy_archive(args, pin, root, archive)
    if args.language == "python313":
        checker = (
            Path(__file__).resolve().parents[1] / "judge/crates/sandbox/runtime/python_check.py"
        )
        put(root, "usr/lib/kf/python_check.py", checker.read_bytes())
    finish(args, pin, root, records)


def copy_archive(args, pin, root, archive):
    members = {}
    for item in archive.getmembers():
        name = item.name.rstrip("/")
        if (
            name in members
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
        ):
            raise ValueError("duplicate/noncanonical public archive member")
        members[name] = item
    for name in sorted(members):
        relative = name.removeprefix(pin["prefix"])
        if relative == name or members[name].isdir():
            continue
        destination = selected(args.language, relative)
        if destination is not None:
            data, executable = member_bytes(archive, members, name)
            put(root, destination, data, executable)


def finish(args, pin, root, records):
    # Flatten immutable image-local loader/libs; no symlink or host source is
    # retained. Resolve ELF requirements from the image plus verified support.
    put(
        root,
        "lib64/ld-linux-x86-64.so.2",
        support_file(args, "ld-linux-x86-64.so.2", records),
        True,
    )
    known = {p.name for p in root.rglob("*") if p.is_file()}
    pending = [p for p in root.rglob("*") if p.is_file()]
    copied = []
    while pending:
        source = pending.pop()
        for name in dependencies(source):
            if name.startswith("$ORIGIN/"):
                target = (source.parent / name.removeprefix("$ORIGIN/")).resolve(strict=True)
                relative = target.relative_to(root.resolve()).as_posix()
                if not canonical(relative) or not target.is_file() or target.is_symlink():
                    raise ValueError("image-relative ELF dependency is not a listed regular file")
                continue
            if not canonical(name) or "/" in name:
                raise ValueError("noncanonical ELF dependency")
            if name in known:
                continue
            data = support_file(args, name, records)
            put(root, "usr/lib/" + name, data, True)
            known.add(name)
            copied.append({"path": "usr/lib/" + name, "sha256": hashlib.sha256(data).hexdigest()})
            pending.append(root / "usr/lib" / name)
    for anchor in ["work", "tmp", "proc", "dev"]:
        (root / anchor).mkdir()
    files = []
    directories = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not canonical(relative):
            raise ValueError("unexpected image inode/path")
        if path.is_file():
            files.append(
                {
                    "path": relative,
                    "bytes": path.stat().st_size,
                    "sha256": sha(path),
                    "executable": bool(path.stat().st_mode & 0o111),
                }
            )
        elif path.is_dir():
            directories.append(relative)
        else:
            raise ValueError("special image inode")
    if len(files) > 4096 or sum(f["bytes"] for f in files) > 1024**3:
        raise ValueError("image exceeds catalog capacity")
    catalog = {
        "format_version": 2,
        # The signed support-library closure requires the x86-64-v3 ISA.
        # Publish that minimum explicitly; Root checks it before executing ELF.
        "architecture": "x86_64_v3",
        "image_kind": pin["kind"],
        "image_revision": 1,
        "toolchain_id": pin["toolchain_id"],
        "runtime_version": pin["version"],
        "directories": sorted(directories),
        "files": sorted(files, key=lambda f: f["path"]),
    }
    data = (
        json.dumps(catalog, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if len(data) > 1024**2:
        raise ValueError("catalog exceeds bounded transport")
    put(root, "manifest.json", data)
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        directory.chmod(0o555)
    root.chmod(0o555)
    identity = {
        "toolchain_id": pin["toolchain_id"],
        "image_kind": pin["kind"],
        "image_revision": 1,
        "runtime_version": pin["version"],
        "image_digest": hashlib.sha256(data).hexdigest(),
    }
    print(
        json.dumps(
            {
                "runtime_identity": identity,
                "archive": pin,
                "support_manifest_sha256": args.support_manifest_sha256,
                "files": len(files),
                "bytes": sum(f["bytes"] for f in files),
                "support_libraries": copied,
                "source_execution": False,
                "root_publication_and_VM_acceptance": False,
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("language", choices=PINS)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--support-dir", required=True, type=Path)
    parser.add_argument("--support-manifest", required=True, type=Path)
    parser.add_argument("--support-manifest-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
