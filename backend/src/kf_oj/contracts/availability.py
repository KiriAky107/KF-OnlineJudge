"""Root-to-API availability binding; independent from immutable judging protocols."""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from kf_oj.contracts.judging import integer_version

AVAILABILITY_PATH = "/etc/kf-admission/status.json"


class AvailabilityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Annotated[Literal[1], BeforeValidator(integer_version)]
    release_id: Annotated[str, Field(pattern=r"^sd-[0-9a-f]{12}$")]
    configuration_id: Annotated[str, Field(pattern=r"^cfg-[0-9a-f]{24}$")]
    state: Literal["open", "draining", "quiescent"]
