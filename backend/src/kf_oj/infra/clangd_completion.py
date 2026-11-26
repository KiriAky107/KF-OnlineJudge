"""Read-only language analysis in an isolated clangd process."""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

type CompletionLanguage = Literal["c17", "cpp20"]

_LANGUAGES = {"c17": ("c", "c17"), "cpp20": ("cpp", "c++20")}
_MAX_SOURCE_BYTES = 128 * 1024
_MAX_MESSAGE_BYTES = 512 * 1024
_MAX_RESULTS = 40
_MAX_MESSAGES = 64
_IDLE_SECONDS = 30
_LIFETIME_SECONDS = 90
_CONFIG = b"Index:\n  StandardLibrary: No\n"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,127}")
_KINDS = {2: "method", 3: "function", 5: "field", 6: "variable", 7: "type", 13: "enum"}


class CompletionUnavailable(Exception):
    """The isolated service could not complete this request."""


@dataclass(frozen=True)
class SemanticCandidate:
    name: str
    kind: str


@dataclass
class ClangdCompletion:
    toolchain_usr: Path
    bubblewrap: Path = Path("/usr/bin/bwrap")
    resource_limiter: Path = Path("/usr/bin/prlimit")

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _document: _ClangdDocument | None = field(default=None, init=False)
    _scope: tuple[str, CompletionLanguage] | None = field(default=None, init=False)
    _expires: float = field(default=0, init=False)
    _idle: asyncio.TimerHandle | None = field(default=None, init=False)

    async def available(self) -> bool:
        process = None
        try:
            async with asyncio.timeout(2):
                process = await self._start("--version")
                return await process.wait() == 0
        except (OSError, TimeoutError):
            return False
        finally:
            if process is not None:
                await _stop(process)

    async def _start(self, *options: str) -> asyncio.subprocess.Process:
        # Only the server's fixed configuration is visible in the sandbox.
        fd = os.memfd_create("clangd-config")
        try:
            os.write(fd, _CONFIG)
            os.lseek(fd, 0, os.SEEK_SET)
            return await asyncio.create_subprocess_exec(
                *self._command(fd),
                *options,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={},
                pass_fds=(fd,),
            )
        finally:
            os.close(fd)

    def _command(self, config_fd: int) -> tuple[str, ...]:
        lib64 = "usr/lib64" if (self.toolchain_usr / "lib64").is_dir() else "usr/lib"
        return (
            str(self.resource_limiter),
            "--as=805306368:805306368",
            "--cpu=6:6",
            "--fsize=33554432:33554432",
            "--nofile=64:64",
            "--core=0:0",
            "--",
            str(self.bubblewrap),
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin",
            "--setenv",
            "HOME",
            "/work",
            "--setenv",
            "XDG_CONFIG_HOME",
            "/config",
            "--ro-bind-data",
            str(config_fd),
            "/config/clangd/config.yaml",
            "--ro-bind",
            str(self.toolchain_usr),
            "/usr",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            lib64,
            "/lib64",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/work",
            "--chdir",
            "/work",
            "--remount-ro",
            "/",
            "--",
            "/usr/bin/clangd",
            "--enable-config=true",
            "--background-index=false",
            "--clang-tidy=false",
            "--sync",
            "--completion-parse=always",
            "--header-insertion=never",
            f"--limit-results={_MAX_RESULTS}",
            "--log=error",
        )

    async def close(self) -> None:
        async with self._lock:
            await self._discard()

    async def _discard(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        document, self._document = self._document, None
        self._scope = None
        if document is not None:
            await _stop(document.process)

    def _expire(self, document: _ClangdDocument) -> None:
        async def expire():
            async with self._lock:
                if self._document is document and self._idle is handle:
                    await self._discard()

        handle = self._idle
        asyncio.create_task(expire())

    @asynccontextmanager
    async def _open(self, language: CompletionLanguage, scope: str | None, *, preparing=False):
        async with self._lock:
            loop = asyncio.get_running_loop()
            key = (scope, language) if scope is not None else None
            if self._document and (
                self._document.process.returncode is not None or loop.time() >= self._expires
            ):
                await self._discard()
            if preparing and self._document is not None:
                # Warming is optional. Leave a recently used document available.
                yield None
                return
            if key is None or self._scope != key:
                await self._discard()
            try:
                async with asyncio.timeout(7):
                    if self._document is None:
                        self._document = _ClangdDocument(await self._start(), language)
                        self._scope = key
                        self._expires = loop.time() + _LIFETIME_SECONDS
                        await self._document.initialize()
                    yield self._document
            except (OSError, EOFError, TimeoutError, ValueError, asyncio.LimitOverrunError):
                await self._discard()
                raise CompletionUnavailable from None
            except asyncio.CancelledError:
                await self._discard()
                raise
            finally:
                if self._document is not None:
                    if key is None:
                        await self._discard()
                    else:
                        if self._idle is not None:
                            self._idle.cancel()
                        self._idle = loop.call_at(
                            min(loop.time() + _IDLE_SECONDS, self._expires),
                            self._expire,
                            self._document,
                        )

    async def prepare(self, language: CompletionLanguage, source: str, scope: str) -> None:
        _validate_source(source)
        async with self._open(language, scope, preparing=True) as document:
            if document is not None:
                await document.update(source)
                await document.request("textDocument/documentSymbol", document.params)

    async def complete(
        self,
        language: CompletionLanguage,
        source: str,
        line: int,
        character: int,
        scope: str | None = None,
    ) -> tuple[SemanticCandidate, ...]:
        _validate_source(source)
        lines = source.split("\n")
        if not 0 <= line < len(lines):
            raise ValueError("invalid position")
        current = lines[line].removesuffix("\r").encode("utf-16-le")
        if not 0 <= character * 2 <= len(current):
            raise ValueError("invalid position")
        try:
            current[: character * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            raise ValueError("invalid position") from None

        async with self._open(language, scope) as document:
            assert document is not None
            await document.update(source)
            reply = await document.request(
                "textDocument/completion",
                document.params
                | {
                    "position": {"line": line, "character": character},
                    "context": {"triggerKind": 1},
                },
            )
        return _candidates(reply)


def _validate_source(source: str) -> None:
    if "\x00" in source or len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("invalid source")


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    # Drain buffered output so wait cannot block on a full pipe.
    await process.communicate()


class _ClangdDocument:
    def __init__(self, process: asyncio.subprocess.Process, language: CompletionLanguage):
        self.process = process
        self.extension, self.standard = _LANGUAGES[language]
        self.params = {"textDocument": {"uri": f"file:///work/source.{self.extension}"}}
        self.sequence = 0
        self.version = 0
        self.source = None

    async def initialize(self):
        await self.request(
            "initialize",
            {
                "rootUri": "file:///work",
                "capabilities": {"general": {"positionEncodings": ["utf-16"]}},
                "initializationOptions": {"fallbackFlags": [f"-std={self.standard}"]},
            },
        )
        await self.send("initialized", {})

    async def update(self, source: str):
        if source == self.source:
            return
        self.version += 1
        document = self.params["textDocument"] | {"version": self.version}
        if self.version == 1:
            await self.send(
                "textDocument/didOpen",
                {"textDocument": document | {"languageId": self.extension, "text": source}},
            )
        else:
            await self.send(
                "textDocument/didChange",
                {
                    "textDocument": document,
                    "contentChanges": [{"text": source}],
                    "wantDiagnostics": False,
                },
            )
        self.source = source

    async def send(self, method, params, sequence=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if sequence is not None:
            message["id"] = sequence
        body = json.dumps(message, ensure_ascii=False).encode()
        stdin = self.process.stdin
        stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await stdin.drain()

    async def request(self, method, params):
        self.sequence += 1
        await self.send(method, params, self.sequence)
        stdout = self.process.stdout
        for _ in range(_MAX_MESSAGES):
            headers = await stdout.readuntil(b"\r\n\r\n")
            match = re.search(rb"(?:^|\r\n)Content-Length: ([0-9]+)\r\n", headers)
            if match is None or not 0 < (length := int(match[1])) <= _MAX_MESSAGE_BYTES:
                raise ValueError("invalid response length")
            msg = json.loads(await stdout.readexactly(length))
            if not isinstance(msg, dict) or msg.get("id") != self.sequence:
                continue
            if "result" not in msg:
                raise ValueError("invalid response")
            return msg["result"]
        raise ValueError("missing response")


def _candidates(reply) -> tuple[SemanticCandidate, ...]:
    if isinstance(reply, dict):
        reply = reply.get("items")
    if not isinstance(reply, list):
        return ()
    candidates = {}
    for item in reply[:_MAX_RESULTS]:
        if not isinstance(item, dict):
            continue
        name, kind = item.get("filterText"), item.get("kind")
        if isinstance(name, str) and _IDENTIFIER.fullmatch(name) and isinstance(kind, int):
            if kind in _KINDS:
                candidates.setdefault(name, SemanticCandidate(name, _KINDS[kind]))
    return tuple(candidates.values())
