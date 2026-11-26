"""Bounded local IPC for a clangd service outside the web container."""

import argparse
import asyncio
import json
import os
import re
import socket
from dataclasses import asdict, dataclass
from pathlib import Path

from .clangd_completion import (
    ClangdCompletion,
    CompletionLanguage,
    CompletionUnavailable,
    SemanticCandidate,
)

_LIMIT = 800 * 1024
_SCOPE = re.compile(r"[0-9a-f-]{36}/[0-9a-f-]{36}")


def _frame(data) -> bytes:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(raw) > _LIMIT:
        raise ValueError("completion message too large")
    return raw


async def _read(reader):
    return json.loads(await reader.readuntil(b"\n"))


@dataclass(frozen=True, slots=True)
class SocketClangdCompletion:
    path: Path

    async def _request(self, message):
        writer = None
        try:
            async with asyncio.timeout(8):
                reader, writer = await asyncio.open_unix_connection(self.path, limit=_LIMIT)
                writer.write(_frame(message))
                await writer.drain()
                return await _read(reader)
        except (OSError, EOFError, TimeoutError, ValueError, asyncio.LimitOverrunError):
            raise CompletionUnavailable from None
        finally:
            if writer is not None:
                writer.close()

    async def available(self) -> bool:
        try:
            return await self._request({"kind": "probe"}) is True
        except CompletionUnavailable:
            return False

    async def prepare(self, language: CompletionLanguage, source: str, scope: str) -> None:
        await self._request(dict(kind="prepare", language=language, source=source, scope=scope))

    async def complete(
        self,
        language: CompletionLanguage,
        source: str,
        line: int,
        character: int,
        scope: str | None = None,
    ) -> tuple[SemanticCandidate, ...]:
        message = dict(
            kind="complete", language=language, source=source, line=line, character=character
        )
        if scope is not None:
            message["scope"] = scope
        result = await self._request(message)
        if not isinstance(result, list) or len(result) > 40:
            raise CompletionUnavailable
        try:
            return tuple(SemanticCandidate(**item) for item in result)
        except TypeError:
            raise CompletionUnavailable from None


async def serve(listener: socket.socket, completion: ClangdCompletion) -> None:
    if not await completion.available():
        raise SystemExit("isolated clangd probe failed")
    slot = asyncio.Semaphore(1)

    async def handle(reader, writer):
        result = None
        try:
            async with asyncio.timeout(1):
                message = await _read(reader)
            if message == {"kind": "probe"}:
                result = True
            elif isinstance(message, dict) and not slot.locked():
                kind, language, source = (
                    message.get(name) for name in ("kind", "language", "source")
                )
                scope = message.get("scope")
                scoped = isinstance(scope, str) and _SCOPE.fullmatch(scope) is not None
                if language in ("c17", "cpp20") and isinstance(source, str):
                    async with slot:
                        if kind == "prepare" and scoped:
                            await completion.prepare(language, source, scope)
                            result = True
                        elif kind == "complete" and (scoped or "scope" not in message):
                            line, character = message.get("line"), message.get("character")
                            if type(line) is int and type(character) is int:
                                items = await completion.complete(
                                    language, source, line, character, scope
                                )
                                result = [asdict(item) for item in items]
        except (
            OSError,
            EOFError,
            TimeoutError,
            ValueError,
            asyncio.LimitOverrunError,
            CompletionUnavailable,
        ):
            pass
        try:
            async with asyncio.timeout(1):
                writer.write(_frame(result))
                await writer.drain()
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()

    try:
        async with await asyncio.start_unix_server(handle, sock=listener, limit=_LIMIT) as server:
            await server.serve_forever()
    finally:
        await completion.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchain-usr", type=Path, required=True)
    options = parser.parse_args()
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("requires one systemd listening socket")
    with socket.socket(fileno=3) as listener:
        if listener.family != socket.AF_UNIX or not listener.getsockopt(
            socket.SOL_SOCKET, socket.SO_ACCEPTCONN
        ):
            raise SystemExit("expected a listening Unix socket")
        asyncio.run(serve(listener, ClangdCompletion(options.toolchain_usr)))


if __name__ == "__main__":
    main()
