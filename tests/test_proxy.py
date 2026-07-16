from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from profact import store
from profact.adapters.claude_code_proxy import build_app
from profact.session import CaptureSession

SSE_STREAM = (
    'event: message_start\n'
    'data: {"type":"message_start","message":{"model":"claude-test"}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}\n\n'
    'event: content_block_delta\n'
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world"}}\n\n'
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
    assert store.read_head(repo_root, "main") == version.id  # HEAD advanced on close
