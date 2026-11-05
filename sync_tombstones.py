"""Recoverable canonical tombstones for Agenda's schema-2 sync transition."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 2


class TombstoneConflict(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_payload(row: sqlite3.Row) -> str:
    return json.dumps(dict(row), sort_keys=True, separators=(",", ":"))


def _insert_tombstone(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    row: sqlite3.Row,
    project_stable_uuid: str | None,
    parent_stable_uuid: str | None,
    deleted_at: str,
) -> None:
    revision = int(row["sync_revision"]) + 1
    conn.execute(
        """
        INSERT INTO sync_tombstones (
            entity_kind, stable_uuid, project_stable_uuid,
            parent_stable_uuid, revision, deleted_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(entity_kind, stable_uuid, revision) DO NOTHING
        """,
        (
            entity_kind,
            row["stable_uuid"],
            project_stable_uuid,
            parent_stable_uuid,
            revision,
            deleted_at,
            _row_payload(row),
        ),
    )


def delete_todo_tree(
    conn: sqlite3.Connection,
    todo_id: int,
    *,
    deleted_at: str | None = None,
) -> int:
    """Capture explicit tombstones for a todo and every descendant, then remove."""
    target = conn.execute(
        "SELECT * FROM todos WHERE id = ?", (todo_id,)
    ).fetchone()
    if target is None:
        return 0
    project = conn.execute(
        "SELECT stable_uuid FROM projects WHERE id = ?", (target["project_id"],)
    ).fetchone()
    if project is None:
        raise TombstoneConflict("todo project is missing")

    project_rows = conn.execute(
        "SELECT * FROM todos WHERE project_id = ?", (target["project_id"],)
    ).fetchall()
    by_parent: dict[int | None, list[sqlite3.Row]] = {}
    by_id = {int(row["id"]): row for row in project_rows}
    for row in project_rows:
        by_parent.setdefault(row["parent_id"], []).append(row)

    selected: list[sqlite3.Row] = []
    stack = [target]
    while stack:
        row = stack.pop()
        selected.append(row)
        stack.extend(by_parent.get(row["id"], ()))

    timestamp = deleted_at or _now()
    for row in selected:
        parent = by_id.get(row["parent_id"]) if row["parent_id"] is not None else None
        _insert_tombstone(
            conn,
            entity_kind="todo",
            row=row,
            project_stable_uuid=project["stable_uuid"],
            parent_stable_uuid=parent["stable_uuid"] if parent else None,
            deleted_at=timestamp,
        )
    conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    return len(selected)


def delete_project(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    deleted_at: str | None = None,
) -> int:
    """Capture project and explicit child tombstones before the FK cascade."""
    project = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        return 0
    todos = conn.execute(
        "SELECT * FROM todos WHERE project_id = ?", (project_id,)
    ).fetchall()
    by_id = {int(row["id"]): row for row in todos}
    timestamp = deleted_at or _now()
    for row in todos:
        parent = by_id.get(row["parent_id"]) if row["parent_id"] is not None else None
        _insert_tombstone(
            conn,
            entity_kind="todo",
            row=row,
            project_stable_uuid=project["stable_uuid"],
            parent_stable_uuid=parent["stable_uuid"] if parent else None,
            deleted_at=timestamp,
        )
    _insert_tombstone(
        conn,
        entity_kind="project",
        row=project,
        project_stable_uuid=None,
        parent_stable_uuid=None,
        deleted_at=timestamp,
    )
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return len(todos) + 1


def list_active(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT entity_kind, stable_uuid, project_stable_uuid,
               parent_stable_uuid, revision, deleted_at, payload_json
        FROM sync_tombstones
        WHERE restored_at IS NULL
        ORDER BY deleted_at, entity_kind, stable_uuid, revision
        """
    ).fetchall()
    return [
        {
            "schema_version": SCHEMA_VERSION,
            "entity_kind": row["entity_kind"],
            "stable_id": row["stable_uuid"],
            "project_stable_id": row["project_stable_uuid"],
            "parent_stable_id": row["parent_stable_uuid"],
            "revision": row["revision"],
            "deleted_at": row["deleted_at"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def restore(
    conn: sqlite3.Connection,
    entity_kind: str,
    stable_uuid: str,
    *,
    restored_at: str | None = None,
) -> dict[str, Any]:
    if entity_kind not in {"project", "todo"}:
        raise TombstoneConflict("unsupported entity kind")
    tombstone = conn.execute(
        """
        SELECT * FROM sync_tombstones
        WHERE entity_kind = ? AND stable_uuid = ? AND restored_at IS NULL
        ORDER BY revision DESC LIMIT 1
        """,
        (entity_kind, stable_uuid),
    ).fetchone()
    if tombstone is None:
        raise TombstoneConflict("active tombstone not found")

    table = "projects" if entity_kind == "project" else "todos"
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE stable_uuid = ?", (stable_uuid,)
    ).fetchone()
    if existing is not None:
        return dict(existing)

    payload = json.loads(tombstone["payload_json"])
    payload.pop("id", None)
    payload["sync_revision"] = int(tombstone["revision"]) + 1
    payload["sync_deleted_at"] = None

    if entity_kind == "todo":
        project = conn.execute(
            "SELECT id FROM projects WHERE stable_uuid = ?",
            (tombstone["project_stable_uuid"],),
        ).fetchone()
        if project is None:
            raise TombstoneConflict("restore the containing project first")
        payload["project_id"] = project["id"]
        if tombstone["parent_stable_uuid"]:
            parent = conn.execute(
                "SELECT id FROM todos WHERE stable_uuid = ?",
                (tombstone["parent_stable_uuid"],),
            ).fetchone()
            if parent is None:
                raise TombstoneConflict("restore the parent todo first")
            payload["parent_id"] = parent["id"]
        else:
            payload["parent_id"] = None

    table_columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
    }
    restored = {key: value for key, value in payload.items() if key in table_columns}
    columns = list(restored)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        [restored[column] for column in columns],
    )
    timestamp = restored_at or _now()
    conn.execute(
        """
        UPDATE sync_tombstones SET restored_at = ?
        WHERE entity_kind = ? AND stable_uuid = ? AND restored_at IS NULL
        """,
        (timestamp, entity_kind, stable_uuid),
    )
    row = conn.execute(
        f"SELECT * FROM {table} WHERE stable_uuid = ?", (stable_uuid,)
    ).fetchone()
    return dict(row)
