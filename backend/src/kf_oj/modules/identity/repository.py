from datetime import datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.modules.identity.models import Account, LoginThrottle, Session
from kf_oj.modules.identity.public import LoginThrottled


async def consume_login_budget(
    session: AsyncSession, bucket: str, now: datetime, reset_at: datetime
) -> None:
    # An upsert establishes the key before FOR UPDATE; requests share one DB-backed budget.
    inserted = await session.scalar(
        insert(LoginThrottle)
        .values(bucket_hash=bucket, attempts=1, reset_at=reset_at)
        .on_conflict_do_nothing(index_elements=[LoginThrottle.bucket_hash])
        .returning(LoginThrottle.bucket_hash)
    )
    row = await session.scalar(
        select(LoginThrottle).where(LoginThrottle.bucket_hash == bucket).with_for_update()
    )
    assert row is not None
    # The initial upsert consumes this request only if there was no prior row.
    # A separate returning flag avoids granting an extra request during concurrency.
    if inserted is not None:
        return
    if row.reset_at <= now:
        row.attempts, row.reset_at = 1, reset_at
    elif row.attempts >= 10:
        raise LoginThrottled("login request budget exhausted")
    else:
        row.attempts += 1


async def prune_throttles(session: AsyncSession, now: datetime, *, limit: int = 100) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(-740164777)"))
    keys = select(LoginThrottle.bucket_hash).where(LoginThrottle.reset_at <= now)
    keys = keys.order_by(LoginThrottle.reset_at).limit(limit).with_for_update(skip_locked=True)
    await session.execute(delete(LoginThrottle).where(LoginThrottle.bucket_hash.in_(keys)))


async def check_throttle_capacity(session: AsyncSession, bucket: str) -> None:
    if await session.get(LoginThrottle, bucket) is None:
        if await session.scalar(select(func.count()).select_from(LoginThrottle)) >= 4096:
            raise LoginThrottled("login throttle capacity exhausted")


async def account_by_username(session: AsyncSession, username: str) -> Account | None:
    return await session.scalar(select(Account).where(Account.username == username))


async def lock_authority(session: AsyncSession, account_id) -> Account | None:
    return await session.scalar(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )


async def current_session(session: AsyncSession, token_hash: str, *, lock_session: bool = False):
    query = (
        select(Session, Account)
        .join(Account)
        .where(
            Session.token_hash == token_hash,
            Session.revoked_at.is_(None),
            Session.expires_at > func.clock_timestamp(),
            Account.enabled.is_(True),
        )
    )
    if lock_session:
        query = query.with_for_update(read=True, of=Session).execution_options(
            populate_existing=True
        )
    return (await session.execute(query)).one_or_none()


async def prune_sessions(session: AsyncSession, account_id) -> None:
    await session.execute(
        delete(Session).where(
            Session.account_id == account_id,
            (Session.expires_at <= func.now()) | Session.revoked_at.is_not(None),
        )
    )
    # Keep at most nine older valid sessions before adding the new one.
    old = select(Session.token_hash).where(Session.account_id == account_id)
    old = old.order_by(Session.created_at.desc(), Session.token_hash).offset(9)
    await session.execute(delete(Session).where(Session.token_hash.in_(old)))


async def revoke(session: AsyncSession, token_hash: str) -> None:
    await session.execute(
        update(Session).where(Session.token_hash == token_hash).values(revoked_at=func.now())
    )
