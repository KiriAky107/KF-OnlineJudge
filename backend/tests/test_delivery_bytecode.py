"""Ordinary owned source/cache checks; no service, isolation or recovery claims."""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
bytecode = importlib.import_module("scripts.release.bytecode")
manifest = importlib.import_module("scripts.release.manifest")
policy = importlib.import_module("scripts.release.policy")


@pytest.fixture
def compiled(tmp_path):
    source = tmp_path / "site/toy.py"
    source.parent.mkdir()
    source.write_text("VALUE = (17, 'ordinary trusted fixture')\n")
    source.chmod(0o444)
    assert bytecode.compile_payload(tmp_path, "sd-" + "b" * 12) == 1
    cache = source.parent / "__pycache__/toy.cpython-313.pyc"
    return tmp_path, source, cache


def test_complete_cache_inventory_and_actual_import_without_source_compilation(compiled):
    payload, source, cache = compiled
    records = manifest.inventory(payload)
    assert {r["path"] for r in records} == {
        "site/toy.py",
        "site/__pycache__/toy.cpython-313.pyc",
    }
    assert all(r["mode"] == "0444" for r in records)
    # Only this tiny trusted fixture executes. An attempted source compilation
    # fails, so success demonstrates CPython actually consumes the checked cache.
    code = """import builtins,sys
sys.path.insert(0,sys.argv[1])
def refuse(*args,**kwargs): raise AssertionError('unexpected source compilation')
builtins.compile=refuse
import toy
assert toy.VALUE==(17,'ordinary trusted fixture')
"""
    before = {p: bytecode._stamp(p.lstat()) for p in (source, cache)}
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(source.parent)],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert {p: bytecode._stamp(p.lstat()) for p in before} == before


def test_source_change_cannot_reuse_cache_even_with_restored_mtime(compiled):
    payload, source, _ = compiled
    before = source.stat()
    source.chmod(0o600)
    source.write_text("VALUE = (18, 'ordinary trusted fixture')\n")
    source.chmod(0o444)
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(policy.DeliveryError, match="bytecode source hash differs"):
        manifest.inventory(payload)


@pytest.mark.parametrize("offset,value", [(0, 0), (4, 0), (4, 1), (4, 2)])
def test_wrong_magic_timestamp_and_unchecked_hash_caches_are_refused(compiled, offset, value):
    payload, _, cache = compiled
    data = bytearray(cache.read_bytes())
    data[offset] = value
    cache.chmod(0o600)
    cache.write_bytes(data)
    cache.chmod(0o444)
    with pytest.raises(policy.DeliveryError, match="only pinned checked-hash"):
        manifest.inventory(payload)


def test_builder_refuses_inherited_cache_and_does_not_write_to_external_prefix(
    compiled, monkeypatch
):
    payload, _, cache = compiled
    before = cache.read_bytes()
    monkeypatch.setattr(sys, "pycache_prefix", str(payload / "outside"))
    with pytest.raises(policy.DeliveryError, match="inherited bytecode cache"):
        bytecode.compile_payload(payload, "sd-" + "b" * 12)
    assert cache.read_bytes() == before and not (payload / "outside").exists()


@pytest.mark.parametrize(
    "name",
    [
        "site/toy.pyc",
        "site/__pycache__/toy.cpython-314.pyc",
        "site/__pycache__/toy.cpython-313.opt-1.pyc",
        "site/__pycache__/toy.py",
        "site/__pycache__/nested/__pycache__/toy.cpython-313.pyc",
        "site/../__pycache__/toy.cpython-313.pyc",
        "images/python313/__pycache__/toy.cpython-313.pyc",
        "frontend/__pycache__/toy.cpython-313.pyc",
    ],
)
def test_cache_allowlist_keeps_wrong_versions_domains_and_paths_closed(name):
    with pytest.raises(policy.DeliveryError):
        policy.relative_name(name)


@pytest.mark.parametrize("prefix", policy.BYTECODE_ROOTS)
def test_only_source_adjacent_pinned_cache_paths_are_allowed(prefix):
    name = prefix + "package/__pycache__/toy.cpython-313.pyc"
    assert policy.relative_name(name) == name
    assert policy.bytecode_source(name) == prefix + "package/toy.py"
    directory = prefix + "package/__pycache__/.__directory__"
    assert policy.relative_name(directory) == directory
