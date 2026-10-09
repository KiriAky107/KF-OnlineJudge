"""Offline GNU catalog from an explicitly pinned, verified public package closure.

No target executable, package hook, submitted source or network installer runs.
Root publication and real execution acceptance remain separate steps.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def canonical(name):
    return len(name) <= 4096 and all(
        re.fullmatch(r"[A-Za-z0-9_.+\-]{1,255}", p) and p not in (".", "..")
        for p in name.split("/")
    )


def resolve_verified(root, records, name):
    parts = name.split("/")
    for _ in range(9):
        changed = False
        for i in range(len(parts)):
            prefix = "/".join(parts[: i + 1])
            record = records.get(prefix)
            path = root / prefix
            if record and record["kind"] == "symlink":
                if not path.is_symlink() or str(path.readlink()) != record["target"]:
                    raise ValueError("GNU package link differs from verified manifest")
                target = record["target"]
                expanded = (
                    ([] if target.startswith("/") else parts[:i])
                    + target.split("/")
                    + parts[i + 1 :]
                )
                parts = []
                for p in expanded:
                    if p in ("", "."):
                        continue
                    if p == "..":
                        if not parts:
                            raise ValueError("GNU image-relative link escapes closure")
                        parts.pop()
                    else:
                        parts.append(p)
                changed = True
                break
            if path.is_symlink():
                raise ValueError("unverified GNU ancestor link")
        if not changed:
            relative = "/".join(parts)
            record = records.get(relative)
            if record is None or record["kind"] != "file":
                raise ValueError("GNU link outside verified regular-file closure")
            source = root / relative
            source.resolve(strict=True).relative_to(root)
            return source, record
    raise ValueError("GNU link cycle or depth budget")


def build(args):
    if sha(args.support_manifest) != args.support_manifest_sha256:
        raise ValueError("verified GNU support manifest fingerprint differs")
    manifest = json.loads(args.support_manifest.read_bytes())
    packages = {p["name"]: p for p in manifest["packages"]}
    expected = {
        "gcc": (
            "16.2.1+r23+gd564253eb6c8-1",
            "ebac12ceef494c616a36fc6f0f792b365dc59b6ed46ca1a69f138aecc810b6ab",
        ),
        "glibc": (
            "2.44+r50+g1848099f063e-3",
            "071d807f4d3bf6b921f86b9a91e83a47ecc026c5821634efa46b09d890e06785",
        ),
        "binutils": ("2.47-2", "62efd213e87590f025cf861b646793d3cacb03e7e26187b73d33b00adaf7a977"),
    }
    for name, (version, digest) in expected.items():
        if (packages[name]["version"], packages[name]["sha256"]) != (version, digest):
            raise ValueError("GNU image requires the pinned compiler/libc/linker closure")
    records = {f["path"]: f for f in manifest["files"]}
    source_root = args.support_dir.resolve(strict=True)
    if args.output.exists():
        raise ValueError("refuse to replace an image")
    args.output.mkdir(mode=0o700)
    directories = {"dev", "proc", "tmp", "work", "usr", "lib64"}
    files = []

    def put(name, source, expected_file):
        if not canonical(name) or expected_file["kind"] != "file":
            raise ValueError("noncanonical verified GNU file")
        if (
            source.stat().st_size != expected_file["bytes"]
            or sha(source) != expected_file["sha256"]
        ):
            raise ValueError("GNU file differs from verified public package bytes")
        if len(files) >= 4096 or sum(f["bytes"] for f in files) + source.stat().st_size > 1024**3:
            raise ValueError("GNU image exceeds finite catalog budget")
        target = args.output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy to a fresh regular inode; never publish source links/hard links.
        with source.open("rb") as reader, target.open("xb") as writer:
            while data := reader.read(1024 * 1024):
                writer.write(data)
        executable = bool(source.stat().st_mode & 0o111)
        target.chmod(0o555 if executable else 0o444)
        if sha(target) != expected_file["sha256"]:
            raise ValueError("GNU copy fingerprint differs")
        files.append(
            {
                "path": name,
                "bytes": target.stat().st_size,
                "sha256": sha(target),
                "executable": executable,
            }
        )
        directories.update(p.as_posix() for p in Path(name).parents if p != Path("."))

    for name, record in sorted(records.items()):
        if not (
            name.startswith(("usr/include/", "usr/lib/"))
            or name in ("usr/bin/gcc", "usr/bin/g++", "usr/bin/as", "usr/bin/ld")
        ):
            continue
        if record["kind"] == "directory":
            continue
        source, real = resolve_verified(source_root, records, name)
        put(name, source, real)
    loader, loader_record = resolve_verified(source_root, records, "usr/lib/ld-linux-x86-64.so.2")
    put("lib64/ld-linux-x86-64.so.2", loader, loader_record)
    for name in directories:
        (args.output / name).mkdir(parents=True, exist_ok=True)
    catalog = dict(
        format_version=2,
        architecture="x86_64_v3",
        image_kind="linux_gnu_cxx_x86_64_v1",
        image_revision=1,
        toolchain_id="gnu-cxx-v1",
        runtime_version="16.2.1+r23+gd564253eb6c8",
        directories=sorted(directories),
        files=sorted(files, key=lambda f: f["path"]),
    )
    raw = (json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > 1024 * 1024:
        raise ValueError("GNU catalog transport budget")
    (args.output / "manifest.json").write_bytes(raw)
    (args.output / "manifest.json").chmod(0o444)
    for path in sorted((p for p in args.output.rglob("*") if p.is_dir()), reverse=True):
        path.chmod(0o555)
    args.output.chmod(0o555)
    print(
        json.dumps(
            dict(
                image_digest=hashlib.sha256(raw).hexdigest(),
                files=len(files),
                bytes=sum(f["bytes"] for f in files),
                support_manifest_sha256=args.support_manifest_sha256,
                source_execution=False,
                root_publication_and_VM_acceptance=False,
            )
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--support-dir", required=True, type=Path)
    parser.add_argument("--support-manifest", required=True, type=Path)
    parser.add_argument("--support-manifest-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
