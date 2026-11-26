from typing import Annotated, Literal

from pydantic import Field

from kf_oj.contracts.judging import WireModel


class EditorCompletionRequest(WireModel):
    language: Literal["c17", "cpp20"]
    prepare: bool = False
    source: Annotated[str, Field(max_length=128 * 1024)]
    line: Annotated[int, Field(ge=0, lt=128 * 1024)]
    character: Annotated[int, Field(ge=0, le=128 * 1024)]


class EditorCompletion(WireModel):
    name: str
    kind: str


class EditorCompletionAccess(WireModel):
    enabled: bool
