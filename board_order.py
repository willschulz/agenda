"""Project-row ordering, shared by the Mac board and the iOS board snapshot.

One setting (settings.board_sort) decides how every row is ordered:

- "activity" (default): most recent activity first. Each todo created,
  completed, or given a note counts as one event, weighted by age with a
  one-week half-life, over the last ACTIVITY_WINDOW_DAYS. Ties (usually
  projects with no recent activity) fall back to the manual order.
- "manual": the order the user dragged the cards into (display_order).

The same rule applies to every row, Top of Mind included.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

BOARD_SORT_MODES = ("activity", "manual")
DEFAULT_BOARD_SORT = "activity"
ACTIVITY_WINDOW_DAYS = 30
ACTIVITY_HALF_LIFE_DAYS = 7.0


def board_sort_mode(conn: sqlite3.Connection) -> str:
    """The stored row-ordering mode, defaulting to activity."""
    try:
        row = conn.execute("SELECT board_sort FROM settings WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return DEFAULT_BOARD_SORT
    value = row[0] if row is not None else None
    return value if value in BOARD_SORT_MODES else DEFAULT_BOARD_SORT


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _note_timestamps(raw) -> list[datetime]:
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    stamps = []
    for item in items:
        if isinstance(item, dict):
            parsed = _parse_ts(item.get("ts"))
            if parsed is not None:
                stamps.append(parsed)
    return stamps


def project_activity(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[int, float]:
    """Recency-weighted activity score per project id (missing = 0).

    Counts todo creations, completions, and note entries. Plain edits and
    reorders are left out on purpose: reordering rewrites updated_at on every
    sibling, which would make a tidy-up look like a burst of work.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ACTIVITY_WINDOW_DAYS)
    scores: dict[int, float] = {}

    def add(project_id: int, when: datetime | None) -> None:
        if when is None or when < cutoff:
            return
        age_days = max(0.0, (now - when).total_seconds() / 86400)
        weight = 0.5 ** (age_days / ACTIVITY_HALF_LIFE_DAYS)
        scores[project_id] = scores.get(project_id, 0.0) + weight

    try:
        rows = conn.execute(
            "SELECT project_id, created_at, completed, completed_at, notes "
            "FROM todos WHERE sync_deleted_at IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            "SELECT project_id, created_at, completed, completed_at, notes FROM todos"
        ).fetchall()
    for row in rows:
        project_id = row["project_id"]
        add(project_id, _parse_ts(row["created_at"]))
        if row["completed"]:
            add(project_id, _parse_ts(row["completed_at"]))
        for stamp in _note_timestamps(row["notes"]):
            add(project_id, stamp)
    return {pid: round(score, 4) for pid, score in scores.items()}


def order_projects(projects, mode: str, activity: dict[int, float]) -> list:
    """Sort project rows/dicts (with id and display_order) for one board row."""
    if mode == "manual":
        return sorted(projects, key=lambda p: (p["display_order"], p["id"]))
    return sorted(
        projects,
        key=lambda p: (-activity.get(p["id"], 0.0), p["display_order"], p["id"]),
    )


def build_board_order(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    utc_offset_minutes: int | None = None,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """Return stable project UUIDs per row, in the same order as the Mac board.

    `today` (end of that local day) is accepted for deterministic tests.
    """
    if now is None and today is not None:
        offset = timedelta(minutes=utc_offset_minutes or 0)
        now = datetime.combine(today, time(23, 59, 59), tzinfo=timezone(offset))
    mode = board_sort_mode(conn)
    activity = project_activity(conn, now=now) if mode == "activity" else {}
    projects = conn.execute(
        "SELECT id, stable_uuid, category, display_order FROM projects "
        "WHERE archived = 0 ORDER BY category, display_order, id"
    ).fetchall()

    rows: dict[str, list] = {}
    for project in projects:
        rows.setdefault(project["category"], []).append(project)
    return {
        category: [p["stable_uuid"] for p in order_projects(items, mode, activity)]
        for category, items in rows.items()
    }
