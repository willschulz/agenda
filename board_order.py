"""Mac-parity project-row ordering for the iOS board snapshot."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

DEFAULT_UNPOINTED_EFFORT = 0.25
VELOCITY_WINDOW_DAYS = 14


def build_board_order(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    utc_offset_minutes: int | None = None,
) -> dict[str, list[str]]:
    """Return stable project UUIDs in the same order as the Mac board."""
    today = today or datetime.now().astimezone().date()
    if utc_offset_minutes is None:
        offset = datetime.now().astimezone().utcoffset()
        utc_offset_minutes = int(offset.total_seconds() / 60) if offset else 0
    offset_modifier = f"{utc_offset_minutes} minutes"
    dates = [today - timedelta(days=offset) for offset in range(VELOCITY_WINDOW_DAYS)]
    date_strings = {value.isoformat() for value in dates}
    overrides = {
        row["date"]
        for row in conn.execute("SELECT date FROM day_overrides").fetchall()
    }
    workdays = sum(
        1
        for value in dates
        if (value.weekday() >= 5) == (value.isoformat() in overrides)
    )

    # With effort points off (settings.points_enabled = 0), each task counts 1.
    try:
        setting = conn.execute("SELECT points_enabled FROM settings WHERE id = 1").fetchone()
        points_on = True if setting is None else bool(setting[0])
    except sqlite3.OperationalError:
        points_on = True
    velocities: dict[int, float] = {}
    for row in conn.execute(
        "SELECT project_id, date(completed_at, ?) AS day, "
        + ("COALESCE(effort, ?) AS effort " if points_on else "? AS effort ")
        + "FROM todos WHERE completed = 1 AND completed_at IS NOT NULL",
        (offset_modifier, DEFAULT_UNPOINTED_EFFORT if points_on else 1.0),
    ).fetchall():
        if row["day"] in date_strings:
            velocities[row["project_id"]] = (
                velocities.get(row["project_id"], 0.0) + float(row["effort"])
            )
    if workdays:
        velocities = {
            project_id: effort / workdays
            for project_id, effort in velocities.items()
        }
    else:
        velocities = {}

    deadlines = {
        row["project_id"]: row["deadline"]
        for row in conn.execute(
            "SELECT project_id, MIN(deadline) AS deadline FROM todos "
            "WHERE completed = 0 AND deadline IS NOT NULL "
            "GROUP BY project_id"
        ).fetchall()
    }
    projects = conn.execute(
        "SELECT id, stable_uuid, category, display_order FROM projects "
        "WHERE archived = 0 ORDER BY category, display_order, id"
    ).fetchall()

    rows: dict[str, list[str]] = {}
    for project in projects:
        rows.setdefault(project["category"], []).append(project)

    result: dict[str, list[str]] = {}
    for category, category_projects in rows.items():
        if category == "top_of_mind":
            ordered = category_projects
        else:
            with_deadline = [p for p in category_projects if deadlines.get(p["id"])]
            without_deadline = [
                p for p in category_projects if not deadlines.get(p["id"])
            ]
            with_deadline.sort(
                key=lambda project: (
                    deadlines[project["id"]],
                    -velocities.get(project["id"], 0.0),
                    project["display_order"],
                    project["id"],
                )
            )
            without_deadline.sort(
                key=lambda project: (
                    -velocities.get(project["id"], 0.0),
                    project["display_order"],
                    project["id"],
                )
            )
            ordered = with_deadline + without_deadline
        result[category] = [project["stable_uuid"] for project in ordered]
    return result
