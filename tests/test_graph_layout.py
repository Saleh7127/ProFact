from __future__ import annotations

from profact import graph_layout, store


def _version(identifier: str, parent_id: str | None) -> store.Version:
    return store.Version(
        id=identifier,
        parent_id=parent_id,
        type="prompt",
        git_branch="main",
        timestamp=f"2026-01-01T00:00:0{identifier[1:]}+00:00",
        prompt_ref=None,
        response_ref=None,
        model=None,
        temperature=None,
        diff_ref=None,
        tree_before="tree",
        tree_after="tree",
        commit_hash=None,
        confidence="high",
    )


def test_layout_represents_a_fork_and_shared_parent() -> None:
    versions = [
        _version("v3", "v1"),
        _version("v2", "v1"),
        _version("v1", None),
    ]

    rows = graph_layout.layout(versions)

    assert [row.version.id for row in rows] == ["v3", "v2", "v1"]
    assert all("*" in row.node for row in rows)
    assert any(row.connector and "/" in row.connector for row in rows)
    assert graph_layout.history_shape(versions) == graph_layout.HistoryShape(
        tips=("v3", "v2"),
        branch_points=("v1",),
    )


def test_layout_marks_parent_outside_the_visible_slice() -> None:
    row = graph_layout.layout([_version("v2", "v1")])[0]

    assert not row.parent_visible
    assert row.continuation.strip() == ""