from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from profact import config, privacy, store
from profact.adapters import cursor_hooks
from profact.adapters.base import CaptureEvent
from profact.session import CaptureSession


def test_builtin_secrets_are_detected_redacted_and_idempotent(tmp_path: Path) -> None:
    github = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    aws = "AKIAABCDEFGHIJKLMNOP"
    jwt = "eyJabcdef.eyJghijkl.mnopqrst"
    text = f"github={github}\naws={aws}\ntoken={jwt}\n"

    findings = privacy.scan_for(tmp_path, text)
    redacted, redacted_findings = privacy.redact_for(tmp_path, text)
    again, repeated = privacy.redact_for(tmp_path, redacted)

    assert {finding.rule for finding in findings} == {
        "github-token",
        "aws-access-key",
        "jwt",
    }
    assert len(redacted_findings) == 3
    assert github not in redacted and aws not in redacted and jwt not in redacted
    assert "[REDACTED:github-token]" in redacted
    assert again == redacted
    assert repeated == []


def test_hashes_and_common_identifiers_are_not_flagged(tmp_path: Path) -> None:
    text = (
        "git=0123456789abcdef0123456789abcdef01234567\n"
        "sha256=" + "a" * 64 + "\n"
        "uuid=123e4567-e89b-12d3-a456-426614174000\n"
    )
    assert privacy.scan_for(tmp_path, text) == []


def test_findings_never_echo_other_secrets_on_the_same_line(tmp_path: Path) -> None:
    github = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    aws = "AKIAABCDEFGHIJKLMNOP"

    findings = privacy.scan_for(tmp_path, f"first={github} second={aws}")

    assert len(findings) == 2
    assert all(finding.masked_excerpt == "[SECRET]" for finding in findings)
    assert github not in repr(findings) and aws not in repr(findings)


def test_custom_pattern_and_named_secret_redact_only_sensitive_value(
    tmp_path: Path,
) -> None:
    (tmp_path / "profact.toml").write_text(
        '[privacy]\nredact_patterns = ["INTERNAL-[0-9]{8}"]\n',
        encoding="utf-8",
    )
    text = "OPENAI_API_KEY=ordinary-custom-secret\nref=INTERNAL-12345678\n"

    redacted, findings = privacy.redact_for(tmp_path, text, strict_config=True)

    assert "OPENAI_API_KEY=[REDACTED:named-secret]" in redacted
    assert "ref=[REDACTED:custom-1]" in redacted
    assert all("ordinary-custom-secret" not in finding.masked_excerpt for finding in findings)


def test_write_object_redacts_before_hashing(git_repo: Path) -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    digest = store.write_object(git_repo, f"token={secret}")
    stored = store.read_object(git_repo, digest)

    assert stored == "token=[REDACTED:github-token]"
    assert digest == hashlib.sha256(stored.encode("utf-8")).hexdigest()
    assert secret not in (store.objects_dir(git_repo) / digest).read_text()


def test_invalid_config_is_strict_for_commands_but_capture_keeps_builtin_redaction(
    git_repo: Path,
) -> None:
    (git_repo / "profact.toml").write_text("[privacy\n", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(git_repo)

    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    digest = store.write_object(git_repo, secret)

    assert store.read_object(git_repo, digest) == "[REDACTED:github-token]"
    assert (store.profact_dir(git_repo) / "privacy-warning.log").exists()


@pytest.mark.parametrize(
    "toml",
    [
        '[privacy]\nprivate_paths = ["!secret.env"]\n',
        '[privacy]\nprivate_paths = ["#secret.env"]\n',
        '[privacy]\nredact_patterns = ["^"]\n',
    ],
)
def test_config_rejects_policy_bypass_patterns(tmp_path: Path, toml: str) -> None:
    (tmp_path / "profact.toml").write_text(toml, encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(tmp_path)


def test_cursor_and_proxy_session_capture_use_the_same_redaction_boundary(
    git_repo: Path,
) -> None:
    cursor_secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    cursor_hooks.handle(
        git_repo,
        {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "cursor-conversation",
            "generation_id": "cursor-generation",
            "prompt": f"use {cursor_secret}",
        },
    )
    cursor_hooks.handle(
        git_repo,
        {
            "hook_event_name": "afterAgentResponse",
            "text": f"removed {cursor_secret}",
        },
    )
    cursor_hooks.handle(git_repo, {"hook_event_name": "stop"})

    proxy_secret = "AKIAABCDEFGHIJKLMNOP"
    timestamp = datetime.now(timezone.utc).isoformat()
    session = CaptureSession(git_repo)
    session.handle_event(
        CaptureEvent(
            "user",
            [{"type": "text", "text": f"configure {proxy_secret}"}],
            "claude-test",
            None,
            timestamp,
        )
    )
    session.handle_event(
        CaptureEvent(
            "assistant",
            [{"type": "text", "text": f"configured {proxy_secret}"}],
            "claude-test",
            None,
            timestamp,
        )
    )
    session.flush()

    with store.connect(git_repo) as conn:
        prompts = [
            version for version in store.all_versions(conn) if version.type == "prompt"
        ]
    assert len(prompts) == 2
    stored_parts: list[str] = []
    for version in prompts:
        if version.prompt_ref:
            stored_parts.append(store.read_object(git_repo, version.prompt_ref) or "")
        if version.response_ref:
            stored_parts.append(store.read_object(git_repo, version.response_ref) or "")
    flattened = "\n".join(stored_parts)
    assert cursor_secret not in flattened and proxy_secret not in flattened
    assert "[REDACTED:github-token]" in flattened
    assert "[REDACTED:aws-access-key]" in flattened