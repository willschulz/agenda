"""Create the starter board for a new Agenda installation.

Run on first launch (the native app does this automatically):
    python3 seed.py

Idempotent: if the projects table is non-empty, this script does nothing.
A new board starts with the default rows and three always-useful projects in
Top of Mind; everything else is the user's to add. Seeding also turns on the
first-run welcome tour, which explains the rows and lets the user edit them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import db


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# Each project has a category plus optional deadline, blocker, todos,
# for_later items, and northstars. A todo may be a title string or a dict containing
# text and an optional note.
# - blocker becomes a single is_blocker=1 todo at order 0
# - todos become non-blocker active todos in listed order
# - for_later items become for_later=1 todos in listed order
# Default rows a brand-new board leaves out. Existing boards keep every row
# they already have; this only shapes a first-time user's starting set.
NEW_BOARD_OMITTED_ROWS: tuple[str, ...] = ("side_project",)

PROJECTS: list[dict] = [
    {
        "name": "Correspondence",
        "category": "top_of_mind",
        "deadline": None,
        "blocker": None,
        "todos": [],
        "for_later": [],
        "northstars": ["Emails and messages that need a reply."],
    },
    {
        "name": "Life Stuff",
        "category": "top_of_mind",
        "deadline": None,
        "blocker": None,
        "todos": [],
        "for_later": [],
        "northstars": ["Errands, appointments, and everything outside work."],
    },
    {
        "name": "Work Miscellany",
        "category": "top_of_mind",
        "deadline": None,
        "blocker": None,
        "todos": [],
        "for_later": [],
        "northstars": ["Small work tasks that don't belong to a bigger project."],
    },
]


def seed() -> None:
    db.init_schema()
    conn = db.get_conn()
    existing = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    if existing > 0:
        print(f"DB already has {existing} project(s); seed is idempotent and will not insert.")
        return

    now = _now()
    project_order_by_cat: dict[str, int] = {}

    with db.transaction() as cx:
        for key in NEW_BOARD_OMITTED_ROWS:
            cx.execute("DELETE FROM board_rows WHERE key = ?", (key,))
        for position, row in enumerate(cx.execute(
            "SELECT key FROM board_rows ORDER BY position, key"
        ).fetchall()):
            cx.execute("UPDATE board_rows SET position = ? WHERE key = ?", (position, row["key"]))

        for proj in PROJECTS:
            cat = proj["category"]
            display_order = project_order_by_cat.get(cat, 0)
            project_order_by_cat[cat] = display_order + 1

            cur = cx.execute(
                "INSERT INTO projects "
                "(name, category, display_order, deadline, notes, paths_json, northstars, archived, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '[]', '[]', ?, 0, ?, ?)",
                (
                    proj["name"],
                    cat,
                    display_order,
                    proj["deadline"],
                    json.dumps(proj.get("northstars", [])),
                    now,
                    now,
                ),
            )
            pid = cur.lastrowid

            todo_order = 0

            if proj["blocker"]:
                cx.execute(
                    "INSERT INTO todos "
                    "(project_id, parent_id, text, is_blocker, for_later, "
                    " display_order, completed, completed_at, created_at, updated_at) "
                    "VALUES (?, NULL, ?, 1, 0, ?, 0, NULL, ?, ?)",
                    (pid, proj["blocker"], todo_order, now, now),
                )
                todo_order += 1

            for todo in proj["todos"]:
                text = todo if isinstance(todo, str) else todo["text"]
                note = None if isinstance(todo, str) else todo.get("note")
                notes = (
                    [{"ts": now, "author": "agent", "text": note}]
                    if note
                    else []
                )
                cx.execute(
                    "INSERT INTO todos "
                    "(project_id, parent_id, text, is_blocker, for_later, "
                    " display_order, completed, completed_at, notes, created_at, updated_at) "
                    "VALUES (?, NULL, ?, 0, 0, ?, 0, NULL, ?, ?, ?)",
                    (pid, text, todo_order, json.dumps(notes), now, now),
                )
                todo_order += 1

            for_later_order = 0
            for text in proj["for_later"]:
                cx.execute(
                    "INSERT INTO todos "
                    "(project_id, parent_id, text, is_blocker, for_later, "
                    " display_order, completed, completed_at, created_at, updated_at) "
                    "VALUES (?, NULL, ?, 0, 1, ?, 0, NULL, ?, ?)",
                    (pid, text, for_later_order, now, now),
                )
                for_later_order += 1

    db.set_onboarding_pending(True)

    n_proj = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    n_todo = conn.execute("SELECT COUNT(*) AS n FROM todos").fetchone()["n"]
    print(f"Seeded {n_proj} projects and {n_todo} todos into {db.DB_PATH}.")


if __name__ == "__main__":
    seed()
