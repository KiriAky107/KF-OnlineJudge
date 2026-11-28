import asyncio
from base64 import b64encode
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kf_oj.modules.problems import repository
from kf_oj.modules.problems.public import ProblemUnavailable, public_diagnostic_sample


def test_diagnostic_uses_the_requested_advertised_sample_without_reading_judge_cases(monkeypatch):
    version = uuid4()

    async def public_version(session, selected, *, admission):
        assert selected == version and admission is True
        return (
            SimpleNamespace(
                public_samples=[
                    {"input": "MQo=", "output": "Mg0K"},
                    {"input": "", "output": "AA=="},
                ]
            ),
            object(),
        )

    monkeypatch.setattr(repository, "public_version", public_version)
    sample = asyncio.run(public_diagnostic_sample(object(), version, 1))
    assert (sample.version_id, sample.ordinal) == (version, 1)
    assert (sample.input_bytes, sample.expected_bytes) == (b"", b"\0")
    for ordinal in (-1, 2):
        with pytest.raises(ProblemUnavailable):
            asyncio.run(public_diagnostic_sample(object(), version, ordinal))


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [{"input": "bad!", "output": ""}],
        [{"input": "", "output": b64encode(b"x" * 65537).decode()}],
    ],
)
def test_diagnostic_rejects_unavailable_or_invalid_public_sample(monkeypatch, rows):
    async def public_version(*_args, **_kwargs):
        if rows is None:
            return None
        return SimpleNamespace(public_samples=rows), object()

    monkeypatch.setattr(repository, "public_version", public_version)
    with pytest.raises(ProblemUnavailable):
        asyncio.run(public_diagnostic_sample(object(), uuid4(), 0))
