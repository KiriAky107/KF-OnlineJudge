"""Validate and store private authoring documents without reading personal bindings."""

import hashlib
import json
from collections.abc import Collection
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_runtime import AgentPurpose
from kf_oj.contracts.skill_drafts import (
    CreateSkillDraft,
    SkillDraftDocument,
    SkillDraftPage,
    SkillDraftSummary,
    SkillDraftView,
    SkillTrialView,
    UpdateSkillDraft,
)
from kf_oj.modules.identity.public import ActiveSessionPort, Principal
from kf_oj.modules.skills.availability import lock_discoverable_version
from kf_oj.modules.skills.catalog import BuiltinSkills, SkillVersionUnavailable
from kf_oj.modules.skills.definitions import PARAMETERS_BY_PURPOSE
from kf_oj.modules.skills.draft_models import SkillDraftRow, SkillTrialRow
from kf_oj.runtime.tools import ToolKey


class SkillDraftUnavailable(LookupError):
    pass


class SkillDraftConflict(ValueError):
    pass


def _summary(row: SkillDraftRow) -> SkillDraftSummary:
    return SkillDraftSummary(
        draft_id=row.id,
        title=row.document["title"],
        purpose=row.document["purpose"],
        revision=row.revision,
        updated_at=row.updated_at,
    )


def _view(row: SkillDraftRow) -> SkillDraftView:
    return SkillDraftView(
        **_summary(row).model_dump(),
        source_version_id=row.source_version_id,
        content_digest=row.content_digest,
        document=SkillDraftDocument.model_validate(row.document),
    )


