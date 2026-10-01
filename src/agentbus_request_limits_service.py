"""Bound inbound HTTP bodies before validation or credential hashing."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 65536


class RequestBodyLimit:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > MAX_BODY_BYTES:
            await self._reject(send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            item = await receive()
            if item["type"] != "http.request":
                return
            chunk = item.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                await self._reject(send)
                return
            chunks.append(chunk)
            if not item.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"detail":"request body exceeds 65536 bytes"}'
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})
