from typing import Annotated, Literal

from pydantic import Field

from kf_oj.contracts.judging import Identifier, Label, Language, RunLimits, WireModel


class PublicSample(WireModel):
    encoding: Literal["base64"] = "base64"
    input: Annotated[str, Field(max_length=87384)]
    output: Annotated[str, Field(max_length=87384)]


class ProblemSummary(WireModel):
    id: Identifier
    slug: Label
    version_id: Identifier
    revision: int
    title: str
    languages: tuple[Language, ...]


class ProblemDetail(ProblemSummary):
    statement: str
    samples: tuple[PublicSample, ...]
    run_limits: RunLimits


class ProblemPage(WireModel):
    items: tuple[ProblemSummary, ...]
    next_after: Label | None
