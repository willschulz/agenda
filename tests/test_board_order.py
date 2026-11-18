import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import db
import main
from board_order import build_board_order, project_activity

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


class BoardOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA_SQL)
        db._migrate_add_sync_identity(self.conn)
        db._migrate_add_todo_effort(self.conn)
        db._migrate_add_todo_deadline(self.conn)
        db._migrate_add_settings_table(self.conn)
        db._migrate_add_board_sort(self.conn)
        self.conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")

    def tearDown(self):
        self.conn.close()

    def project(self, name, category, order):
        return self.conn.execute(
            "INSERT INTO projects "
            "(name, category, display_order, created_at, updated_at, stable_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, category, order, ago(400), ago(400), f"project-{name}"),
        ).lastrowid

    def todo(self, project_id, *, created=400, completed=None, notes=(), updated=None,
             deadline=None):
        self.conn.execute(
            "INSERT INTO todos (project_id, text, completed, completed_at, deadline, "
            "notes, created_at, updated_at, stable_uuid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id, "todo", int(completed is not None),
                ago(completed) if completed is not None else None,
                deadline,
                json.dumps([{"ts": ago(d), "author": "agent", "text": "n"} for d in notes]),
                ago(created), ago(updated if updated is not None else created),
                f"todo-{project_id}-{self.conn.total_changes}",
            ),
        )

    def order(self, category):
        return build_board_order(self.conn, now=NOW)[category]

    def set_mode(self, mode):
        self.conn.execute("UPDATE settings SET board_sort = ? WHERE id = 1", (mode,))

    def test_default_mode_is_activity(self):
        quiet = self.project("quiet", "in_development", 0)
        busy = self.project("busy", "in_development", 10)
        self.todo(busy, created=1)
        self.assertEqual(self.order("in_development"), ["project-busy", "project-quiet"])
        del quiet

    def test_creations_completions_and_notes_all_count(self):
        created = self.project("created", "side_project", 0)
        completed = self.project("completed", "side_project", 1)
        noted = self.project("noted", "side_project", 2)
        self.todo(created, created=2)
        self.todo(completed, completed=2)
        self.todo(noted, notes=(2,))
        scores = project_activity(self.conn, now=NOW)
        self.assertAlmostEqual(scores[created], 0.5 ** (2 / 7), places=3)
        self.assertEqual(scores[created], scores[completed])
        self.assertEqual(scores[created], scores[noted])

    def test_recent_work_outranks_more_older_work(self):
        old = self.project("old", "side_project", 0)
        new = self.project("new", "side_project", 1)
        for _ in range(3):
            self.todo(old, completed=25)
        self.todo(new, completed=0)
        self.assertEqual(self.order("side_project"), ["project-new", "project-old"])

    def test_edits_reorders_and_events_outside_window_do_not_count(self):
        touched = self.project("touched", "side_project", 1)
        ancient = self.project("ancient", "side_project", 2)
        self.todo(touched, updated=0)        # an edit or reorder only
        self.todo(ancient, completed=31)
        self.assertEqual(project_activity(self.conn, now=NOW), {})

    def test_ties_fall_back_to_manual_order(self):
        self.project("b", "side_project", 10)
        self.project("a", "side_project", 0)
        self.assertEqual(self.order("side_project"), ["project-a", "project-b"])

    def test_deadlines_no_longer_jump_the_queue(self):
        due = self.project("due", "near_publication", 0)
        busy = self.project("busy", "near_publication", 10)
        self.todo(due, deadline="2026-08-14")
        self.todo(busy, completed=1)
        self.assertEqual(self.order("near_publication"), ["project-busy", "project-due"])

    def test_top_of_mind_follows_the_same_rule(self):
        first = self.project("first", "top_of_mind", 0)
        second = self.project("second", "top_of_mind", 10)
        self.todo(second, completed=1)
        self.assertEqual(self.order("top_of_mind"), ["project-second", "project-first"])
        self.set_mode("manual")
        self.assertEqual(self.order("top_of_mind"), ["project-first", "project-second"])
        del first

    def test_manual_mode_ignores_activity_in_every_row(self):
        self.project("first", "side_project", 0)
        busy = self.project("busy", "side_project", 10)
        self.todo(busy, completed=0)
        self.set_mode("manual")
        self.assertEqual(self.order("side_project"), ["project-first", "project-busy"])

    def test_today_argument_still_supported(self):
        p = self.project("p", "side_project", 0)
        self.todo(p, completed=0)
        result = build_board_order(self.conn, today=date(2026, 8, 13), utc_offset_minutes=0)
        self.assertEqual(result["side_project"], ["project-p"])

    def test_sync_endpoint_uses_stable_ids_and_schema_two(self):
        self.project("one", "top_of_mind", 0)
        with patch.object(db, "get_conn", return_value=self.conn):
            result = asyncio.run(main.sync_board_order())
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["rows"]["top_of_mind"], ["project-one"])


class BoardSortApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path, self.old_conn = db.DB_PATH, db._conn
        db.DB_PATH = Path(self.tmp.name) / "agenda.db"
        db._conn = db._connect(db.DB_PATH)
        db.init_schema()
        self.conn = db.get_conn()
        stamp = datetime.now(timezone.utc).isoformat()
        self.ids = [
            self.conn.execute(
                "INSERT INTO projects (name, category, display_order, created_at, "
                "updated_at, stable_uuid) VALUES (?, 'side_project', ?, ?, ?, ?)",
                (name, order, stamp, stamp, f"uuid-{name}"),
            ).lastrowid
            for order, name in enumerate("abc")
        ]
        self.conn.execute(
            "INSERT INTO todos (project_id, text, created_at, updated_at) "
            "VALUES (?, 't', ?, ?)", (self.ids[2], stamp, stamp))

    def tearDown(self):
        db._conn.close()
        db._conn, db.DB_PATH = self.old_conn, self.old_path
        self.tmp.cleanup()

    def row_names(self):
        data = asyncio.run(main.list_projects(include_archived=False, todo_notes="summary"))
        return data["board_sort"], [p["name"] for p in data["projects"]["side_project"]]

    def test_settings_round_trip_and_projects_payload_order(self):
        self.assertEqual(db.get_settings()["board_sort"], "activity")
        self.assertEqual(self.row_names(), ("activity", ["c", "a", "b"]))
        asyncio.run(main.patch_settings(main.SettingsUpdate(board_sort="manual")))
        self.assertEqual(self.row_names(), ("manual", ["a", "b", "c"]))

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(Exception):
            main.SettingsUpdate(board_sort="velocity")

    def test_reorder_saves_manual_order_and_skips_unchanged(self):
        asyncio.run(main.patch_settings(main.SettingsUpdate(board_sort="manual")))
        a, b, c = self.ids
        result = asyncio.run(main.reorder_projects(
            main.ProjectReorder(category="side_project", project_ids=[c, a])))
        self.assertEqual(result["project_ids"], [c, a, b])
        self.assertEqual(self.row_names()[1], ["c", "a", "b"])
        again = asyncio.run(main.reorder_projects(
            main.ProjectReorder(category="side_project", project_ids=[c, a, b])))
        self.assertEqual(again["changed"], 0)

    def test_reorder_rejects_projects_from_other_rows(self):
        other = self.conn.execute(
            "INSERT INTO projects (name, category, created_at, updated_at) "
            "VALUES ('x', 'top_of_mind', 'x', 'x')").lastrowid
        with self.assertRaises(main.HTTPException):
            asyncio.run(main.reorder_projects(
                main.ProjectReorder(category="side_project", project_ids=[other])))

    def test_upgrading_v2_board_gets_activity_and_a_backup(self):
        self.conn.execute("ALTER TABLE settings DROP COLUMN board_sort")
        self.conn.execute("PRAGMA user_version = 2")
        db.init_schema()
        self.assertEqual(db.get_settings()["board_sort"], "activity")
        snaps = list((db.DB_PATH.parent / "backups").glob("pre-migration-v2-to-v3-*.db"))
        self.assertEqual(len(snaps), 1)

    def test_existing_database_migrates_to_activity(self):
        self.assertEqual(
            self.conn.execute("SELECT board_sort FROM settings").fetchone()[0], "activity")
        self.assertEqual(
            self.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
