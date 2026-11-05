"""Effort points can be switched off without losing them."""

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import board_order
import db
import main
import seed


class PointsSettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agenda.db"
        self.old_path, self.old_conn = db.DB_PATH, db._conn
        db.DB_PATH = self.path
        db._conn = db._connect(self.path)
        db.init_schema()
        self.conn = db.get_conn()
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        self.pid = self.conn.execute(
            "INSERT INTO projects (name, category, created_at, updated_at, stable_uuid) "
            "VALUES ('P', 'in_development', ?, ?, 'uuid-p')", (stamp, stamp)).lastrowid
        # Completed today: one 5-pointer, one 2-pointer, one unpointed.
        for effort in (5, 2, None):
            self.conn.execute(
                "INSERT INTO todos (project_id, text, completed, completed_at, effort, "
                "created_at, updated_at) VALUES (?, 't', 1, ?, ?, ?, ?)",
                (self.pid, stamp, effort, stamp, stamp))

    def tearDown(self):
        db._conn.close()
        db._conn, db.DB_PATH = self.old_conn, self.old_path
        self.tmp.cleanup()

    def stats_total(self):
        data = asyncio.run(main.stats(since=None, tz_offset_minutes=0))
        day = next(iter(data["by_day"].values()))
        return sum(day.values()), data

    def test_existing_board_keeps_points_on(self):
        self.assertTrue(db.get_settings()["points_enabled"])
        total, data = self.stats_total()
        self.assertEqual(total, 7.25)           # 5 + 2 + 0.25 for unpointed
        self.assertTrue(data["points_enabled"])

    def test_off_counts_every_task_as_one_and_on_restores_points(self):
        db.patch_settings({"points_enabled": False})
        total, data = self.stats_total()
        self.assertEqual(total, 3.0)
        self.assertFalse(data["points_enabled"])
        efforts = sorted(i["effort"] for items in data["accomplishments_by_day"].values()
                         for i in items)
        self.assertEqual(efforts, [1.0, 1.0, 1.0])
        # Stored points are untouched while off...
        stored = sorted(r[0] for r in self.conn.execute(
            "SELECT effort FROM todos WHERE effort IS NOT NULL"))
        self.assertEqual(stored, [2, 5])
        # ...and come back when points are switched on again.
        db.patch_settings({"points_enabled": True})
        self.assertEqual(self.stats_total()[0], 7.25)

    def test_board_order_velocity_and_payload_follow_the_setting(self):
        other = self.conn.execute(
            "INSERT INTO projects (name, category, created_at, updated_at, stable_uuid) "
            "VALUES ('Q', 'in_development', 'x', 'x', 'uuid-q')").lastrowid
        stamp = datetime.now(timezone.utc).isoformat()
        for _ in range(4):   # four unpointed tasks: 1.0 with points on, 4.0 with them off
            self.conn.execute(
                "INSERT INTO todos (project_id, text, completed, completed_at, "
                "created_at, updated_at) VALUES (?, 't', 1, ?, ?, ?)",
                (other, stamp, stamp, stamp))
        on = board_order.build_board_order(self.conn)["in_development"]
        self.assertEqual(on[0], "uuid-p")     # 7.25 beats 1.0
        db.patch_settings({"points_enabled": False})
        off = board_order.build_board_order(self.conn)["in_development"]
        self.assertEqual(off[0], "uuid-q")    # 4 tasks beat 3
        payload = asyncio.run(main.sync_board_order())
        self.assertIs(payload["points_enabled"], False)

    def test_settings_api_round_trip(self):
        result = asyncio.run(main.patch_settings(main.SettingsUpdate(points_enabled=False)))
        self.assertFalse(result["points_enabled"])
        self.assertFalse(db.points_enabled())


class NewBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agenda.db"
        self.old_path, self.old_conn = db.DB_PATH, db._conn
        db.DB_PATH = self.path
        db._conn = db._connect(self.path)

    def tearDown(self):
        db._conn.close()
        db._conn, db.DB_PATH = self.old_conn, self.old_path
        self.tmp.cleanup()

    def test_new_boards_start_with_points_off(self):
        seed.seed()
        self.assertFalse(db.get_settings()["points_enabled"])

    def test_upgrading_board_keeps_points_on_and_is_backed_up_first(self):
        # A board from schema version 1 (before the setting existed).
        db.init_schema()
        conn = db.get_conn()
        conn.execute("INSERT INTO projects (name, category, created_at, updated_at) "
                     "VALUES ('Mine', 'in_development', 'x', 'x')")
        conn.execute("ALTER TABLE settings DROP COLUMN points_enabled")
        conn.execute("PRAGMA user_version = 1")
        db.init_schema()
        self.assertTrue(db.get_settings()["points_enabled"])
        snaps = list((self.path.parent / "backups").glob("pre-migration-v1-to-v2-*.db"))
        self.assertEqual(len(snaps), 1)


if __name__ == "__main__":
    unittest.main()
