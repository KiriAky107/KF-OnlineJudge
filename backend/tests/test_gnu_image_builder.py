"""Offline package data only; no compiler, submitted source or image execution."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "gnu_image_builder", Path(__file__).resolve().parents[2] / "scripts/build_gnu_image.py"
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def test_absolute_package_link_is_relocated_inside_verified_image(tmp_path):
    root = tmp_path / "image"
    target = root / "usr/lib/plugin.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"synthetic pinned package bytes")
    link = root / "usr/lib/plugin-link.so"
    link.symlink_to("/usr/lib/plugin.so")
    records = {
        "usr/lib/plugin-link.so": {"kind": "symlink", "target": "/usr/lib/plugin.so"},
        "usr/lib/plugin.so": {"kind": "file", "sha256": builder.sha(target)},
    }
    source, record = builder.resolve_verified(root, records, "usr/lib/plugin-link.so")
    assert source == target
    assert source.read_bytes() == b"synthetic pinned package bytes"
    assert record == records["usr/lib/plugin.so"]


@pytest.mark.parametrize("target", ["../../../../outside", "plugin-link.so"])
def test_package_link_escape_or_cycle_refused(tmp_path, target):
    link = tmp_path / "usr/lib/plugin-link.so"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    records = {"usr/lib/plugin-link.so": {"kind": "symlink", "target": target}}
    with pytest.raises(ValueError):
        builder.resolve_verified(tmp_path, records, "usr/lib/plugin-link.so")


def test_replaced_or_unverified_ancestor_link_is_refused(tmp_path):
    library = tmp_path / "usr/lib"
    library.parent.mkdir()
    library.symlink_to("/usr/lib")
    records = {"usr/lib/plugin.so": {"kind": "file", "sha256": "a" * 64}}
    with pytest.raises(ValueError, match="unverified GNU ancestor"):
        builder.resolve_verified(tmp_path, records, "usr/lib/plugin.so")
    records["usr/lib"] = {"kind": "symlink", "target": "/different/path"}
    with pytest.raises(ValueError, match="differs from verified manifest"):
        builder.resolve_verified(tmp_path, records, "usr/lib/plugin.so")


def test_verified_manifest_does_not_authorize_changed_package_bytes(tmp_path):
    root = tmp_path / "support"
    library = root / "usr/lib/ld-linux-x86-64.so.2"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"original synthetic library")
    manifest = {
        "packages": [
            {
                "name": "gcc",
                "version": "16.2.1+r23+gd564253eb6c8-1",
                "sha256": "ebac12ceef494c616a36fc6f0f792b365dc59b6ed46ca1a69f138aecc810b6ab",
            },
            {
                "name": "glibc",
                "version": "2.44+r50+g1848099f063e-3",
                "sha256": "071d807f4d3bf6b921f86b9a91e83a47ecc026c5821634efa46b09d890e06785",
            },
            {
                "name": "binutils",
                "version": "2.47-2",
                "sha256": "62efd213e87590f025cf861b646793d3cacb03e7e26187b73d33b00adaf7a977",
            },
        ],
        "files": [
            {
                "path": "usr/lib/ld-linux-x86-64.so.2",
                "kind": "file",
                "bytes": library.stat().st_size,
                "sha256": builder.sha(library),
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    library.write_bytes(b"changed synthetic library")
    output = tmp_path / "output"
    args = SimpleNamespace(
        support_dir=root,
        support_manifest=manifest_path,
        support_manifest_sha256=builder.sha(manifest_path),
        output=output,
    )
    with pytest.raises(ValueError, match="differs from verified public package bytes"):
        builder.build(args)
    assert not (output / "manifest.json").exists()
    assert not list(output.rglob("*.so*"))
