"""Load and resolve the versioned learning skill declarations."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from types import MappingProxyType
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from kf_oj.contracts.agent_runtime import AgentPurpose
from kf_oj.contracts.judging import WireModel
from kf_oj.modules.skills.definitions import (
    BUILTIN_DECLARATION,
    PARAMETERS_BY_PURPOSE,
    BuiltinDeclaration,
    RequestedTool,
)


class SkillVersionUnavailable(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeDeclaration:
    origin: Literal["published", "trial"]
    slug: str
    release: int
    purpose: AgentPurpose
    title: str
    description: str
    parameters: WireModel
    requested_tools: tuple[RequestedTool, ...]
    archived_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SkillVersion[Declaration: (BuiltinDeclaration, RuntimeDeclaration)]:
    version_id: UUID
    digest: str
    declaration: Declaration

    def parameters_schema(self) -> dict:
        defaults = self.declaration.parameters
        schema = type(defaults).model_json_schema()
        for key, value in defaults.model_dump(mode="json").items():
            schema["properties"][key]["default"] = value
        return schema

    def validate_parameters(self, raw: bytes) -> WireModel:
        if len(raw) > 4096:
            raise ValueError("skill parameters exceed their size limit")
        overrides = json.loads(raw)
        if not isinstance(overrides, dict):
            raise ValueError("expected skill parameter object")
        defaults = self.declaration.parameters
        combined = defaults.model_dump(mode="json") | overrides
        return type(defaults).model_validate_json(json.dumps(combined), strict=True)


type BuiltinSkillVersion = SkillVersion[BuiltinDeclaration]
type RuntimeSkillVersion = BuiltinSkillVersion | SkillVersion[RuntimeDeclaration]


def stored_skill_version(
    version_id: UUID,
    skill_id: UUID,
    release: int,
    digest: str,
    document: dict,
    *,
    origin: Literal["published", "trial"],
) -> SkillVersion[RuntimeDeclaration]:
    purpose = AgentPurpose(document["purpose"])
    declaration = RuntimeDeclaration(
        origin=origin,
        slug=f"{origin}-{skill_id.hex}",
        release=release,
        purpose=purpose,
        title=document.get("title", ""),
        description=document.get("description", ""),
        parameters=PARAMETERS_BY_PURPOSE[purpose].model_validate_json(
            json.dumps(document["parameters"]), strict=True
        ),
        requested_tools=tuple(
            RequestedTool.model_validate(tool) for tool in document["requested_tools"]
        ),
    )
    return SkillVersion(version_id, digest, declaration)


def parse_builtin_skill(raw: bytes) -> BuiltinSkillVersion:
    if len(raw) > 16_384:
        raise ValueError("skill declaration exceeds its size limit")
    declaration = BUILTIN_DECLARATION.validate_json(raw, strict=True)
    canonical = json.dumps(
        json.loads(raw),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    version_id = uuid5(
        NAMESPACE_URL, f"urn:kf-onlinejudge:skill:{declaration.slug}:{declaration.release}:{digest}"
    )
    return SkillVersion(version_id, digest, declaration)


class BuiltinSkills:
    def __init__(self, versions: Iterable[BuiltinSkillVersion]):
        indexed: dict[UUID, BuiltinSkillVersion] = {}
        releases: set[tuple[str, int]] = set()
        for version in versions:
            key = version.declaration.slug, version.declaration.release
            if key in releases or version.version_id in indexed:
                raise ValueError(f"duplicate skill release: {key[0]} {key[1]}")
            releases.add(key)
            indexed[version.version_id] = version
        self._versions = MappingProxyType(indexed)

    def list_versions(self, purpose: AgentPurpose) -> tuple[BuiltinSkillVersion, ...]:
        return tuple(
            version for version in self._versions.values() if version.declaration.purpose == purpose
        )

    def get(self, version_id: UUID) -> BuiltinSkillVersion:
        try:
            return self._versions[version_id]
        except KeyError:
            raise SkillVersionUnavailable("skill version unavailable") from None


def load_builtin_skills() -> BuiltinSkills:
    declarations = files(__package__).joinpath("builtin")
    versions = []
    for source in sorted(declarations.iterdir(), key=lambda path: path.name):
        if not source.name.endswith(".json"):
            continue
        try:
            versions.append(parse_builtin_skill(source.read_bytes()))
        except (OSError, ValueError) as error:
            raise ValueError(f"invalid builtin skill: {source.name}") from error
    skills = BuiltinSkills(versions)
    if any(not skills.list_versions(purpose) for purpose in AgentPurpose):
        raise ValueError("a builtin learning purpose has no skill")
    return skills
