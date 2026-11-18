"""One real PostgreSQL annotation/authority scenario; no judging execution."""

import asyncio
import secrets

import pytest
from conftest import connect
from test_maintenance_authority import set_administrator
from test_submission_create import setup_app

from kf_oj.contracts.problem_learning import LearningMetadataUpdate
from kf_oj.modules.identity.public import MaintenanceDenied


def test_metadata_live_authority_audit_and_immutable_version(postgres_url, tmp_path, monkeypatch):
    async def run():
        app, engine, mod = await setup_app(postgres_url, tmp_path)
        monkeypatch.setattr(mod, "build_engine", lambda _: engine)
        connection = await connect(postgres_url)
        try:
            async with app.router.lifespan_context(app):
                password = secrets.token_urlsafe(24)
                account = await app.state.identity.create_account("metadata_actor", password)
                issued = await app.state.identity.login(account.username, password, peer="metadata")
                ordinary = await app.state.identity.authenticate(issued.token)
                version = await app.state.problems.import_directory(tmp_path / "import")
                before = dict(
                    await connection.fetchrow("SELECT * FROM problem_versions WHERE id=$1", version)
                )
                service = app.state.problem_maintenance
                body = LearningMetadataUpdate(tags=("排序",), difficulty="easy")
                with pytest.raises(MaintenanceDenied):
                    await service.learning_metadata(ordinary, version, body)
                assert (
                    await connection.fetchval("SELECT count(*) FROM problem_learning_metadata") == 0
                )
                await set_administrator(connection, account.account_id, True)
                issued = await app.state.identity.login(account.username, password, peer="metadata")
                administrator = await app.state.identity.authenticate(issued.token)
                first = await service.learning_metadata(administrator, version, body)
                assert first.revision == 1 and first.tags == ("排序",)
                second = await service.learning_metadata(
                    administrator,
                    version,
                    LearningMetadataUpdate(tags=("排序", "二分"), difficulty="medium"),
                )
                assert second.revision == 2
                assert (await service.learning_metadata(administrator, version)).revision == 2
                after = dict(
                    await connection.fetchrow("SELECT * FROM problem_versions WHERE id=$1", version)
                )
                assert after == before
                events = await connection.fetch(
                    "SELECT actor_id,target_id,authorization_version "
                    "FROM problem_maintenance_events WHERE action='learning_metadata_updated'"
                )
                assert len(events) == 2
                assert all(
                    e["actor_id"] == account.account_id
                    and e["target_id"] == version
                    and e["authorization_version"] == administrator.authorization_version
                    for e in events
                )
        finally:
            await connection.close()
            await engine.dispose()

    asyncio.run(run())
