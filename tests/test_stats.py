import sqlite3
import unittest
from unittest.mock import patch

import main


class StatsAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE projects (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL
            );
            CREATE TABLE todos (
              id INTEGER PRIMARY KEY,
              project_id INTEGER NOT NULL,
              text TEXT NOT NULL,
              completed INTEGER NOT NULL,
              completed_at TEXT,
              effort INTEGER,
              parent_id INTEGER
            );
            INSERT INTO projects (id, name) VALUES (1, 'Agenda');
            INSERT INTO todos (id, project_id, text, completed, completed_at, effort) VALUES
              (10, 1, 'Publish release', 1, '2026-08-08T01:30:00+00:00', 3),
              (11, 1, 'Write release notes', 1, '2026-08-08T00:30:00+00:00', NULL),
              (12, 1, 'Still open', 0, NULL, 5);
            """
        )

    async def asyncTearDown(self):
        self.conn.close()

    async def test_returns_timezone_correct_daily_accomplishments(self):
        with patch.object(main.db, "get_conn", return_value=self.conn):
            result = await main.stats(
                since="2026-08-07",
                tz_offset_minutes=240,
            )

        self.assertEqual(result["by_day"], {"2026-08-07": {"1": 3.25}})
        accomplishments = result["accomplishments_by_day"]["2026-08-07"]
        self.assertEqual(
            [item["text"] for item in accomplishments],
            ["Publish release", "Write release notes"],
        )
        self.assertEqual(accomplishments[0]["project_name"], "Agenda")
        self.assertEqual(accomplishments[1]["effort"], 0.25)
        self.assertEqual(result["total_completed"], 2)


if __name__ == "__main__":
    unittest.main()
