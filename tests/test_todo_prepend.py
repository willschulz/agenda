"""Default create placement: new todos prepend within their sibling group."""

import tempfile
import unittest
from pathlib import Path

import db
import main


class TodoPrependOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agenda.db"
        self.old_path = db.DB_PATH
        self.old_conn = db._conn
        db.DB_PATH = self.path
        db._conn = db._connect(self.path)
        db.init_schema()
        self.conn = db.get_conn()

    async def asyncTearDown(self):
        self.conn.close()
        db._conn = self.old_conn
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    async def _make_project(self):
        return await main.create_project(
            main.ProjectCreate(name="Prepend", category="side_project")
        )

    async def test_omitted_display_order_prepends_among_main_roots(self):
        project = await self._make_project()
        first = await main.create_todo(
            main.TodoCreate(project_id=project["id"], text="Oldest")
        )
        second = await main.create_todo(
            main.TodoCreate(project_id=project["id"], text="Newest")
        )

        self.assertEqual(first["display_order"], 0)
        self.assertEqual(second["display_order"], -10)

        ordered = self.conn.execute(
            "SELECT text FROM todos "
            "WHERE project_id = ? AND parent_id IS NULL AND for_later = 0 "
            "ORDER BY display_order, id",
            (project["id"],),
        ).fetchall()
        self.assertEqual([row["text"] for row in ordered], ["Newest", "Oldest"])

    async def test_explicit_display_order_is_preserved(self):
        project = await self._make_project()
        await main.create_todo(
            main.TodoCreate(project_id=project["id"], text="First")
        )
        pinned = await main.create_todo(
            main.TodoCreate(
                project_id=project["id"],
                text="Pinned",
                display_order=50,
            )
        )
        self.assertEqual(pinned["display_order"], 50)

    async def test_for_later_create_prepends_only_among_later_siblings(self):
        project = await self._make_project()
        main_root = await main.create_todo(
            main.TodoCreate(project_id=project["id"], text="Main")
        )
        later_first = await main.create_todo(
            main.TodoCreate(
                project_id=project["id"],
                text="Later A",
                for_later=True,
            )
        )
        later_second = await main.create_todo(
            main.TodoCreate(
                project_id=project["id"],
                text="Later B",
                for_later=True,
            )
        )

        self.assertEqual(main_root["display_order"], 0)
        self.assertFalse(main_root["for_later"])
        self.assertEqual(later_first["display_order"], 0)
        self.assertEqual(later_second["display_order"], -10)

        main_rows = self.conn.execute(
            "SELECT text FROM todos "
            "WHERE project_id = ? AND parent_id IS NULL AND for_later = 0 "
            "ORDER BY display_order, id",
            (project["id"],),
        ).fetchall()
        later_rows = self.conn.execute(
            "SELECT text FROM todos "
            "WHERE project_id = ? AND parent_id IS NULL AND for_later = 1 "
            "ORDER BY display_order, id",
            (project["id"],),
        ).fetchall()
        self.assertEqual([row["text"] for row in main_rows], ["Main"])
        self.assertEqual(
            [row["text"] for row in later_rows], ["Later B", "Later A"]
        )


if __name__ == "__main__":
    unittest.main()
