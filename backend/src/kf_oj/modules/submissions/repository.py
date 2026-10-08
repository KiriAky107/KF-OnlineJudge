from sqlalchemy import func, select

from kf_oj.contracts.judging import LeaseBinding
from kf_oj.modules.submissions.models import Evaluation, JudgeAttempt, Submission


async def leased_submission(session, binding: LeaseBinding, token_hash: str):
    return (
        await session.execute(
            select(Evaluation, JudgeAttempt, Submission)
            .join(JudgeAttempt, JudgeAttempt.id == Evaluation.current_attempt_id)
            .join(Submission, Submission.id == Evaluation.submission_id)
            .where(
                Evaluation.id == binding.evaluation_id,
                Evaluation.state.in_(("claimed", "compiling", "running")),
                Evaluation.cancel_requested_at.is_(None),
                Evaluation.fencing_version == binding.fencing_version,
                JudgeAttempt.id == binding.attempt_id,
                JudgeAttempt.evaluation_id == Evaluation.id,
                JudgeAttempt.worker_id == binding.worker_id,
                JudgeAttempt.fencing_version == binding.fencing_version,
                JudgeAttempt.lease_token_hash == token_hash,
                JudgeAttempt.expires_at > func.clock_timestamp(),
                JudgeAttempt.revoked_at.is_(None),
            )
        )
    ).one_or_none()
