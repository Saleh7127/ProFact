from __future__ import annotations

import hashlib
from pathlib import Path

from typer.testing import CliRunner

from profact import privacy_audit, store
from profact.cli import app

runner = CliRunner()


def _raw_object(repo: Path, text: str) -> str:
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    path = store.objects_dir(repo) / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return digest


def _seed_unsafe_history(repo: Path) -> tuple[str, str, str]:
    prompt_secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    output_secret = "AKIAABCDEFGHIJKLMNOP"
    prompt_ref = _raw_object(repo, f"use token {prompt_secret}")
    output_ref = _raw_object(repo, f"credential={output_secret}\n")
    with store.connect(repo) as conn:
        anchor = store.get_version(conn, "v1")
        assert anchor is not None
        version = store.create_version(
            conn,
            git_branch="main",
            parent_id=anchor.id,
            tree_before=anchor.tree_after or anchor.tree_before,
            tree_after=anchor.tree_after,
            prompt_ref=prompt_ref,
        )
        store.set_test_result(
            conn,
            version.id,
            status="fail",
            exit_code=1,
            command="pytest -q",
            output_ref=output_ref,
        )
    return version.id, prompt_secret, output_secret


def test_privacy_audit_reports_masked_findings_and_fixes_references(
    git_repo: Path,
) -> None:
    version_id, prompt_secret, output_secret = _seed_unsafe_history(git_repo)
    orphan_secret = "sk_live_abcdefghijklmnopqrstuv"
    orphan_ref = _raw_object(git_repo, f"orphan={orphan_secret}")

    report = privacy_audit.scan(git_repo)
    assert len(report.findings) == 3
    assert {finding.section for finding in report.findings} == {
        "prompt",
        "test-output",
        "orphan-object",
    }
    assert all(prompt_secret not in finding.masked_excerpt for finding in report.findings)
    assert all(output_secret not in finding.masked_excerpt for finding in report.findings)

    fixed = privacy_audit.scan(git_repo, fix=True)
    assert fixed.updated_references == 2
    assert fixed.replaced_objects == 2
    assert fixed.removed_unsafe_objects == 3
    assert not (store.objects_dir(git_repo) / orphan_ref).exists()
    with store.connect(git_repo) as conn:
        version = store.get_version(conn, version_id)
        result = store.get_test_result(conn, version_id)
        referenced = store.referenced_object_ids(conn)
    assert version is not None and result is not None
    assert "[REDACTED:github-token]" in (
        store.read_object(git_repo, version.prompt_ref) or ""
    )
    assert "[REDACTED:aws-access-key]" in (
        store.read_object(git_repo, result.output_ref) or ""
    )
    assert all(
        secret not in path.read_text(encoding="utf-8")
        for path in store.objects_dir(git_repo).iterdir()
        if path.is_file()
        for secret in (prompt_secret, output_secret, orphan_secret)
    )
    assert privacy_audit.scan(git_repo).findings == ()


def test_privacy_scan_cli_never_prints_secret(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    _version_id, prompt_secret, output_secret = _seed_unsafe_history(git_repo)

    report = runner.invoke(app, ["privacy", "scan"])
    assert report.exit_code == 0, report.output
    assert "github-token" in report.output and "aws-access-key" in report.output
    assert prompt_secret not in report.output and output_secret not in report.output
    assert "[SECRET]" in report.output

    fixed = runner.invoke(app, ["privacy", "scan", "--fix"])
    assert fixed.exit_code == 0, fixed.output
    assert "updated 2 reference(s)" in fixed.output
    assert "removed 2 unsafe" in fixed.output
    clean = runner.invoke(app, ["privacy", "scan"])
    assert "No stored secrets detected" in clean.output


def test_privacy_scan_rejects_invalid_config(git_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(git_repo)
    (git_repo / "profact.toml").write_text(
        '[privacy]\nredact_patterns = ["("]\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["privacy", "scan"])

    assert result.exit_code == 2
    assert "invalid profact.toml" in result.output