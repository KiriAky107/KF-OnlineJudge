import json
from uuid import uuid4

import pytest

from kf_oj.contracts.agent_runtime import AgentPurpose
from kf_oj.modules.skills.public import (
    BuiltinSkills,
    SkillVersionUnavailable,
    load_builtin_skills,
    parse_builtin_skill,
)


def test_packaged_skills_resolve_by_fixed_version_and_validate_parameters():
    skills = load_builtin_skills()
    versions = tuple(skill for purpose in AgentPurpose for skill in skills.list_versions(purpose))
    assert {skill.declaration.purpose for skill in versions} == set(AgentPurpose)
    assert len(versions) == 6
    for skill in versions:
        assert skills.get(skill.version_id) is skill
        assert skill.validate_parameters(b"{}") == skill.declaration.parameters
        assert skill.parameters_schema()["additionalProperties"] is False
        with pytest.raises(ValueError):
            skill.validate_parameters(b'{"tools":["submit_as_another_user"]}')
    with pytest.raises(SkillVersionUnavailable):
        skills.get(uuid4())

    focused = skills.list_versions(AgentPurpose.FOCUSED_TRAINING)[0]
    selected = focused.validate_parameters(b'{"daily_minutes":15, "maximum_difficulty":"medium"}')
    assert selected.daily_minutes == 15
    assert selected.maximum_difficulty == "medium"
    with pytest.raises(ValueError):
        focused.validate_parameters(b'{"minimum_difficulty":"hard","maximum_difficulty":"easy"}')
    guided = skills.list_versions(AgentPurpose.GUIDED_HELP)[0]
    with pytest.raises(ValueError):
        guided.validate_parameters(b'{"allow_code_examples":"true"}')


def test_content_changes_create_distinct_versions_and_cannot_replace_a_release():
    original = load_builtin_skills().list_versions(AgentPurpose.FOCUSED_TRAINING)[0]
    document = original.declaration.model_dump(mode="json")
    changed = document | {"parameters": document["parameters"] | {"daily_minutes": 45}}
    v1 = parse_builtin_skill(json.dumps(document).encode())
    assert v1 == original
    duplicate = parse_builtin_skill(json.dumps(changed).encode())
    assert duplicate.digest != original.digest and duplicate.version_id != original.version_id
    with pytest.raises(ValueError, match="duplicate skill release"):
        BuiltinSkills((original, duplicate))

    v2 = parse_builtin_skill(json.dumps(changed | {"release": 2}).encode())
    catalog = BuiltinSkills((original, v2))
    assert catalog.get(original.version_id).validate_parameters(b"{}").daily_minutes == 30
    assert catalog.get(v2.version_id).validate_parameters(b"{}").daily_minutes == 45
    assert (
        catalog.get(v2.version_id).parameters_schema()["properties"]["daily_minutes"]["default"]
        == 45
    )
