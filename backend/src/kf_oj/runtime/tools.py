"""Versioned, server-owned tools and their dispatch boundary."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError


class ToolDenied(PermissionError):
    """The tool or the underlying run is unavailable to this context."""


class ToolBudgetExceeded(ValueError):
    """The next tool call would exceed a server budget."""


class ToolArgumentsInvalid(ValueError):
    """Tool input failed size or schema checks."""


class ToolResultInvalid(ValueError):
    """The tool returned a result outside its declared contract or budget."""


class ToolTimedOut(TimeoutError):
    def __init__(self, *, retryable: bool):
        super().__init__("Agent tool timed out")
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ToolKey:
    name: str
    version: int


SOURCE_EVIDENCE = ToolKey("source_evidence", 1)
SUBMISSION_HISTORY = ToolKey("submission_history", 1)
TRAINING_CANDIDATES = ToolKey("training_candidates", 1)


@dataclass(frozen=True, slots=True)
class AgentToolGrant:
    tools: frozenset[ToolKey]
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class ToolContext:
    run_id: UUID
    run_revision: int


@dataclass(frozen=True, slots=True)
class Tool[Arguments: BaseModel, Result: BaseModel]:
    key: ToolKey
    scope: str
    input_model: type[Arguments]
    output_model: type[Result]
    handler: Callable[[Arguments], Awaitable[Result]]
    read_only: bool
    timeout_seconds: int
    retry_on_timeout: bool = False


class ToolPolicy(Protocol):
    async def reserve(self, context: ToolContext, tool: Tool) -> UUID:
        """Recheck authorization and atomically charge an attempt before its handler starts."""
        ...

    async def require_current(self, context: ToolContext, tool: Tool) -> None:
        """Recheck authorization before returning a result."""
        ...

    async def release(self, reservation_id: UUID) -> None:
        """Return the concurrent slot while retaining the charged attempt."""
        ...


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool]):
        entries: dict[ToolKey, Tool] = {}
        for tool in tools:
            if tool.key in entries or not 1 <= tool.timeout_seconds <= 30:
                raise ValueError("duplicate or unbounded Agent tool")
            if not tool.read_only and tool.retry_on_timeout:
                raise ValueError("write tools require durable retry handling")
            entries[tool.key] = tool
        self._tools = MappingProxyType(entries)

    @staticmethod
    def _validated_result(tool: Tool, output) -> BaseModel:
        try:
            result = tool.output_model.model_validate(output)
        except ValidationError:
            raise ToolResultInvalid("invalid Agent tool result") from None
        if len(result.model_dump_json().encode()) > 65_536:
            raise ToolResultInvalid("Agent tool result exceeds its size limit")
        return result

    async def restore(
        self, policy: ToolPolicy, context: ToolContext, key: ToolKey, output: dict
    ) -> BaseModel:
        tool = self._tools.get(key)
        if tool is None:
            raise ToolDenied("Agent tool unavailable")
        await policy.require_current(context, tool)
        return self._validated_result(tool, output)

    async def invoke(
        self, policy: ToolPolicy, context: ToolContext, key: ToolKey, arguments: bytes
    ) -> BaseModel:
        tool = self._tools.get(key)
        if tool is None:
            raise ToolDenied("Agent tool unavailable")
        if len(arguments) > 16_384:
            raise ToolArgumentsInvalid("Agent tool arguments exceed their size limit")
        try:
            parsed = tool.input_model.model_validate_json(arguments, strict=True)
        except ValidationError:
            raise ToolArgumentsInvalid("invalid Agent tool arguments") from None

        reservation = await policy.reserve(context, tool)
        try:
            try:
                async with asyncio.timeout(tool.timeout_seconds):
                    output = await tool.handler(parsed)
            except TimeoutError:
                raise ToolTimedOut(retryable=tool.retry_on_timeout) from None
            result = self._validated_result(tool, output)
            await policy.require_current(context, tool)
            return result
        finally:
            await policy.release(reservation)
