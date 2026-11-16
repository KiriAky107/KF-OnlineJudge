"""Ordinary pipe fixtures only; no Root, filesystem-copy or VM acceptance."""

import json
import os
import stat
import subprocess
import sys
import time
import weakref

import pytest
from scripts.release import restore_io, restore_native
from scripts.release.policy import DeliveryError


@pytest.fixture
def transport(tmp_path, monkeypatch):
    helper = tmp_path / "bin/kf-restore-io"
    helper.parent.mkdir()
    helper.write_bytes(b"ordinary metadata fixture, never executed\n")
    helper.chmod(0o555)
    monkeypatch.setattr(restore_native.preflight, "protected", lambda *_a, **_kw: None)
    original = subprocess.Popen
    children = []

    def start(program):
        def popen(args, **options):
            assert args == [str(helper), "--stdio-v1"]
            assert options["start_new_session"] and options["close_fds"]
            assert set(options["env"]) == {"PATH", "LC_ALL", "LD_LIBRARY_PATH"}
            child = original([sys.executable, "-c", program], **options)
            children.append(child)
            return child

        monkeypatch.setattr(restore_native.subprocess, "Popen", popen)

    yield tmp_path, start, children
    assert all(p.poll() is not None for p in children)
    assert all(h.closed for p in children for h in (p.stdin, p.stdout, p.stderr))


def test_transport_counts_and_child_join_without_execution_claim(transport):
    payload, start, children = transport
    start("""
import json,sys
count=total=batch=0
for line in sys.stdin:
 value=json.loads(line)
 kind=value['kind']
 stage='opened'
 if kind=='restore.io.batch':
  count+=len(value['files'])
  total+=sum(r['bytes'] for r in value['files'])
  batch=value['batch_id']
  stage='verified'
 if kind=='restore.io.finish':stage='finished'
 reply={'schema_version':1,'kind':'restore.io.reply','stage':stage,'batch_id':batch,'verified_files':count,'verified_logical_bytes':total,'execution_verified':False,'error':'none'}
 print(json.dumps(reply),flush=True)
 if stage=='finished':break
""")
    session = restore_native.Session(
        payload, "bk-" + "1" * 32, "rr-" + "2" * 32, 2, 7, time.monotonic() + 10
    )
    try:
        session.materialize(
            [
                {"path": "artifacts/a", "bytes": 3, "sha256": "1" * 64},
                {"path": "artifacts/b", "bytes": 4, "sha256": "2" * 64},
            ]
        )
        session.finish()
        assert session.finished and children[0].returncode == 0
    finally:
        session.close()


@pytest.mark.parametrize(
    "response",
    [
        b"",
        b"not-json\n",
        b"{}\n{}\n",
        b"x" * (64 * 1024 + 1),
        json.dumps(
            {
                "schema_version": 1,
                "kind": "restore.io.reply",
                "stage": "opened",
                "batch_id": 0,
                "verified_files": 0,
                "verified_logical_bytes": 0,
                "execution_verified": True,
                "error": "none",
            }
        ).encode()
        + b"\n",
    ],
)
def test_transport_refuses_eof_malformed_unbounded_or_authority_reply(transport, response):
    payload, start, _ = transport
    start(
        "import sys;sys.stdin.readline();sys.stdout.buffer.write("
        + repr(response)
        + ");sys.stdout.flush()"
    )
    with pytest.raises(DeliveryError):
        restore_native.Session(
            payload, "bk-" + "1" * 32, "rr-" + "2" * 32, 1, 0, time.monotonic() + 10
        )


def test_transport_deadline_reaps_its_owned_waiting_child(transport):
    payload, start, _ = transport
    start("import sys,time;sys.stdin.readline();time.sleep(10)")
    then = time.monotonic()
    with pytest.raises(DeliveryError):
        restore_native.Session(payload, "bk-" + "1" * 32, "rr-" + "2" * 32, 1, 0, then + 0.2)
    assert time.monotonic() - then < 5


