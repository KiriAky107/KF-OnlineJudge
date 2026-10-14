"""Read-only observers must not load delivery or archive mutation implementations."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("observer", "forbidden"),
    [
        (
            "kf_oj.workers.outbox_status",
            ["kf_oj.infra.messaging.publisher", "aio_pika", "aiormq"],
        ),
        (
            "kf_oj.workers.problem_status",
            [
                "kf_oj.modules.problems.imports",
                "kf_oj.modules.problems.maintenance",
                "kf_oj.infra.artifacts.staging",
            ],
        ),
    ],
)
def test_read_only_observer_imports_exclude_mutation_implementations(observer, forbidden):
    source = Path(__file__).resolve().parents[1] / "src"
    probe = """import importlib, importlib.abc, sys
class RefuseMutation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in sys.argv[2:]):
            raise AssertionError('read-only observer loaded mutation implementation')
sys.meta_path.insert(0, RefuseMutation())
importlib.import_module(sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-s", "-P", "-c", probe, observer, *forbidden],
        env={
            "PATH": os.defpath,
            "PYTHONPATH": str(source),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_import_policy_identity_and_availability_error_remain_shared():
    from kf_oj.modules.problems import import_policy, imports

    assert imports.ImportUnavailable is import_policy.ImportUnavailable
    assert imports.policy_digest is import_policy.policy_digest
