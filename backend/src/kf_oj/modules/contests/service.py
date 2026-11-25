from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import async_sessionmaker

from kf_oj.contracts.contests import ContestRating, ContestSettlement, RatingChange, SettleContest
from kf_oj.modules.contests.models import ContestRatingChange, RatedContest
from kf_oj.modules.contests.rating import INITIAL_RATING, PROMOTIONS, rating_deltas
from kf_oj.modules.identity.public import ContestAdministrationPort, Principal


class SettlementConflict(ValueError):
    pass


def settlement(row: RatedContest) -> ContestSettlement:
    return ContestSettlement(
        key=row.key,
        title=row.title,
        ended_at=row.ended_at,
        settled_at=row.settled_at,
        participants=row.participants,
    )


class ContestRatings:
    def __init__(self, sessions: async_sessionmaker, authority: ContestAdministrationPort):
        self.sessions = sessions
        self.authority = authority

    async def settle(self, principal: Principal, body: SettleContest) -> ContestSettlement:
        ordered = tuple(sorted(body.standings, key=lambda row: (row.rank, row.username)))
        normalized = body.model_copy(
            update={
                "title": body.title.strip(),
                "ended_at": body.ended_at.astimezone(UTC),
                "standings": ordered,
            }
        )
        digest = sha256(normalized.model_dump_json().encode()).hexdigest()
        async with self.sessions.begin() as db:
            await self.authority.require_administrator(db, principal)
            # One settlement order across all API processes.
            await db.execute(select(func.pg_advisory_xact_lock(0x4B465243)))
            existing = await db.get(RatedContest, body.key)
            if existing is not None:
                if existing.payload_digest != digest:
                    raise SettlementConflict("contest already settled with different standings")
                return settlement(existing)
            now = datetime.now(UTC)
            if body.ended_at > now:
                raise ValueError("contest has not ended")
            latest = await db.scalar(select(func.max(RatedContest.ended_at)))
            if latest is not None and body.ended_at < latest:
                raise SettlementConflict("settle older contests before newer contests")
            usernames = tuple(row.username for row in ordered)
            ids = await self.authority.contest_account_ids(db, usernames)
            if len(ids) != len(usernames):
                raise ValueError("standings contain unknown usernames")
            changes = await db.scalars(
                select(ContestRatingChange)
                .where(ContestRatingChange.account_id.in_(ids.values()))
                .ext(distinct_on(ContestRatingChange.account_id))
                .order_by(ContestRatingChange.account_id, ContestRatingChange.round_number.desc())
            )
            previous = {row.account_id: row for row in changes}
            ratings = [
                previous[ids[name]].internal_rating if ids[name] in previous else INITIAL_RATING
                for name in usernames
            ]
            deltas = rating_deltas(ratings, [row.rank for row in ordered])
            contest = RatedContest(
                key=body.key,
                title=normalized.title,
                ended_at=body.ended_at,
                settled_at=now,
                settled_by=principal.account_id,
                payload_digest=digest,
                participants=len(ordered),
            )
            db.add(contest)
            await db.flush()
            for row, rating, delta in zip(ordered, ratings, deltas, strict=True):
                account = ids[row.username]
                prior = previous.get(account)
                rounds = prior.round_number if prior else 0
                internal = rating + delta
                remaining = sum(PROMOTIONS[rounds + 1 :])
                db.add(
                    ContestRatingChange(
                        contest_key=body.key,
                        account_id=account,
                        rank=row.rank,
                        round_number=rounds + 1,
                        old_rating=prior.new_rating if prior else 0,
                        new_rating=internal - remaining,
                        internal_rating=internal,
                    )
                )
            await db.flush()
            return settlement(contest)

    async def profile(self, principal: Principal) -> ContestRating:
        scope = ContestRatingChange.account_id == principal.account_id
        # A single statement keeps the counts and the recent rows on the same snapshot.
        total = func.count().over()
        peak = func.max(ContestRatingChange.new_rating).over()
        async with self.sessions() as db:
            entries = (
                await db.execute(
                    select(ContestRatingChange, RatedContest, total, peak)
                    .join(RatedContest, RatedContest.key == ContestRatingChange.contest_key)
                    .where(scope)
                    .order_by(ContestRatingChange.round_number.desc())
                    .limit(50)
                )
            ).all()
        return ContestRating(
            rating=entries[0][0].new_rating if entries else None,
            highest_rating=entries[0][3] if entries else None,
            rated_contests=entries[0][2] if entries else 0,
            history=tuple(
                RatingChange(
                    contest_key=contest.key,
                    title=contest.title,
                    ended_at=contest.ended_at,
                    rank=change.rank,
                    old_rating=change.old_rating,
                    new_rating=change.new_rating,
                )
                for change, contest, _, _ in entries
            ),
        )
