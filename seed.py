"""Create a small example board for a new Agenda installation.

Run after first deploy:
    python3 seed.py

Idempotent: if the projects table is non-empty, this script does nothing.
The examples demonstrate Agenda's main project stages without assuming a
particular profession or workflow.
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
PROJECTS: list[dict] = [
    {
        "name": "Neighborhood oral history",
        "category": "near_publication",
        "deadline": None,
        "blocker": "Receive final interview release",
        "todos": [
            "Proofread the story collection",
            "Confirm photo captions",
            "Prepare the launch announcement",
        ],
        "for_later": [
            "Donate recordings to the local archive",
        ],
        "northstars": [
            "Preserve participants' voices accurately and respectfully.",
            "Publish a collection that is accessible to the whole neighborhood.",
        ],
    },
    {
        "name": "Community garden season",
        "category": "in_development",
        "deadline": None,
        "blocker": "Choose a date for the spring workday",
        "todos": [
            {
                "text": "Draft the planting plan",
                "note": "Start with the seed inventory, then leave open beds for member requests.",
            },
            "Ask members about tool needs",
            "Update the watering rota",
        ],
        "for_later": [
            "Plan a midsummer potluck",
        ],
        "northstars": [],
    },
    {
        "name": "Household energy dashboard",
        "category": "early_stage",
        "deadline": None,
        "blocker": "Pick a data source",
        "todos": [
            "Compare smart-meter export formats",
            "Sketch the first weekly report",
            "Test a local-only prototype",
        ],
        "for_later": [],
        "northstars": [],
    },
    {
        "name": "Learn conversational Italian",
        "category": "side_project",
        "deadline": None,
        "blocker": None,
        "todos": [
            "Choose a weekly practice group",
            "Review vocabulary for ten minutes",
        ],
        "for_later": [
            "Plan a language-immersion weekend",
        ],
        "northstars": [],
    },
    {
        "name": "Someday ideas",
        "category": "future_ideas",
        "deadline": None,
        "blocker": None,
        "todos": [
            "Build a tiny lending library",
            "Take a wilderness first-aid course",
        ],
        "for_later": [
            "Review this list next season",
        ],
        "northstars": [],
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

    n_proj = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    n_todo = conn.execute("SELECT COUNT(*) AS n FROM todos").fetchone()["n"]
    print(f"Seeded {n_proj} projects and {n_todo} todos into {db.DB_PATH}.")


if __name__ == "__main__":
    seed()
