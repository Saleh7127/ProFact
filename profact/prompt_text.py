"""Normalize provider-enriched prompt envelopes for storage and display."""

from __future__ import annotations

IDE_CONTEXT_HEADER = "# Context from my IDE setup:"
IDE_REQUEST_MARKER = "## My request:"
PROVIDER_CONTEXT_PREFIXES = (
    "<recommended_plugins>",
    "<environment_context>",
    "<skills_instructions>",
)


def user_request(text: str | None) -> str | None:
    """Return the actual request from Codex's optional IDE context envelope."""
    if not text:
        return text
    normalized = text.replace("\r\n", "\n")
    stripped = normalized.lstrip()
    if IDE_CONTEXT_HEADER in normalized:
        _context, marker, request = normalized.partition(IDE_REQUEST_MARKER)
        if marker and request.strip():
            return request.strip()
    if stripped.startswith(PROVIDER_CONTEXT_PREFIXES):
        return None
    return text