def _hash(data) -> str:
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _has_null_character(value: object) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(
            _has_null_character(key) or _has_null_character(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_null_character(item) for item in value)
    return False


class SkillDrafts:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        identity: ActiveSessionPort,
        catalog: BuiltinSkills,
        allowed_tools: Collection[ToolKey],
    ) -> None:
        self._sessions, self._identity, self._catalog = sessions, identity, catalog
        self._allowed_tools = {
            purpose: frozenset(
                ToolKey(request.name, request.version)
                for skill in catalog.list_versions(purpose)
                for request in skill.declaration.requested_tools
            ).intersection(allowed_tools)
            for purpose in AgentPurpose
        }

    def _canonical(self, document: SkillDraftDocument) -> dict:
        requested = {ToolKey(tool.name, tool.version) for tool in document.requested_tools}
        if len(requested) != len(document.requested_tools):
            raise ValueError("duplicate requested tool")
        if not requested <= self._allowed_tools[document.purpose]:
            raise ValueError("unapproved tool for this learning purpose")
        params = PARAMETERS_BY_PURPOSE[document.purpose].model_validate_json(
            json.dumps(document.parameters), strict=True
        )
        result = document.model_dump(mode="json")
        result["parameters"] = params.model_dump(mode="json")
        for reference in result.get("references", ()):
            digest = hashlib.sha256(reference["content"].encode()).hexdigest()
            if reference["sha256"] not in (None, digest):
                raise ValueError("reference digest does not match content")
            reference["sha256"] = digest
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode()) > 16384 or _has_null_character(result):
            raise ValueError("draft text exceeds the declaration format")
        return result

    def _owned(self, principal: Principal):
        return select(SkillDraftRow).where(SkillDraftRow.owner_id == principal.account_id)

    async def page(self, principal: Principal) -> SkillDraftPage:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            rows = (
                await db.scalars(
                    self._owned(principal)
                    .order_by(SkillDraftRow.updated_at.desc(), SkillDraftRow.id)
                    .limit(51)
                )
            ).all()
            return SkillDraftPage(
                items=tuple(_summary(row) for row in rows[:50]), truncated=len(rows) > 50
            )

    async def get(self, principal: Principal, draft_id: UUID) -> SkillDraftView:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            row = await db.scalar(self._owned(principal).where(SkillDraftRow.id == draft_id))
            if row is None:
                raise SkillDraftUnavailable("skill draft unavailable")
            return _view(row)

    async def _copy_source(self, db: AsyncSession, version_id: UUID) -> SkillDraftDocument:
        try:
            builtin = self._catalog.get(version_id).declaration
        except SkillVersionUnavailable:
            builtin = None
        if builtin is not None:
            data = builtin.model_dump(mode="json", exclude={"slug", "release", "author"})
            data["attribution"] = (
                f"KF-OnlineJudge / {builtin.slug} v{builtin.release} ({version_id})"
            )
            return SkillDraftDocument.model_validate(data)

        published = await lock_discoverable_version(db, version_id)
        if published is None:
            raise SkillDraftUnavailable("source skill version unavailable")
        source_credit = f"{published.author_name} / v{published.version} ({version_id})"
        previous_credit = published.document.get("attribution")
        attribution = f"{previous_credit} · {source_credit}" if previous_credit else source_credit
        if len(attribution) > 256:
            # Keep existing license credit; source_version_id still records the direct source.
            attribution = previous_credit
        return SkillDraftDocument.model_validate(published.document | {"attribution": attribution})

    async def create(self, principal: Principal, body: CreateSkillDraft) -> SkillDraftView:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            doc = body.document
            if body.source_version_id is not None:
                previous = await db.scalar(
                    self._owned(principal).where(SkillDraftRow.request_id == body.request_id)
                )
                if previous is not None:
                    if previous.source_version_id != body.source_version_id:
                        raise SkillDraftConflict("draft request already has different inputs")
                    return _view(previous)
                doc = await self._copy_source(db, body.source_version_id)
            assert doc is not None
            data = self._canonical(doc)
            digest = _hash(data)
            fingerprint = _hash({"from": str(body.source_version_id), "document": data})
            row = await db.scalar(
                insert(SkillDraftRow)
                .values(
                    id=uuid4(),
                    owner_id=principal.account_id,
                    request_id=body.request_id,
                    request_digest=fingerprint,
                    source_version_id=body.source_version_id,
                    document=data,
                    content_digest=digest,
                )
                .on_conflict_do_nothing(constraint="uq_skills_draft_request")
                .returning(SkillDraftRow)
            )
            if row is None:
                row = await db.scalar(
                    self._owned(principal).where(SkillDraftRow.request_id == body.request_id)
                )
                if row is None or row.request_digest != fingerprint:
                    raise SkillDraftConflict("draft request already has different inputs")
            return _view(row)

    async def update(
        self, principal: Principal, draft_id: UUID, body: UpdateSkillDraft
    ) -> SkillDraftView:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            row = await db.scalar(
                self._owned(principal).where(SkillDraftRow.id == draft_id).with_for_update()
            )
            if row is None:
                raise SkillDraftUnavailable("skill draft unavailable")
            data = self._canonical(body.document)
            digest = _hash(data)
            if row.content_digest == digest:
                return _view(row)
            if row.revision != body.revision:
                raise SkillDraftConflict("skill draft was updated")
            row.document, row.content_digest = data, digest
            row.revision += 1
            row.updated_at = await db.scalar(select(func.clock_timestamp()))
            await db.flush()
            return _view(row)

    async def create_trial(
        self, principal: Principal, draft_id: UUID, revision: int
    ) -> SkillTrialView:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            draft = await db.scalar(
                self._owned(principal).where(SkillDraftRow.id == draft_id).with_for_update()
            )
            if draft is None:
                raise SkillDraftUnavailable("skill draft unavailable")
            trial = await db.scalar(
                select(SkillTrialRow).where(
                    SkillTrialRow.draft_id == draft_id, SkillTrialRow.revision == revision
                )
            )
            if trial is None:
                if draft.revision != revision:
                    raise SkillDraftConflict("skill draft was updated")
                fields = ("purpose", "title", "description", "parameters", "requested_tools")
                trial = SkillTrialRow(
                    id=uuid4(),
                    draft_id=draft.id,
                    revision=draft.revision,
                    content_digest=draft.content_digest,
                    declaration={key: draft.document[key] for key in fields},
                )
                db.add(trial)
            return SkillTrialView(
                version_id=trial.id, revision=trial.revision, content_digest=trial.content_digest
            )
