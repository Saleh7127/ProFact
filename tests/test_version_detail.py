from __future__ import annotations

from pathlib import Path

from profact import store, version_detail


def test_detail_loads_content_test_output_and_placeholders(git_repo: Path) -> None:
    with store.connect(git_repo) as conn:
        anchor = store.get_version(conn, "v1")
        assert anchor is not None
        prompt = store.create_version(
            conn,
            git_branch="main",
            parent_id=anchor.id,
            tree_before=anchor.tree_after or anchor.tree_before,
            tree_after=anchor.tree_after,
            prompt_ref=store.write_object(git_repo, "make it work"),
            response_ref=store.write_object(git_repo, "done"),
            thinking_source=store.THINKING_NONE,
        )
        result = store.set_test_result(
            conn,
            prompt.id,
            status="pass",
            exit_code=0,
            command="pytest -q",
            output_ref=store.write_object(git_repo, "1 passed\n"),
        )

    detail = version_detail.load(
        git_repo,
        prompt,
        head_id=prompt.id,
        tags=("working",),
        test_result=result,
    )

    assert detail.is_head
    assert detail.tags == ("working",)
    assert detail.prompt == "make it work"
    assert detail.response == "done"
    assert detail.test_output == "1 passed\n"
    assert detail.section("thinking") == version_detail.NO_THINKING_PLACEHOLDER
    assert detail.section("diff") == "(no file changes)"