"""Bound request bytes and upload time before schema parsing can allocate large inputs."""

import asyncio

from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        limit = 8192 if scope["path"] == "/api/v1/auth/login" else 1024 * 1024
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length is not None:
            try:
                size = int(length)
                if size < 0 or size > limit:
                    raise ValueError
            except ValueError:
                await JSONResponse({"detail": "request body outside byte policy"}, 413)(
                    scope, receive, send
                )
                return
        # Buffer a finite body so errors can be returned before response headers are sent.
        chunks, size, received_chunks = [], 0, 0
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body = message.get("body", b"")
                    size += len(body)
                    received_chunks += 1
                    if size > limit or received_chunks > 1024:
                        await JSONResponse({"detail": "request body outside byte policy"}, 413)(
                            scope, receive, send
                        )
                        return
                    chunks.append(body)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"detail": "request body deadline exceeded"}, 408)(
                scope, receive, send
            )
            return
        body = b"".join(chunks)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
