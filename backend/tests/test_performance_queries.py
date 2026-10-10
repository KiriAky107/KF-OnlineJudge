"""Integer-domain tests and finite PG read integration, not measured executions."""

import asyncio
from datetime import UTC, datetime

import pytest
from test_coordinator import claim, fixture, manifest, stages, synthetic_report, worker_headers

from kf_oj.contracts.judging import I64_MAX
from kf_oj.modules.submissions.performance import metric


@pytest.mark.parametrize(
    "current,values,greater,ties",
    [
        (0, [0, 0, 1, 2, 2], 3, 2),
        (
            9_007_199_254_740_993,
            [9_007_199_254_740_992, 9_007_199_254_740_993, 9_007_199_254_740_994, I64_MAX, 0],
            2,
            1,
        ),
        (I64_MAX, [0, 1, 2, 3, 4], 0, 0),
        (0, [0, 0, 0, 0, 0], 0, 5),
    ],
)
def test_exact_metrics_keep_zero_ties_and_extreme_values(current, values, greater, ties):
    now = datetime.now(UTC)
    result = metric("cpu_microseconds", current, [(v, now) for v in values])
    assert result.available and result.current == str(current)
    assert result.strictly_slower_count == greater and result.tied_count == ties
    assert sum(b.count for b in result.buckets) == len(values)
    assert len(result.buckets) <= 24
    for value in values:
        assert (
            sum(
                int(b.start) <= value < int(b.end) or (b.upper_inclusive and value == int(b.end))
                for b in result.buckets
            )
            == 1
        )


def test_recent_window_missing_and_insufficient_are_distinct():
    now = datetime.now(UTC)
    limited = metric("bytes", 0, [(1, now)] * 10_001)
    assert limited.sample_count == 10_000 and limited.window_truncated
    assert limited.strictly_slower_count == 10_000
    assert metric("bytes", None).reason == "measurement_missing"
    assert metric("bytes", 0).reason == "insufficient_samples"
    assert metric("bytes", 0, [(0, now)] * 4).buckets == ()


def test_actual_owned_read_snapshot_cache_invalidation_and_history(
    postgres_url, tmp_path, monkeypatch
):
    async def run():
        async with fixture(
            postgres_url, tmp_path, monkeypatch, measurement_epochs=["a" * 64] * 2
        ) as (client, connection, workers, _, create):
            app = client.fixture_app
            owner = client.fixture_account
            created_items = []
            for _ in range(6):
                created = await create()
                grant = await claim(client, created, workers[0])
                await stages(client, workers[0], grant)
                report = synthetic_report(grant, await manifest(connection, grant))
                result = await client.post(
                    "/internal/report", json=report, headers=worker_headers(workers[0], grant)
                )
                assert result.status_code == 204, result.text
                created_items.append(created)
            target = created_items[0].submission_id
            first = await app.state.submission_performance.detail(owner, target)
            assert first.cpu.sample_count == 5 and first.cpu.tied_count == 5
            assert first.cpu.strictly_slower_count == 0 and first.memory.tied_count == 5
            cached = await app.state.submission_performance.detail(owner, target)
            assert cached == first
            pending = await create()
            assert (
                await app.state.submission_performance.detail(owner, pending.submission_id)
            ).cpu.reason == "not_accepted"
            grant = await claim(client, pending, workers[0])
            await stages(client, workers[0], grant)
            report = synthetic_report(grant, await manifest(connection, grant))
            assert (
                await client.post(
                    "/internal/report", json=report, headers=worker_headers(workers[0], grant)
                )
            ).status_code == 204
            updated = await app.state.submission_performance.detail(owner, target)
            assert updated.cpu.sample_count == 6 and updated.cpu.tied_count == 6
            assert updated.as_of >= first.as_of
            page = await app.state.submission_queries.page(
                owner, None, 2, client.fixture_version, "c17"
            )
            assert len(page.items) == 2 and page.next_after
            older = await app.state.submission_queries.page(
                owner, page.next_after, 2, client.fixture_version, "c17"
            )
            assert len(older.items) == 2
            assert not {i.submission_id for i in page.items} & {
                i.submission_id for i in older.items
            }
            assert not (
                await app.state.submission_queries.page(
                    owner, None, 2, client.fixture_version, "java21"
                )
            ).items

    asyncio.run(run())
