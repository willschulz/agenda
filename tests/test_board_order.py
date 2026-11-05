import asyncio
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

import db
import main
from board_order import build_board_order


class BoardOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA_SQL)
        db._migrate_add_sync_identity(self.conn)
        db._migrate_add_todo_effort(self.conn)
        db._migrate_add_todo_deadline(self.conn)
        self.today = date(2026, 8, 13)

    def tearDown(self):
        self.conn.close()

    def project(self, name, category, order):
        cursor = self.conn.execute(
            "INSERT INTO projects "
            "(name, category, display_order, created_at, updated_at, stable_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                category,
                order,
                "2026-08-01T12:00:00+00:00",
                "2026-08-01T12:00:00+00:00",
                f"project-{name}",
            ),
        )
        return cursor.lastrowid

    def todo(
        self,
        project_id,
        *,
        completed=False,
        completed_at=None,
        effort=None,
        deadline=None,
    ):
        self.conn.execute(
            "INSERT INTO todos "
            "(project_id, text, completed, completed_at, effort, deadline, "
            "created_at, updated_at, stable_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                "todo",
                int(completed),
                completed_at,
                effort,
                deadline,
                "2026-08-01T12:00:00+00:00",
                "2026-08-01T12:00:00+00:00",
                f"todo-{project_id}-{self.conn.total_changes}",
            ),
        )

    def order(self, category):
        return build_board_order(
            self.conn,
            today=self.today,
            utc_offset_minutes=0,
        )[category]

    def test_top_of_mind_stays_manual_despite_deadline_and_velocity(self):
        first = self.project("first", "top_of_mind", 0)
        second = self.project("second", "top_of_mind", 10)
        self.todo(first, deadline="2026-09-01")
        self.todo(
            second,
            completed=True,
            completed_at="2026-08-13T12:00:00+00:00",
            effort=13,
        )

        self.assertEqual(
            self.order("top_of_mind"),
            ["project-first", "project-second"],
        )

    def test_deadlines_precede_velocity_and_sort_earliest_first(self):
        later = self.project("later", "in_development", 0)
        fast = self.project("fast", "in_development", 10)
        sooner = self.project("sooner", "in_development", 20)
        self.todo(later, deadline="2026-09-01")
        self.todo(sooner, deadline="2026-08-20")
        self.todo(
            fast,
            completed=True,
            completed_at="2026-08-13T12:00:00+00:00",
            effort=21,
        )

        self.assertEqual(
            self.order("in_development"),
            ["project-sooner", "project-later", "project-fast"],
        )

    def test_deadline_ties_and_no_deadline_rows_use_velocity(self):
        slow_due = self.project("slow-due", "side_project", 0)
        fast_due = self.project("fast-due", "side_project", 10)
        slow = self.project("slow", "side_project", 20)
        fast = self.project("fast", "side_project", 30)
        for project_id in (slow_due, fast_due):
            self.todo(project_id, deadline="2026-08-30")
        for project_id, effort in (
            (slow_due, 1),
            (fast_due, 5),
            (slow, 1),
            (fast, 8),
        ):
            self.todo(
                project_id,
                completed=True,
                completed_at="2026-08-12T12:00:00+00:00",
                effort=effort,
            )

        self.assertEqual(
            self.order("side_project"),
            [
                "project-fast-due",
                "project-slow-due",
                "project-fast",
                "project-slow",
            ],
        )

    def test_unpointed_effort_and_manual_fallback_are_deterministic(self):
        pointed = self.project("pointed", "early_stage", 20)
        unpointed = self.project("unpointed", "early_stage", 0)
        inactive = self.project("inactive", "early_stage", 10)
        self.todo(
            pointed,
            completed=True,
            completed_at="2026-08-13T12:00:00+00:00",
            effort=1,
        )
        self.todo(
            unpointed,
            completed=True,
            completed_at="2026-08-13T12:00:00+00:00",
        )

        self.assertEqual(
            self.order("early_stage"),
            ["project-pointed", "project-unpointed", "project-inactive"],
        )

    def test_day_overrides_preserve_relative_velocity_order(self):
        fast = self.project("fast", "near_publication", 10)
        slow = self.project("slow", "near_publication", 0)
        self.todo(
            fast,
            completed=True,
            completed_at="2026-08-13T12:00:00+00:00",
            effort=2,
        )
        self.todo(
            slow,
            completed=True,
            completed_at="2026-08-13T12:00:00+00:00",
            effort=1,
        )
        self.conn.execute(
            "INSERT INTO day_overrides (date) VALUES (?)",
            ("2026-08-12",),
        )

        self.assertEqual(
            self.order("near_publication"),
            ["project-fast", "project-slow"],
        )

    def test_current_fixed_offset_matches_web_stats_day_grouping(self):
        near_midnight = self.project("near-midnight", "side_project", 10)
        inactive = self.project("inactive", "side_project", 0)
        self.todo(
            near_midnight,
            completed=True,
            completed_at="2026-11-01T04:30:00+00:00",
            effort=2,
        )

        rows = build_board_order(
            self.conn,
            today=date(2026, 11, 14),
            utc_offset_minutes=-300,
        )

        self.assertEqual(
            rows["side_project"],
            ["project-inactive", "project-near-midnight"],
        )

    def test_zero_effective_workdays_uses_manual_fallback(self):
        manual_first = self.project("manual-first", "side_project", 0)
        active = self.project("active", "side_project", 10)
        self.todo(
            active,
            completed=True,
            completed_at="2026-08-13T12:00:00+00:00",
            effort=13,
        )
        for offset in range(14):
            value = self.today.fromordinal(self.today.toordinal() - offset)
            if value.weekday() < 5:
                self.conn.execute(
                    "INSERT INTO day_overrides (date) VALUES (?)",
                    (value.isoformat(),),
                )

        self.assertEqual(
            self.order("side_project"),
            ["project-manual-first", "project-active"],
        )

    def test_sync_endpoint_uses_stable_ids_and_schema_two(self):
        self.project("one", "top_of_mind", 0)
        with patch.object(db, "get_conn", return_value=self.conn):
            result = asyncio.run(main.sync_board_order())

        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["rows"]["top_of_mind"], ["project-one"])


if __name__ == "__main__":
    unittest.main()
