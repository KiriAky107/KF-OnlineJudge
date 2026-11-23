"""Resolve configured knowledge prerequisites using the learner's own accepted work."""

from sqlalchemy import text

from kf_oj.contracts.learning import LearningEvidence, TrainingPrerequisite


async def accepted_prerequisite_evidence(session, owner_id, candidates, now):
    tags = sorted({tag for candidate in candidates for tag in candidate.prerequisite_tags})
    if not tags:
        return {}
    rows = await session.execute(
        text("SELECT * FROM public.kf_learning_prerequisite_evidence(:owner, :tags, :as_of)"),
        {"owner": owner_id, "tags": tags, "as_of": now},
    )
    return {
        row.tag: TrainingPrerequisite(
            tag=row.tag,
            metadata_revision=row.metadata_revision,
            evidence=LearningEvidence(
                submission_id=row.submission_id,
                evaluation_id=row.evaluation_id,
                problem_id=row.problem_id,
                problem_version_id=row.problem_version_id,
                submitted_at=row.submitted_at,
                verdict="accepted",
                state="finished",
            ),
        )
        for row in rows
    }
