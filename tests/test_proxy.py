from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from profact import store
from profact.adapters.claude_code_proxy import (
    _SSEAssistantParser,
    _json_response_event,
    build_app,
)
from profact.session import CaptureSession

SSE_STREAM = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"claude-test","usage":{"input_tokens":10,"output_tokens":1,"cache_read_input_tokens":4,"cache_creation_input_tokens":3}}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world"}}\n\n'
    'event: message_delta\n'
    'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n'
    'event: message_stop\n'
    'data: {"type":"message_stop"}\n\n'
)


def _mock_upstream() -> Starlette:
    async def messages(request):
        return Response(SSE_STREAM, media_type="text/event-stream")

    return Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])])


def _prompt_body(text: str) -> dict:
    return {
        "model": "claude-test",
        "temperature": 1.0,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }


def _tool_result_body() -> dict:
    return {
        "model": "claude-test",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t", "content": "done"}],
            },
        ],
    }


async def _scenario(repo_root: Path) -> None:
    session = CaptureSession(repo_root)
    upstream = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_mock_upstream()), base_url="http://upstream"
    )
    app = build_app(session.handle_event, upstream)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    )

    response = await client.post("/v1/messages", json=_prompt_body("create a login page"))
    assert response.status_code == 200
    assert response.text == SSE_STREAM  # SSE passed through byte-for-byte

    # A tool-loop continuation must NOT open a second version.
    await client.post("/v1/messages", json=_tool_result_body())

    session.flush()
    await client.aclose()
    await upstream.aclose()


def test_proxy_creates_exactly_one_version(git_repo: Path):
    repo_root = git_repo
    asyncio.run(_scenario(repo_root))

    with store.connect(repo_root) as conn:
        prompts = [v for v in store.list_versions(conn, "main") if v.type == "prompt"]

    assert len(prompts) == 1
    version = prompts[0]
    assert version.tree_before  # captured before forwarding
    assert version.parent_id is not None  # chained off the anchor
    assert version.model == "claude-test"
    assert store.read_object(repo_root, version.prompt_ref) == "create a login page"
    assert version.response_ref is not None
    assert "Hello world" in store.read_object(repo_root, version.response_ref)
    assert version.input_tokens == 20
    assert version.output_tokens == 10
    assert version.cache_read_tokens == 8
    assert version.cache_creation_tokens == 6
    assert version.api_call_count == 2
    assert version.completed_at is not None
    assert version.completion_source == "claude-session-end"
    assert store.read_head(repo_root, "main") == version.id  # HEAD advanced on close


def test_streaming_usage_is_cumulative_within_one_api_call() -> None:
    parser = _SSEAssistantParser()
    parser.feed(
        b'data: {"type":"message_start","message":{"model":"m","usage":{"input_tokens":7,"output_tokens":1}}}\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":3}}\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":9}}\n'
    )

    event = parser.event()

    assert event.usage == {"input_tokens": 7, "output_tokens": 9}


def test_streaming_parser_preserves_utf8_split_across_chunks() -> None:
    parser = _SSEAssistantParser()
    payload = json.dumps(
        {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Done ✓"},
        },
        ensure_ascii=False,
    ).encode()
    stream = b"data: " + payload + b"\n"
    split_at = stream.index("✓".encode()) + 1

    parser.feed(stream[:split_at])
    parser.feed(stream[split_at:])

    assert parser.event().content_blocks == [{"type": "text", "text": "Done ✓"}]


def test_non_streaming_response_exposes_provider_usage() -> None:
    event = _json_response_event(
        b'{"role":"assistant","model":"m","content":[{"type":"text","text":"ok"}],'
        b'"usage":{"input_tokens":11,"output_tokens":6,"cache_read_input_tokens":2}}'
    )

    assert event is not None
    assert event.usage == {
        "input_tokens": 11,
        "output_tokens": 6,
        "cache_read_tokens": 2,
    }


def test_proxy_decodes_compressed_non_streaming_response() -> None:
    response_body = json.dumps(
        {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": "Completed ✓"}],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        },
        ensure_ascii=False,
    ).encode()

    async def scenario() -> None:
        async def messages(request):
            return Response(
                gzip.compress(response_body),
                media_type="application/json",
                headers={"content-encoding": "gzip"},
            )

        upstream_app = Starlette(
            routes=[Route("/v1/messages", messages, methods=["POST"])]
        )
        upstream = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upstream_app),
            base_url="http://upstream",
        )
        events = []
        proxy = build_app(events.append, upstream)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=proxy),
            base_url="http://proxy",
        )

        response = await client.post("/v1/messages", json=_prompt_body("test"))

        assert response.status_code == 200
        assert response.content == response_body
        assert response.headers.get("content-encoding") is None
        assert events[-1].role == "assistant"
        assert events[-1].content_blocks == [
            {"type": "text", "text": "Completed ✓"}
        ]
        await client.aclose()
        await upstream.aclose()

    asyncio.run(scenario())
