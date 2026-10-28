"""Ordinary pipe fixtures only; no Root, filesystem-copy or VM acceptance."""

import json
import subprocess
import sys
import time

import pytest
from scripts.release import restore_native
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
