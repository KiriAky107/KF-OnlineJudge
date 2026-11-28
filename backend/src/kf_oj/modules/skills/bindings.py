"""Personal skill bindings and immutable configurations for Agent runs."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kf_oj.contracts.agent_runtime import AgentPurpose, AgentRun
from kf_oj.contracts.judging import WireModel
from kf_oj.modules.identity.public import ActiveSessionPort, Principal
from kf_oj.modules.skills.binding_models import SkillBindingRow, SkillConfigurationRow
from kf_oj.modules.skills.catalog import (
    BuiltinSkills,
    RuntimeDeclaration,
    RuntimeSkillVersion,
    SkillVersionUnavailable,
    stored_skill_version,
)
from kf_oj.modules.skills.draft_models import SkillDraftRow, SkillTrialRow
from kf_oj.modules.skills.publication_models import SkillVersionArchiveRow
from kf_oj.runtime.tools import AgentToolGrant, ToolKey


class SkillBindingUnavailable(LookupError):
    """A binding or configuration is unavailable to the caller."""


@dataclass(frozen=True, slots=True)
class SkillBinding:
    version_id: UUID
    parameters: WireModel
    enabled: bool
    authorized_tools: frozenset[ToolKey]


@dataclass(frozen=True, slots=True)
class SkillConfiguration:
    version_id: UUID
    digest: str
    parameters: WireModel


def _encode_tools(tools: frozenset[ToolKey]) -> list[list]:
    return [list(key) for key in sorted((tool.name, tool.version) for tool in tools)]


def _decode_tools(tools: list) -> frozenset[ToolKey]:
    return frozenset(ToolKey(name, version) for name, version in tools)


def _preferences(version: RuntimeSkillVersion, data: dict) -> WireModel:
    return version.validate_parameters(json.dumps(data, ensure_ascii=False).encode())


class SkillBindings:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        identity: ActiveSessionPort,
        catalog: BuiltinSkills,
        tool_scopes: Mapping[ToolKey, str],
        *,
        claim: AgentRun | None = None,
    ) -> None:
        self._sessions = sessions
        self._identity = identity
        self._catalog = catalog
        self._tool_scopes = dict(tool_scopes)
        self._claim = claim

    async def _version(
        self, db: AsyncSession, principal: Principal, version_id: UUID
    ) -> RuntimeSkillVersion:
        try:
            return self._catalog.get(version_id)
        except SkillVersionUnavailable:
            pass
        statement = text("SELECT * FROM public.kf_read_published_skill(:version_id)").columns(
            declaration=JSONB
        )
        row = (await db.execute(statement, {"version_id": version_id})).one_or_none()
        if row is not None:
            skill_id, release, digest, document = row
            return stored_skill_version(
                version_id, skill_id, release, digest, document, origin="published"
            )
        return await self._trial_version(db, principal.account_id, version_id)

    async def _trial_version(
        self, db: AsyncSession, owner_id: UUID, version_id: UUID
    ) -> RuntimeSkillVersion:
        claim = self._claim
        if claim is None:
            result = await db.execute(
                select(
                    SkillTrialRow.revision + 1,
                    SkillTrialRow.content_digest,
                    SkillTrialRow.declaration,
                )
                .join(SkillDraftRow, SkillDraftRow.id == SkillTrialRow.draft_id)
                .where(SkillTrialRow.id == version_id, SkillDraftRow.owner_id == owner_id)
            )
        else:
            result = await db.execute(
                text(
                    "SELECT * FROM public.kf_read_claimed_skill_trial("
                    ":run_id, :revision, :version_id, :owner_id)"
                ).columns(declaration=JSONB),
                {
                    "run_id": claim.run_id,
                    "revision": claim.revision,
                    "version_id": version_id,
                    "owner_id": owner_id,
                },
            )
        row = result.one_or_none()
        if row is None:
            raise SkillVersionUnavailable("skill version unavailable")
        release, digest, document = row
        return stored_skill_version(
            version_id, version_id, release, digest, document, origin="trial"
        )

    def approved_tools(self, version: RuntimeSkillVersion) -> frozenset[ToolKey]:
        requested = frozenset(
            ToolKey(tool.name, tool.version) for tool in version.declaration.requested_tools
        )
        return requested.intersection(self._tool_scopes)

    async def configure(
        self,
        principal: Principal,
        version_id: UUID,
        *,
        parameters: bytes,
        enabled: bool,
        authorized_tools: frozenset[ToolKey],
    ) -> SkillBinding:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            version = await self._version(db, principal, version_id)
            declaration = version.declaration
            slug = declaration.slug
            key = (principal.account_id, slug)
            archived = (
                isinstance(declaration, RuntimeDeclaration)
                and declaration.origin == "published"
                and await db.get(SkillVersionArchiveRow, version_id) is not None
            )
            if archived:
                binding = await db.get(SkillBindingRow, key, with_for_update=True)
                if binding is None or binding.version_id != version_id:
                    raise SkillVersionUnavailable("skill version unavailable")
            if not authorized_tools <= self.approved_tools(version):
                raise ValueError("skill requests tools beyond its approved scope")
            validated = version.validate_parameters(parameters)
            preferences = validated.model_dump(mode="json")
            tools = _encode_tools(authorized_tools)
            if not archived:
                await db.execute(
                    insert(SkillBindingRow)
                    .values(
                        owner_id=principal.account_id,
                        slug=slug,
                        version_id=version_id,
                        preferences=preferences,
                        authorized_tools=tools,
                        enabled=enabled,
                        authorization_epoch=1,
                    )
                    .on_conflict_do_nothing()
                )
                binding = await db.get(SkillBindingRow, key, with_for_update=True)
            assert binding is not None
            if (binding.enabled and not enabled) or not _decode_tools(
                binding.authorized_tools
            ) <= authorized_tools:
                binding.authorization_epoch += 1
            binding.version_id = version_id
            binding.preferences = preferences
            binding.authorized_tools = tools
            binding.enabled = enabled
        return SkillBinding(version_id, validated, enabled, authorized_tools)

    async def binding(self, principal: Principal, slug: str) -> SkillBinding:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            record = await db.get(SkillBindingRow, (principal.account_id, slug))
            if record is None:
                raise SkillBindingUnavailable("skill binding unavailable")
            version = await self._version(db, principal, record.version_id)
            return self._project_binding(record, version)

    @staticmethod
    def _project_binding(record: SkillBindingRow, version: RuntimeSkillVersion) -> SkillBinding:
        return SkillBinding(
            version.version_id,
            _preferences(version, record.preferences),
            record.enabled,
            _decode_tools(record.authorized_tools),
        )

    async def bindings(self, principal: Principal) -> dict[str, SkillBinding]:
        return {
            version.declaration.slug: saved
            for version, saved in await self.catalog_versions(principal)
            if saved is not None
        }

    async def catalog_versions(
        self, principal: Principal, extra: UUID | None = None
    ) -> tuple[tuple[RuntimeSkillVersion, SkillBinding | None], ...]:
        versions: dict[UUID, RuntimeSkillVersion] = {
            version.version_id: version
            for purpose in AgentPurpose
            for version in sorted(
                self._catalog.list_versions(purpose), key=lambda item: -item.declaration.release
            )
        }
        builtins = {version.declaration.slug for version in versions.values()}
        personal: dict[UUID, SkillBinding] = {}
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            records = (
                await db.scalars(
                    select(SkillBindingRow)
                    .where(SkillBindingRow.owner_id == principal.account_id)
                    .order_by(SkillBindingRow.slug.in_(builtins).desc(), SkillBindingRow.slug)
                    .limit(len(builtins) + 50)
                )
            ).all()
            for record in records:
                try:
                    version = versions.get(record.version_id) or await self._version(
                        db, principal, record.version_id
                    )
                except SkillVersionUnavailable:
                    continue
                versions[version.version_id] = version
                personal[version.version_id] = self._project_binding(record, version)
            if extra is not None and extra not in versions:
                version = await self._version(db, principal, extra)
                if version.declaration.slug not in builtins:
                    record = await db.get(
                        SkillBindingRow, (principal.account_id, version.declaration.slug)
                    )
                    if record is not None and record.version_id == extra:
                        personal[extra] = self._project_binding(record, version)
                versions[extra] = version
            archive_times = (
                await db.execute(
                    select(
                        SkillVersionArchiveRow.version_id, SkillVersionArchiveRow.created_at
                    ).where(SkillVersionArchiveRow.version_id.in_(versions))
                )
            ).all()
            for version_id, archived_at in archive_times:
                if version_id not in personal:
                    raise SkillVersionUnavailable("skill version unavailable")
                version = versions[version_id]
                declaration = version.declaration
                if isinstance(declaration, RuntimeDeclaration):
                    versions[version_id] = replace(
                        version, declaration=replace(declaration, archived_at=archived_at)
                    )
        return tuple((version, personal.get(key)) for key, version in versions.items())

    async def freeze(
        self,
        principal: Principal,
        version_id: UUID,
        *,
        purpose: AgentPurpose | None = None,
        required_tool: ToolKey | None = None,
    ) -> SkillConfiguration:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            version = await self._version(db, principal, version_id)
            if purpose is not None and version.declaration.purpose != purpose:
                raise SkillBindingUnavailable("skill unavailable for this learning purpose")
            row = await self._enabled_binding(db, principal, version)
            if row is None or row.version_id != version_id:
                raise SkillBindingUnavailable("skill binding unavailable")
            authorized_tools = _decode_tools(row.authorized_tools) & self.approved_tools(version)
            if required_tool is not None and required_tool not in authorized_tools:
                raise SkillBindingUnavailable("required skill tool unavailable")
            parameters = _preferences(version, row.preferences)
            values = {
                "preferences": parameters.model_dump(mode="json"),
                "authorized_tools": _encode_tools(authorized_tools),
                "authorization_epoch": row.authorization_epoch,
            }
            digest = hashlib.sha256(
                json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            await db.execute(
                insert(SkillConfigurationRow)
                .values(
                    owner_id=principal.account_id, version_id=version_id, digest=digest, **values
                )
                .on_conflict_do_nothing()
            )
            return SkillConfiguration(version_id, digest, parameters)

    async def _enabled_binding(
        self, db: AsyncSession, principal: Principal, version: RuntimeSkillVersion
    ) -> SkillBindingRow | None:
        key = principal.account_id, version.declaration.slug
        locked = await db.scalar(
            text("SELECT public.kf_lock_agent_skill(:owner, :slug)"),
            {"owner": key[0], "slug": key[1]},
        )
        if not locked:
            return None
        return await db.get(SkillBindingRow, key)

    async def require_enabled_tool(
        self,
        db: AsyncSession,
        principal: Principal,
        version_id: UUID,
        *,
        purpose: AgentPurpose,
        required_tool: ToolKey,
    ) -> WireModel:
        version = await self._version(db, principal, version_id)
        if version.declaration.purpose != purpose:
            raise SkillBindingUnavailable("skill unavailable for this learning purpose")
        binding = await self._enabled_binding(db, principal, version)
        if (
            binding is None
            or binding.version_id != version_id
            or required_tool not in _decode_tools(binding.authorized_tools)
            or required_tool not in self.approved_tools(version)
        ):
            raise SkillBindingUnavailable("required skill tool unavailable")
        return _preferences(version, binding.preferences)

    async def _current_configuration(
        self,
        db: AsyncSession,
        principal: Principal,
        version: RuntimeSkillVersion,
        digest: str,
    ) -> tuple[SkillBindingRow, SkillConfigurationRow] | None:
        binding = await self._enabled_binding(db, principal, version)
        if binding is None:
            return None
        frozen = await db.get(
            SkillConfigurationRow, (principal.account_id, version.version_id, digest)
        )
        if frozen is None or frozen.authorization_epoch != binding.authorization_epoch:
            return None
        return binding, frozen

    async def require_execution_configuration(
        self,
        db: AsyncSession,
        principal: Principal,
        version_id: UUID,
        digest: str,
        *,
        required_tool: ToolKey | None = None,
    ) -> SkillConfiguration:
        """Read the already authorized account's configuration in a caller transaction."""
        version = await self._version(db, principal, version_id)
        saved = await self._current_configuration(db, principal, version, digest)
        if saved is None:
            raise SkillBindingUnavailable("skill configuration unavailable")
        binding, frozen = saved
        if required_tool is not None and required_tool not in self._effective_tools(
            version, binding, frozen
        ):
            raise SkillBindingUnavailable("required skill tool unavailable")
        return SkillConfiguration(version_id, digest, _preferences(version, frozen.preferences))

    async def configuration(
        self,
        principal: Principal,
        version_id: UUID,
        digest: str,
        *,
        required_tool: ToolKey | None = None,
    ) -> SkillConfiguration:
        async with self._sessions.begin() as db:
            await self._identity.require_active_session(db, principal)
            return await self.require_execution_configuration(
                db, principal, version_id, digest, required_tool=required_tool
            )

    def _effective_tools(
        self, version: RuntimeSkillVersion, binding: SkillBindingRow, frozen: SkillConfigurationRow
    ) -> frozenset[ToolKey]:
        return (
            _decode_tools(frozen.authorized_tools)
            & _decode_tools(binding.authorized_tools)
            & self.approved_tools(version)
        )

    async def for_current_skill(
        self, db: AsyncSession, principal: Principal, version_id: UUID, configuration_digest: str
    ) -> AgentToolGrant | None:
        """After identity verification, intersect pinned and currently authorized tools."""
        try:
            version = await self._version(db, principal, version_id)
        except SkillVersionUnavailable:
            return None
        saved = await self._current_configuration(db, principal, version, configuration_digest)
        if saved is None:
            return None
        binding, frozen = saved
        tools = self._effective_tools(version, binding, frozen)
        return AgentToolGrant(tools, frozenset(self._tool_scopes[tool] for tool in tools))
