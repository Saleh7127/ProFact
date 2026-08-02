"""Secret detection, redaction, and private-path policy helpers."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from . import config

REDACTION_PREFIX = "[REDACTED:"


@dataclass(frozen=True)
class Finding:
    rule: str
    line: int
    masked_excerpt: str


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    secret_group: str | None = None


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    rule: str


_BUILTIN_RULES = (
    _Rule(
        "private-key",
        re.compile(
            r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
            r"-----END [^-\r\n]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    _Rule(
        "github-token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|"
            r"github_pat_[A-Za-z0-9_]{20,255})\b"
        ),
    ),
    _Rule("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _Rule("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    _Rule("stripe-secret", re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b")),
    _Rule(
        "ai-api-key",
        re.compile(r"\bsk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    _Rule(
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\."
            r"[A-Za-z0-9_-]{5,}\b"
        ),
    ),
    _Rule(
        "named-secret",
        re.compile(
            r"(?im)\b(?:ANTHROPIC_API_KEY|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY|"
            r"GITHUB_TOKEN|SLACK_TOKEN|STRIPE_SECRET_KEY|DATABASE_URL)"
            r"\s*=\s*[\"']?(?P<secret>[^\s\"'#]{8,})"
        ),
        "secret",
    ),
    _Rule(
        "url-password",
        re.compile(
            r"(?i)\b[a-z][a-z0-9+.-]*://[^:\s/@]+:"
            r"(?P<secret>[^@\s/]{8,})@"
        ),
        "secret",
    ),
)


def scan_for(repo_root: Path, text: str, *, strict_config: bool = True) -> list[Finding]:
    policy = config.load(repo_root, strict=strict_config)
    return _findings(text, _rules(policy))


def redact_for(
    repo_root: Path, text: str, *, strict_config: bool = False
) -> tuple[str, list[Finding]]:
    policy = config.load(repo_root, strict=strict_config)
    matches = _matches(text, _rules(policy))
    findings = [_finding(text, match) for match in matches]
    redacted = text
    for match in reversed(matches):
        marker = f"{REDACTION_PREFIX}{match.rule}]"
        redacted = redacted[: match.start] + marker + redacted[match.end :]
    return redacted, findings


def is_private_path(repo_root: Path, rel_path: str) -> bool:
    path = rel_path.replace("\\", "/").lstrip("./")
    policy = config.load(repo_root, strict=False)
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in policy.private_paths)


def private_paths(repo_root: Path, *, strict: bool = False) -> tuple[str, ...]:
    return config.load(repo_root, strict=strict).private_paths


def _rules(policy: config.PrivacyConfig) -> tuple[_Rule, ...]:
    custom = tuple(
        _Rule(f"custom-{index}", re.compile(pattern, re.MULTILINE))
        for index, pattern in enumerate(policy.redact_patterns, start=1)
    )
    return _BUILTIN_RULES + custom


def _findings(text: str, rules: tuple[_Rule, ...]) -> list[Finding]:
    return [_finding(text, match) for match in _matches(text, rules)]


def _matches(text: str, rules: tuple[_Rule, ...]) -> list[_Match]:
    candidates: list[tuple[int, int, int, str]] = []
    for rule_index, rule in enumerate(rules):
        for match in rule.pattern.finditer(text):
            start, end = (
                match.span(rule.secret_group) if rule.secret_group else match.span()
            )
            if start != end:
                candidates.append((start, end, rule_index, rule.name))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

    result: list[_Match] = []
    occupied_until = -1
    for start, end, _rule_index, name in candidates:
        if start < occupied_until:
            continue
        result.append(_Match(start, end, name))
        occupied_until = end
    return result


def _finding(text: str, match: _Match) -> Finding:
    line = text.count("\n", 0, match.start) + 1
    return Finding(match.rule, line, "[SECRET]")