"""Atomic todo hierarchy validation and project structural revisions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable


class StructureValidationError(ValueError):
    """The proposed todo graph is incomplete or invalid."""


@dataclass(frozen=True)
class StructureUpdate:
    todo_id: int
    parent_id: int | None
    display_order: int
    for_later: bool


def validate_no_cycles(
    conn: sqlite3.Connection,
    proposed_parents: dict[int, int | None],
) -> None:
    """Reject self-parenting and cycles in the projected global todo graph."""
    rows = conn.execute("SELECT id, parent_id FROM todos").fetchall()
    parents = {int(row["id"]): row["parent_id"] for row in rows}
    parents.update(proposed_parents)
    for start in parents:
        seen: set[int] = set()
        current: int | None = start
        while current is not None:
            if current in seen:
                raise StructureValidationError("todo hierarchy contains a cycle")
            seen.add(current)
            current = parents.get(current)


def bump_project_structure_revisions(
    conn: sqlite3.Connection,
    project_ids: Iterable[int],
    *,
    updated_at: str,
) -> None:
    """Advance each affected project's scalar and structural revisions once."""
    unique_ids = sorted(set(project_ids))
    if not unique_ids:
        return
    conn.executemany(
        """
        UPDATE projects
        SET sync_revision = sync_revision + 1,
            sync_structure_revision = sync_structure_revision + 1,
            updated_at = ?
        WHERE id = ?
        """,
        [(updated_at, project_id) for project_id in unique_ids],
    )


