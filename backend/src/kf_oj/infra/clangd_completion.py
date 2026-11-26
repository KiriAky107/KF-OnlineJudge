"""One-document, read-only language analysis in an isolated clangd process."""

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type CompletionLanguage = Literal["c17", "cpp20"]

_LANGUAGES = {"c17": ("c", "c17"), "cpp20": ("cpp", "c++20")}
_MAX_SOURCE_BYTES = 128 * 1024
_MAX_MESSAGE_BYTES = 512 * 1024
_MAX_RESULTS = 40
_MAX_MESSAGES = 64
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,127}")
_KINDS = {2: "method", 3: "function", 5: "field", 6: "variable", 7: "type", 13: "enum"}


class CompletionUnavailable(Exception):
    """The isolated service could not complete this request."""


@dataclass(frozen=True)
class SemanticCandidate:
    name: str
    kind: str


@dataclass(frozen=True)
class ClangdCompletion:
    toolchain_usr: Path
    bubblewrap: Path = Path("/usr/bin/bwrap")
    resource_limiter: Path = Path("/usr/bin/prlimit")

    async def available(self) -> bool:
        command = self._command()
        prefix = command[: command.index("/usr/bin/clangd") + 1]
        process = None
        try:
            async with asyncio.timeout(2):
                process = await asyncio.create_subprocess_exec(
                    *prefix,
                    "--version",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={},
                )
                return await process.wait() == 0
        except (OSError, TimeoutError):
            return False
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    def _command(self) -> tuple[str, ...]:
        return (
            str(self.resource_limiter),
            "--as=805306368:805306368",
            "--cpu=2:2",
            "--fsize=8388608:8388608",
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
            "--ro-bind",
            str(self.toolchain_usr),
            "/usr",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib",
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
            "--enable-config=false",
            "--background-index=false",
            "--clang-tidy=false",
            "--sync",
            "--completion-parse=always",
            "--header-insertion=never",
            f"--limit-results={_MAX_RESULTS}",
            "--log=error",
            "-j=1",
        )

    async def complete(
        self, language: CompletionLanguage, source: str, line: int, character: int
    ) -> tuple[SemanticCandidate, ...]:
        extension, standard = _LANGUAGES[language]
        if "\x00" in source or len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
            raise ValueError("invalid source")
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

        process = None
        try:
            async with asyncio.timeout(3):
                process = await asyncio.create_subprocess_exec(
                    *self._command(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={},
                )
                return await _query(process, extension, standard, source, line, character)
        except (OSError, EOFError, TimeoutError, ValueError, asyncio.LimitOverrunError):
            raise CompletionUnavailable from None
        finally:
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                # Consume the remainder of the pipes so wait cannot block on a full buffer.
                await process.communicate()


async def _query(process, extension, standard, source, line, character):
    stdin, stdout = process.stdin, process.stdout

    async def send(method, params, sequence=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if sequence is not None:
            message["id"] = sequence
        body = json.dumps(message, ensure_ascii=False).encode()
        stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await stdin.drain()

    async def result(sequence):
        for _ in range(_MAX_MESSAGES):
            headers = await stdout.readuntil(b"\r\n\r\n")
            match = re.search(rb"(?:^|\r\n)Content-Length: ([0-9]+)\r\n", headers)
            if match is None or not 0 < (length := int(match[1])) <= _MAX_MESSAGE_BYTES:
                raise ValueError("invalid response length")
            msg = json.loads(await stdout.readexactly(length))
            if not isinstance(msg, dict) or msg.get("id") != sequence:
                continue
            if "result" not in msg:
                raise ValueError("invalid response")
            return msg["result"]
        raise ValueError("missing response")

    uri = f"file:///work/source.{extension}"
    await send(
        "initialize",
        {
            "rootUri": "file:///work",
            "capabilities": {"general": {"positionEncodings": ["utf-16"]}},
            "initializationOptions": {"fallbackFlags": [f"-std={standard}"]},
        },
        1,
    )
    await result(1)
    await send("initialized", {})
    await send(
        "textDocument/didOpen",
        {"textDocument": {"uri": uri, "languageId": extension, "version": 1, "text": source}},
    )
    await send(
        "textDocument/completion",
        {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"triggerKind": 1},
        },
        2,
    )
    reply = await result(2)
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
