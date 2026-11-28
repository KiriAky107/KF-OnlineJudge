"""An MCP session confined to the resources authorized for one review."""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta

from mcp import ClientSession, types
from mcp.server import Server
from mcp.shared.memory import create_connected_server_and_client_session

from kf_oj.contracts.learning_model import ReviewResourceName

_DESCRIPTIONS: dict[ReviewResourceName, str] = {
    "problem": "Read the public statement for the exact problem version under review.",
    "evaluations": (
        "Read aggregate submission verdict counts and the selected evaluation's coarse category. "
        "The counts describe submissions, not test cases. A missing category is inconclusive."
    ),
    "code": "Read code-change and compiler evidence summaries for the selected submission.",
    "batch_diagnostics": (
        "Read the registered batch diagnostics available for this submission and the confirmed "
        "outcomes from this conversation. This tool never queues or executes anything."
    ),
    "recent_practice": (
        "Read the authenticated learner's authorized recent practice overview. Use only when "
        "the current question needs this evidence and allows its use."
    ),
}


@asynccontextmanager
async def review_session(
    resources: Mapping[ReviewResourceName, Callable[[], Awaitable[str]]],
) -> AsyncIterator[ClientSession]:
    server = Server("kf-review-resources")
    tools = {
        name: types.Tool(
            name=name,
            description=_DESCRIPTIONS[name],
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=types.ToolAnnotations(readOnlyHint=True),
        )
        for name in resources
    }

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return list(tools.values())

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        loader = resources.get(name)
        if arguments or loader is None:
            return types.CallToolResult(content=[], isError=True)
        try:
            text = await loader()
        except (LookupError, PermissionError):
            return types.CallToolResult(content=[], isError=True)
        if not text or len(text.encode("utf-8")) > 16 * 1024:
            return types.CallToolResult(content=[], isError=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    async with create_connected_server_and_client_session(
        server, read_timeout_seconds=timedelta(seconds=4)
    ) as session:
        yield session