def apply_project_structure_edit(
    conn: sqlite3.Connection,
    *,
    project_stable_uuid: str,
    proposed_payload: dict[str, Any],
    next_revision: int,
    next_structure_revision: int,
    updated_at: str,
) -> None:
    """Validate and atomically apply one complete within-project structure edit."""
    if set(proposed_payload) != {"updates"}:
        raise StructureValidationError("payload must contain only updates")
    raw_updates = proposed_payload["updates"]
    if not isinstance(raw_updates, list) or not raw_updates:
        raise StructureValidationError("updates must be a non-empty list")

    project = conn.execute(
        """
        SELECT id FROM projects
        WHERE stable_uuid = ? AND sync_deleted_at IS NULL
        """,
        (project_stable_uuid,),
    ).fetchone()
    if project is None:
        raise StructureValidationError("target project is missing")
    project_id = int(project["id"])

    rows = conn.execute(
        """
        SELECT child.id, child.stable_uuid, child.parent_id,
               parent.stable_uuid AS parent_stable_uuid,
               child.display_order, child.for_later, child.completed
        FROM todos child
        LEFT JOIN todos parent ON parent.id = child.parent_id
        WHERE child.project_id = ? AND child.sync_deleted_at IS NULL
        """,
        (project_id,),
    ).fetchall()
    by_stable = {str(row["stable_uuid"]): row for row in rows}
    editable_stable_ids = {
        stable_id
        for stable_id, row in by_stable.items()
        if not bool(row["completed"])
    }
    updates: list[StructureUpdate] = []
    update_stable_ids: set[str] = set()
    projected_parent_stable: dict[str, str | None] = {
        stable_id: row["parent_stable_uuid"]
        for stable_id, row in by_stable.items()
    }
    projected_order: dict[str, int] = {
        stable_id: int(row["display_order"])
        for stable_id, row in by_stable.items()
    }
    projected_for_later: dict[str, bool] = {
        stable_id: bool(row["for_later"])
        for stable_id, row in by_stable.items()
    }

    required_fields = {
        "stable_id",
        "parent_stable_id",
        "display_order",
        "for_later",
    }
    for raw in raw_updates:
        if not isinstance(raw, dict) or set(raw) != required_fields:
            raise StructureValidationError("each update must use the locked fields")
        stable_id = raw["stable_id"]
        parent_stable_id = raw["parent_stable_id"]
        display_order = raw["display_order"]
        for_later = raw["for_later"]
        if type(stable_id) is not str or stable_id not in editable_stable_ids:
            raise StructureValidationError("todo is missing or outside target project")
        if stable_id in update_stable_ids:
            raise StructureValidationError("duplicate todo update")
        if parent_stable_id is not None and (
            type(parent_stable_id) is not str
            or parent_stable_id not in by_stable
            or (
                parent_stable_id not in editable_stable_ids
                and parent_stable_id != by_stable[stable_id]["parent_stable_uuid"]
            )
        ):
            raise StructureValidationError("parent is missing or outside target project")
        if parent_stable_id == stable_id:
            raise StructureValidationError("todo cannot parent itself")
        if (
            type(display_order) is not int
            or display_order < 0
            or display_order % 10 != 0
        ):
            raise StructureValidationError("display order must use non-negative 10s")
        if type(for_later) is not bool:
            raise StructureValidationError("for_later must be boolean")

        update_stable_ids.add(stable_id)
        projected_parent_stable[stable_id] = parent_stable_id
        projected_order[stable_id] = display_order
        projected_for_later[stable_id] = for_later
        updates.append(
            StructureUpdate(
                todo_id=int(by_stable[stable_id]["id"]),
                parent_id=(
                    int(by_stable[parent_stable_id]["id"])
                    if parent_stable_id is not None
                    else None
                ),
                display_order=display_order,
                for_later=for_later,
            )
        )

    projected_parents = {
        int(by_stable[stable_id]["id"]): (
            int(by_stable[parent_stable_id]["id"])
            if parent_stable_id is not None
            else None
        )
        for stable_id, parent_stable_id in projected_parent_stable.items()
    }
    validate_no_cycles(conn, projected_parents)

    def sibling_group(
        parent_stable_id: str | None,
        for_later: bool,
    ) -> tuple[str, str | bool]:
        if parent_stable_id is not None:
            return ("children", parent_stable_id)
        return ("roots", for_later)

    touched_groups: set[tuple[str, str | bool]] = set()
    for stable_id in update_stable_ids:
        touched_groups.add(
            sibling_group(
                by_stable[stable_id]["parent_stable_uuid"],
                bool(by_stable[stable_id]["for_later"]),
            )
        )
        touched_groups.add(
            sibling_group(
                projected_parent_stable[stable_id],
                projected_for_later[stable_id],
            )
        )
    required_stable_ids = {
        stable_id
        for stable_id, old_row in by_stable.items()
        if stable_id in editable_stable_ids
        and (
            sibling_group(
                old_row["parent_stable_uuid"],
                bool(old_row["for_later"]),
            )
            in touched_groups
            or sibling_group(
                projected_parent_stable[stable_id],
                projected_for_later[stable_id],
            )
            in touched_groups
        )
    }
    if not required_stable_ids.issubset(update_stable_ids):
        raise StructureValidationError("affected sibling groups are incomplete")

    sibling_groups: dict[tuple[str, str | bool], list[str]] = {}
    for stable_id, parent_stable_id in projected_parent_stable.items():
        if stable_id not in editable_stable_ids:
            continue
        group = sibling_group(
            parent_stable_id,
            projected_for_later[stable_id],
        )
        if group in touched_groups:
            sibling_groups.setdefault(group, []).append(stable_id)
    for sibling_ids in sibling_groups.values():
        orders = sorted(projected_order[stable_id] for stable_id in sibling_ids)
        if orders != [index * 10 for index in range(len(sibling_ids))]:
            raise StructureValidationError("sibling order must be normalized")

    def root_for(stable_id: str) -> str:
        current = stable_id
        while projected_parent_stable[current] is not None:
            current = str(projected_parent_stable[current])
        return current

    expected_for_later = {
        stable_id: projected_for_later[root_for(stable_id)]
        for stable_id in projected_parent_stable
    }
    for stable_id in update_stable_ids:
        if projected_for_later[stable_id] != expected_for_later[stable_id]:
            raise StructureValidationError(
                "descendants must inherit the projected root for_later value"
            )

    conn.executemany(
        """
        UPDATE todos
        SET parent_id = ?, display_order = ?, for_later = ?, updated_at = ?
        WHERE id = ?
        """,
        [
            (
                update.parent_id,
                update.display_order,
                int(update.for_later),
                updated_at,
                update.todo_id,
            )
            for update in updates
        ],
    )
    conn.executemany(
        """
        UPDATE todos SET for_later = ?, updated_at = ? WHERE id = ?
        """,
        [
            (
                int(expected_for_later[stable_id]),
                updated_at,
                int(row["id"]),
            )
            for stable_id, row in by_stable.items()
            if stable_id not in update_stable_ids
            and bool(row["for_later"]) != expected_for_later[stable_id]
        ],
    )
    conn.execute(
        """
        UPDATE projects
        SET sync_revision = ?, sync_structure_revision = ?, updated_at = ?
        WHERE id = ?
        """,
        (next_revision, next_structure_revision, updated_at, project_id),
    )
