from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from .base import CaptureEvent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7845
DEFAULT_UPSTREAM = "https://api.anthropic.com"

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_DROP_REQUEST_HEADERS = _HOP_BY_HOP | {"host", "content-length", "accept-encoding"}
_DROP_RESPONSE_HEADERS = _HOP_BY_HOP | {"content-encoding", "content-length"}

Emit = Callable[[CaptureEvent], None]

_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}


def build_app(emit: Emit, upstream: httpx.AsyncClient) -> Starlette:
    async def handler(request: Request) -> StreamingResponse:
        body = await request.body()
        is_messages = request.method == "POST" and request.url.path.rstrip("/").endswith(
            "/v1/messages"
        )

        if is_messages:
            event = _request_event(body)
            if event is not None:
                emit(event)

        upstream_request = upstream.build_request(
            request.method,
            request.url.path,
            params=httpx.QueryParams(request.url.query),
            headers=_forward_headers(request.headers),
            content=body,
        )
        upstream_response = await upstream.send(upstream_request, stream=True)

        is_sse = "text/event-stream" in upstream_response.headers.get("content-type", "")
        parser = _SSEAssistantParser() if (is_messages and is_sse) else None
        buffered = bytearray() if (is_messages and not is_sse) else None

        async def stream_body():
            try:
                async for chunk in upstream_response.aiter_raw():
                    if parser is not None:
                        parser.feed(chunk)
                    elif buffered is not None:
                        buffered.extend(chunk)
                    yield chunk
            finally:
                await upstream_response.aclose()
                if is_messages:
                    event = (
                        parser.event()
                        if parser is not None
                        else _json_response_event(bytes(buffered or b""))
                    )
                    if event is not None and event.content_blocks:
                        emit(event)

        return StreamingResponse(
            stream_body(),
            status_code=upstream_response.status_code,
            headers=_response_headers(upstream_response.headers),
        )

    return Starlette(
        routes=[
            Route(
                "/{path:path}",
                handler,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ]
    )


class ClaudeCodeProxyAdapter:
    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        upstream: str = DEFAULT_UPSTREAM,
    ) -> None:
        self.host = host
        self.port = port
        self.upstream = upstream
        self._callback: Emit | None = None
        self._client: httpx.AsyncClient | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def on_event(self, callback: Emit) -> None:
        self._callback = callback

    def start(self) -> None:
        self._client = httpx.AsyncClient(base_url=self.upstream, timeout=None)
        app = build_app(self._emit, self._client)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        while not self._server.started:
            time.sleep(0.02)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _emit(self, event: CaptureEvent) -> None:
        if self._callback is None:
            return
        try:
            self._callback(event)
        except Exception as exc:  # never let capture break the proxy
            print(f"profact: capture error: {exc}", file=sys.stderr)


class _SSEAssistantParser:
    def __init__(self) -> None:
        self._buffer = ""
        self._text_parts: list[str] = []
        self._model: str | None = None
        self._usage: dict[str, int] = {}

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk.decode("utf-8", errors="ignore")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                self._consume(json.loads(payload))
            except json.JSONDecodeError:
                continue

    def _consume(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "message_start":
            message = obj.get("message", {})
            self._model = message.get("model")
            self._usage.update(_normalize_usage(message.get("usage")))
        elif kind == "message_delta":
            # Anthropic streaming usage is cumulative within one API response;
            # overwrite repeated keys rather than adding them.
            self._usage.update(_normalize_usage(obj.get("usage")))
        elif kind == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                self._text_parts.append(delta.get("text", ""))

    def event(self) -> CaptureEvent:
        return CaptureEvent(
            role="assistant",
            content_blocks=[{"type": "text", "text": "".join(self._text_parts)}],
            model=self._model,
            timestamp=_now_iso(),
            usage=self._usage or None,
        )


def _request_event(body: bytes) -> CaptureEvent | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    return CaptureEvent(
        role=last.get("role", "user"),
        content_blocks=_normalize_content(last.get("content")),
        model=data.get("model"),
        temperature=data.get("temperature"),
        timestamp=_now_iso(),
    )


def _json_response_event(body: bytes) -> CaptureEvent | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    content = data.get("content")
    if content is None:
        return None
    return CaptureEvent(
        role=data.get("role", "assistant"),
        content_blocks=_normalize_content(content),
        model=data.get("model"),
        timestamp=_now_iso(),
        usage=_normalize_usage(data.get("usage")) or None,
    )


def _normalize_content(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _normalize_usage(value) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for source, target in _USAGE_FIELDS.items():
        amount = value.get(source)
        if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
            result[target] = amount
    return result


def _forward_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _DROP_REQUEST_HEADERS}


def _response_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
