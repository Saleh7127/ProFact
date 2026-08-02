"""Pure lane layout and topology helpers for ProFact version DAGs."""

from __future__ import annotations

from dataclasses import dataclass

from . import store


@dataclass(frozen=True)
class GraphRow:
    version: store.Version
    node: str
    continuation: str
    connector: str | None
    column: int
    parent_visible: bool


@dataclass(frozen=True)
class HistoryShape:
    tips: tuple[str, ...]
    branch_points: tuple[str, ...]


def layout(versions: list[store.Version]) -> list[GraphRow]:
    """Lay out newest-first versions using stable ASCII graph lanes."""
    visible_ids = {version.id for version in versions}
    lanes: list[str | None] = []
    rows: list[GraphRow] = []

    for version in versions:
        if version.id in lanes:
            column = lanes.index(version.id)
        elif None in lanes:
            column = lanes.index(None)
            lanes[column] = version.id
        else:
            lanes.append(version.id)
            column = len(lanes) - 1

        node = "".join(
            "*" if index == column else ("|" if lane is not None else " ")
            for index, lane in enumerate(lanes)
        )
        parent_visible = version.parent_id in visible_ids
        detail_lanes = list(lanes)
        if not parent_visible:
            detail_lanes[column] = None
        continuation = "".join(
            "|" if lane is not None else " " for lane in detail_lanes
        )

        parent = version.parent_id if parent_visible else None
        merge: tuple[int, int] | None = None
        if parent is None:
            lanes[column] = None
        elif parent in lanes and lanes.index(parent) != column:
            other = lanes.index(parent)
            keep, drop = min(column, other), max(column, other)
            lanes[keep] = parent
            lanes[drop] = None
            merge = (keep, drop)
        else:
            lanes[column] = parent

        connector = None
        if merge is not None:
            keep, drop = merge
            connector = "".join(
                "/"
                if index == drop
                else ("|" if lanes[index] is not None or index == keep else " ")
                for index in range(len(lanes))
            )

        rows.append(
            GraphRow(
                version=version,
                node=node,
                continuation=continuation,
                connector=connector,
                column=column,
                parent_visible=parent_visible,
            )
        )
        while lanes and lanes[-1] is None:
            lanes.pop()

    return rows


def history_shape(versions: list[store.Version]) -> HistoryShape:
    """Return leaves and fork points within the supplied visible slice."""
    visible_ids = {version.id for version in versions}
    child_count: dict[str, int] = {}
    for version in versions:
        if version.parent_id in visible_ids:
            child_count[version.parent_id] = child_count.get(version.parent_id, 0) + 1
    return HistoryShape(
        tips=tuple(
            version.id
            for version in versions
            if child_count.get(version.id, 0) == 0
        ),
        branch_points=tuple(
            version.id
            for version in versions
            if child_count.get(version.id, 0) > 1
        ),
    )