"""Repository configuration loaded from ``profact.toml``."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "profact.toml"


class ConfigError(ValueError):
    """Raised when repository configuration is malformed or unsafe."""


@dataclass(frozen=True)
class PrivacyConfig:
    private_paths: tuple[str, ...] = ()
    redact_patterns: tuple[str, ...] = ()


def load(repo_root: Path, *, strict: bool = True) -> PrivacyConfig:
    """Load privacy policy, or safe built-in defaults when absent.

    Capture callers use ``strict=False``: malformed custom policy is ignored,
    but built-in secret redaction remains active and a local warning is written.
    Interactive validation uses the default strict behavior.
    """
    path = repo_root / CONFIG_NAME
    if not path.exists():
        return PrivacyConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        privacy = data.get("privacy", {})
        if not isinstance(privacy, dict):
            raise ConfigError("[privacy] must be a TOML table")
        private_paths = tuple(
            _normalize_private_path(value)
            for value in _string_list(privacy, "private_paths")
        )
        redact_patterns = tuple(_string_list(privacy, "redact_patterns"))
        for index, pattern in enumerate(redact_patterns, start=1):
            try:
                compiled = re.compile(pattern, re.MULTILINE)
            except re.error as exc:
                raise ConfigError(
                    f"privacy.redact_patterns[{index}] is invalid: {exc}"
                ) from exc
            if compiled.search("") is not None:
                raise ConfigError(
                    f"privacy.redact_patterns[{index}] cannot match empty text"
                )
        return PrivacyConfig(
            private_paths=private_paths,
            redact_patterns=redact_patterns,
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ConfigError) as exc:
        error = exc if isinstance(exc, ConfigError) else ConfigError(str(exc))
        if strict:
            raise error
        _write_warning(repo_root, error)
        return PrivacyConfig()


def _string_list(table: dict, key: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"privacy.{key} must be an array of strings")
    return value


def _normalize_private_path(value: str) -> str:
    pattern = value.strip().replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if not pattern:
        raise ConfigError("privacy.private_paths cannot contain an empty pattern")
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:/", pattern):
        raise ConfigError(f"private path must be repository-relative: {value!r}")
    if pattern.startswith(("!", "#")):
        raise ConfigError(
            f"private path cannot start with ignore-control syntax: {value!r}"
        )
    if ".." in pattern.split("/"):
        raise ConfigError(f"private path cannot leave the repository: {value!r}")
    if "\x00" in pattern:
        raise ConfigError("private path cannot contain a NUL byte")
    if pattern.endswith("/"):
        pattern += "**"
    return pattern


def _write_warning(repo_root: Path, error: Exception) -> None:
    try:
        directory = repo_root / ".profact"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "privacy-warning.log").write_text(
            f"Ignored invalid {CONFIG_NAME}; built-in redaction remains active: {error}\n",
            encoding="utf-8",
        )
    except OSError:
        pass