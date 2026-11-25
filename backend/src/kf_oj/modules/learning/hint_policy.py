"""Apply the tightest site, account role and training-scope hint limit."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kf_oj.contracts.learning_model import PROGRESSIVE_HINT_LEVELS, ProgressiveHintLevel


class HintCeilingDenied(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HintPolicy:
    site: ProgressiveHintLevel = "L3"
    member: ProgressiveHintLevel = "L3"
    administrator: ProgressiveHintLevel = "L3"

    def role_ceiling(self, is_administrator: bool) -> ProgressiveHintLevel:
        role = self.administrator if is_administrator else self.member
        return min(self.site, role, key=PROGRESSIVE_HINT_LEVELS.index)

    async def ceiling(
        self,
        db: AsyncSession,
        account_id: UUID,
        is_administrator: bool,
        problem_version_id: UUID,
        training_id: UUID | None,
    ) -> ProgressiveHintLevel:
        training = "L3"
        if training_id is not None:
            training = await db.scalar(
                select(func.kf_training_hint_ceiling(account_id, training_id, problem_version_id))
            )
            if training not in PROGRESSIVE_HINT_LEVELS:
                raise HintCeilingDenied("training cannot authorize hints for this problem version")
        return min(self.role_ceiling(is_administrator), training, key=PROGRESSIVE_HINT_LEVELS.index)


DEFAULT_HINT_POLICY = HintPolicy()
