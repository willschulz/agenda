"""Editable board rows: storage, migration, the row-editor plan, and seeding."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from pydantic import ValidationError

import db
import insights
import main
import seed


def _project(conn, name, category, archived=0, order=0):
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO projects (name, category, display_order, archived, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, category, order, archived, now, now),
    )
    return cur.lastrowid


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agenda.db"
        self.old_path = db.DB_PATH
        self.old_conn = db._conn
        db.DB_PATH = self.path
        db._conn = db._connect(self.path)
        db.init_schema()
        self.conn = db.get_conn()

    def tearDown(self):
        db._conn.close()
        db._conn = self.old_conn
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def labels(self):
        return [(r["key"], r["label"]) for r in db.list_rows()]


class FreshSchemaTests(TempDB):
    def test_fresh_database_has_default_rows_and_no_category_check(self):
        self.assertEqual(self.labels(), list(db.DEFAULT_ROWS))
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='projects'").fetchone()[0]
        self.assertNotIn("CHECK (category IN", sql)
        self.assertTrue(db.list_rows()[0]["locked"])
        self.assertFalse(db.get_settings()["onboarding_pending"])

    def test_new_database_gets_no_pre_migration_snapshot(self):
        self.assertEqual(list((self.path.parent / "backups").glob("pre-migration-*.db")), [])
        self.assertEqual(
            self.conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)

    def test_init_schema_is_idempotent(self):
        db.init_schema()
        db.init_schema()
        self.assertEqual(self.labels(), list(db.DEFAULT_ROWS))


class LegacyMigrationTests(unittest.TestCase):
    """A database created before editable rows: fixed CHECK, extra columns."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.executescript("""
            CREATE TABLE "projects" (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              name            TEXT    NOT NULL,
              category        TEXT    NOT NULL CHECK (category IN
                                ('top_of_mind','near_publication','in_development',
                                 'early_stage','side_project','future_ideas')),
              display_order   INTEGER NOT NULL DEFAULT 0,
              deadline        TEXT,
              notes           TEXT    NOT NULL DEFAULT '',
              paths_json      TEXT    NOT NULL DEFAULT '[]',
              archived        INTEGER NOT NULL DEFAULT 0,
              created_at      TEXT    NOT NULL,
              updated_at      TEXT    NOT NULL
            , git_repo TEXT, northstars TEXT NOT NULL DEFAULT '[]');
            CREATE TABLE todos (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              parent_id       INTEGER REFERENCES todos(id) ON DELETE CASCADE,
              text            TEXT    NOT NULL,
              is_blocker      INTEGER NOT NULL DEFAULT 0,
              is_followup     INTEGER NOT NULL DEFAULT 0,
              display_order   INTEGER NOT NULL DEFAULT 0,
              completed       INTEGER NOT NULL DEFAULT 0,
              completed_at    TEXT,
              created_at      TEXT    NOT NULL,
              updated_at      TEXT    NOT NULL);
            INSERT INTO projects (id, name, category, created_at, updated_at, northstars)
              VALUES (7, 'Paper', 'early_stage', 'x', 'x', '["Ship it"]');
            INSERT INTO projects (id, name, category, created_at, updated_at)
              VALUES (9, 'Gone soon', 'side_project', 'x', 'x');
            DELETE FROM projects WHERE id = 9;
            INSERT INTO todos (project_id, text, created_at, updated_at)
              VALUES (7, 'Draft', 'x', 'x');
        """)
        conn.close()
        self.old_path = db.DB_PATH
        self.old_conn = db._conn
        db.DB_PATH = self.path
        db._conn = db._connect(self.path)

    def tearDown(self):
        db._conn.close()
        db._conn = self.old_conn
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_rebuild_keeps_data_drops_check_and_preserves_sequence(self):
        db.init_schema()
        conn = db.get_conn()
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='projects'").fetchone()[0]
        self.assertNotIn("CHECK (category IN", sql)
        row = conn.execute("SELECT * FROM projects WHERE id = 7").fetchone()
        self.assertEqual((row["name"], row["category"], row["northstars"]),
                         ("Paper", "early_stage", '["Ship it"]'))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        # The deleted project's id 9 must never be reused.
        new_id = _project(conn, "Next", "early_stage")
        self.assertGreater(new_id, 9)
        # Sync triggers added by later migrations are present.
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='projects'")}
        self.assertIn("projects_sync_revision_after_update", triggers)
        db.init_schema()  # idempotent


class PreMigrationBackupTests(LegacyMigrationTests):
    """Reuses the legacy (pre-editable-rows) database from LegacyMigrationTests."""

    def backups(self):
        return sorted((self.path.parent / "backups").glob("pre-migration-*.db"))

    def test_existing_database_is_snapshotted_once_before_migrating(self):
        db.init_schema()
        snaps = self.backups()
        self.assertEqual(len(snaps), 1)
        self.assertIn("pre-migration-v0-to-v1-", snaps[0].name)
        snap = sqlite3.connect(snaps[0])
        try:
            self.assertEqual(snap.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            old_sql = snap.execute(
                "SELECT sql FROM sqlite_master WHERE name='projects'").fetchone()[0]
            self.assertIn("CHECK (category IN", old_sql)   # truly the pre-upgrade state
        finally:
            snap.close()
        self.assertEqual(
            db.get_conn().execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        db.init_schema()
        self.assertEqual(len(self.backups()), 1)       # up to date: no new snapshot

    def test_failed_snapshot_changes_nothing(self):
        def boom(**kwargs):
            raise OSError("disk full")
        original = db.create_backup
        db.create_backup = boom
        try:
            with self.assertRaises(db.PreMigrationBackupError):
                db.init_schema()
        finally:
            db.create_backup = original
        sql = db.get_conn().execute(
            "SELECT sql FROM sqlite_master WHERE name='projects'").fetchone()[0]
        self.assertIn("CHECK (category IN", sql)


class RowPlanTests(TempDB):
    def plan(self, rows, deletions=()):
        return db.apply_row_plan(rows, list(deletions))

    def current(self):
        return [{"key": k, "label": l} for k, l in self.labels()]

    def test_rename_reorder_add_and_delete_with_move(self):
        early = _project(self.conn, "Early A", "early_stage", order=0)
        early_archived = _project(self.conn, "Early B", "early_stage", archived=1, order=1)
        _project(self.conn, "Dev A", "in_development", order=0)

        rows = [
            {"key": "top_of_mind", "label": "Now"},
            {"id": "new:1", "label": "Ongoing"},
            {"key": "near_publication", "label": "Near Publication"},
            {"key": "in_development", "label": "In Development"},
            {"key": "side_project", "label": "Side Projects"},
            {"key": "future_ideas", "label": "Future Ideas"},
        ]
        result = self.plan(rows, [{"key": "early_stage", "move_to": "in_development"}])

        self.assertEqual(
            [r["label"] for r in result],
            ["Now", "Ongoing", "Near Publication", "In Development",
             "Side Projects", "Future Ideas"],
        )
        self.assertEqual(result[1]["key"], "ongoing")
        moved = {
            r["id"]: (r["category"], r["display_order"])
            for r in self.conn.execute(
                "SELECT id, category, display_order FROM projects WHERE id IN (?, ?)",
                (early, early_archived),
            )
        }
        # Moved projects go after the target row's existing ones, archived too.
        self.assertEqual(moved[early], ("in_development", 1))
        self.assertEqual(moved[early_archived], ("in_development", 2))
        self.assertNotIn("early_stage", db.row_keys())

    def test_delete_can_move_into_a_row_created_in_the_same_edit(self):
        pid = _project(self.conn, "Early A", "early_stage")
        rows = [r for r in self.current() if r["key"] != "early_stage"]
        rows.append({"id": "new:7", "label": "Ongoing"})
        self.plan(rows, [{"key": "early_stage", "move_to": "new:7"}])
        category = self.conn.execute(
            "SELECT category FROM projects WHERE id = ?", (pid,)).fetchone()[0]
        self.assertEqual(category, "ongoing")

    def test_invalid_plans_change_nothing(self):
        before = self.labels()
        _project(self.conn, "Early A", "early_stage")
        cases = [
            ([], []),                                                   # no rows
            ([{"key": "near_publication", "label": "X"}] + self.current()[2:], []),  # top not first
            ([{"key": "top_of_mind", "label": "  "}] + self.current()[1:], []),     # empty name
            ([{"key": "top_of_mind", "label": "Same"},
              {"key": "near_publication", "label": "same"}] + self.current()[2:], []),  # dup names
            ([r for r in self.current() if r["key"] != "early_stage"], []),    # missing move target
            ([r for r in self.current() if r["key"] != "early_stage"],
             [{"key": "early_stage", "move_to": "early_stage"}]),             # move into itself
            (self.current()[1:], [{"key": "top_of_mind", "move_to": "near_publication"}]),
        ]
        for rows, deletions in cases:
            with self.subTest(rows=rows, deletions=deletions):
                with self.assertRaises(db.RowError):
                    self.plan(rows, deletions)
                self.assertEqual(self.labels(), before)

    def test_new_row_keys_are_unique_and_never_reuse_a_live_key(self):
        rows = self.current() + [
            {"id": "new:1", "label": "In Development!"},   # slug collides with a live key
            {"id": "new:2", "label": "Ideas & Notes"},
        ]
        rows[3]["label"] = "Building"   # free up the name, not the key
        result = self.plan(rows)
        keys = [r["key"] for r in result]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("in_development_2", keys)
        self.assertIn("ideas_notes", keys)


class ApiTests(TempDB):
    def test_project_validation_follows_live_rows(self):
        rows = [r for r in [{"key": k, "label": l} for k, l in self.labels()]
                if r["key"] != "future_ideas"]
        rows.append({"id": "new:1", "label": "Ongoing"})
        db.apply_row_plan(rows, [{"key": "future_ideas", "move_to": "side_project"}])
        main.ProjectCreate(name="Fine", category="ongoing")
        with self.assertRaises(ValidationError):
            main.ProjectCreate(name="Stale row", category="future_ideas")

    def test_projects_endpoint_and_board_order_carry_row_definitions(self):
        data = asyncio.run(main.list_projects(include_archived=False))
        self.assertEqual(data["categories"], [k for k, _ in db.DEFAULT_ROWS])
        self.assertEqual(data["rows"][0], {"key": "top_of_mind", "label": "Top of Mind", "locked": True})
        order = asyncio.run(main.sync_board_order())
        self.assertEqual(
            order["row_definitions"],
            [{"key": k, "label": l} for k, l in db.DEFAULT_ROWS],
        )

    def test_put_rows_reports_plan_errors_as_400(self):
        payload = main.BoardRowsPlan(rows=[{"key": "near_publication", "label": "X"}])
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(main.apply_board_rows(payload))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_onboarding_flag_round_trip(self):
        asyncio.run(main.restart_onboarding())
        self.assertTrue(db.get_settings()["onboarding_pending"])
        asyncio.run(main.complete_onboarding())
        self.assertFalse(db.get_settings()["onboarding_pending"])


class SeedTests(TempDB):
    def test_seed_creates_top_of_mind_trio_and_turns_on_onboarding(self):
        seed.seed()
        names = [r[0] for r in self.conn.execute(
            "SELECT name FROM projects WHERE category = 'top_of_mind' ORDER BY display_order")]
        self.assertEqual(names, ["Correspondence", "Life Stuff", "Work Miscellany"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM projects WHERE category != 'top_of_mind'").fetchone()[0],
            0,
        )
        self.assertTrue(db.get_settings()["onboarding_pending"])
        # New boards start without Side Projects; positions stay contiguous.
        rows = db.list_rows()
        self.assertEqual(
            [r["key"] for r in rows],
            ["top_of_mind", "near_publication", "in_development", "early_stage", "future_ideas"],
        )
        self.assertEqual([r["position"] for r in rows], list(range(5)))

    def test_seed_is_a_no_op_on_an_existing_board(self):
        _project(self.conn, "Mine", "in_development")
        seed.seed()
        self.assertFalse(db.get_settings()["onboarding_pending"])
        self.assertIn("side_project", db.row_keys())   # existing boards keep it
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)


class InsightRankTests(TempDB):
    def test_rank_and_label_follow_the_users_rows(self):
        rows = [{"key": k, "label": l} for k, l in self.labels()]
        rows.insert(1, rows.pop(5))                      # Future Ideas moves to second
        rows[1]["label"] = "Someday"
        db.apply_row_plan(rows, [])
        _project(self.conn, "Dreams", "future_ideas")
        projects, _ = insights._load_rows(self.conn)
        dreams = next(p for p in projects if p["name"] == "Dreams")
        self.assertEqual(insights._row_rank(dreams), 1)
        self.assertEqual(insights._row_label(dreams), "Someday")


if __name__ == "__main__":
    unittest.main()
