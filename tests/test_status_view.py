from __future__ import annotations

from pathlib import Path

from profact import status_view, store


def test_status_report_tracks_head_dirty_state_and_open_turn(git_repo: Path) -> None:
    clean = status_view.inspect(git_repo)
    assert clean.branch == "main"
    assert clean.head_id == "v1"
    assert clean.visible_versions == 1
    assert clean.dirty is False
    assert not clean.open_turn

    (git_repo / "a.txt").write_text("changed\n")
    dirty = status_view.inspect(git_repo)
    assert dirty.dirty is True

    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
        assert anchor is not None
        prompt = store.create_version(
            conn,
            git_branch="main",
            parent_id=anchor.id,
            tree_before=anchor.tree_after or anchor.tree_before,
        )
    store.write_head(git_repo, "main", prompt.id)

    open_report = status_view.inspect(git_repo, include_dirty=False)
    assert open_report.head_id == prompt.id
    assert open_report.open_turn
    assert open_report.dirty is None