@pytest.mark.parametrize("owned", [False, True])
def test_owned_planner_spool_preserves_transport_counts_and_join(transport, owned):
    payload, start, children = transport
    start("""
import json,sys
count=total=batch=0
for line in sys.stdin:
 value=json.loads(line)
 kind=value['kind']
 stage='opened'
 if kind=='restore.io.batch':
  count+=len(value['files'])
  total+=sum(r['bytes'] for r in value['files'])
  batch=value['batch_id']
  stage='verified'
 if kind=='restore.io.finish':stage='finished'
 reply={'schema_version':1,'kind':'restore.io.reply','stage':stage,'batch_id':batch,'verified_files':count,'verified_logical_bytes':total,'execution_verified':False,'error':'none'}
 print(json.dumps(reply),flush=True)
 if stage=='finished':break
""")
    rows = [{"path": f"artifacts/{n:04}", "bytes": n, "sha256": "1" * 64} for n in range(513)]
    session = restore_native.Session(
        payload,
        "bk-" + "1" * 32,
        "rr-" + "2" * 32,
        len(rows),
        sum(r["bytes"] for r in rows),
        time.monotonic() + 10,
    )
    try:
        if owned:
            session.materialize_owned(rows, payload)
            assert rows == []
        else:
            session.materialize(rows)
            assert len(rows) == 513
        session.finish()
        assert session.finished and children[0].returncode == 0
        assert session.files == 513 and session.batch_id == 3
    finally:
        session.close()


def test_owned_planner_spool_releases_rows_and_preserves_exact_sorted_batches(tmp_path):
    class Row(dict):
        pass

    rows = [Row(path=f"artifacts/{n:04}", bytes=n, sha256=f"{n % 7:064x}") for n in range(600)]
    expected = [
        json.dumps(b, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        for b in restore_io.batches(rows)
    ]
    references = [weakref.ref(row) for row in rows]
    with restore_native.BatchSpool(rows, tmp_path, time.monotonic() + 10) as spool:
        assert not rows and all(ref() is None for ref in references)
        assert spool.stream is not None
        info = os.fstat(spool.stream.fileno())
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_nlink == 0 and not os.get_inheritable(spool.stream.fileno())
        assert list(tmp_path.iterdir()) == []
        actual = [
            json.dumps(b, separators=(",", ":"), ensure_ascii=True).encode() + b"\n" for b in spool
        ]
        assert actual == expected and spool.complete
        with pytest.raises(DeliveryError, match="already consumed"):
            list(spool)
    assert spool.stream.closed and list(tmp_path.iterdir()) == []


def test_owned_planner_spool_partial_consumer_fails_and_closes(tmp_path):
    spool = restore_native.BatchSpool(
        [{"path": "artifacts/a", "bytes": 1, "sha256": "1" * 64}],
        tmp_path,
        time.monotonic() + 10,
    )
    with pytest.raises(DeliveryError, match="input incomplete"), spool:
        next(iter(spool))
    assert spool.stream.closed and list(tmp_path.iterdir()) == []


def test_owned_planner_spool_preserves_consumer_error_and_closes(tmp_path):
    spool = restore_native.BatchSpool([], tmp_path, time.monotonic() + 10)
    with pytest.raises(RuntimeError, match="fixture failure"), spool:
        raise RuntimeError("fixture failure")
    assert spool.stream.closed and list(tmp_path.iterdir()) == []


def test_owned_planner_spool_bounds_creation_and_releases_descriptor(tmp_path, monkeypatch):
    original = restore_native.tempfile.TemporaryFile
    opened = []

    def temporary_file(**options):
        stream = original(**options)
        opened.append(stream)
        return stream

    monkeypatch.setattr(restore_native.tempfile, "TemporaryFile", temporary_file)
    monkeypatch.setattr(restore_native, "MAX_SPOOL_BYTES", 1)
    with pytest.raises(DeliveryError, match="input exceeds bound"):
        restore_native.BatchSpool(
            [{"path": "artifacts/a", "bytes": 1, "sha256": "1" * 64}],
            tmp_path,
            time.monotonic() + 10,
        )
    assert len(opened) == 1 and opened[0].closed and list(tmp_path.iterdir()) == []


def test_owned_planner_spool_retains_original_deadline(tmp_path):
    spool = restore_native.BatchSpool([], tmp_path, time.monotonic() + 10)
    spool.deadline = time.monotonic() - 1
    with pytest.raises(DeliveryError, match="deadline exceeded"), spool:
        list(spool)
    assert spool.stream.closed and list(tmp_path.iterdir()) == []
