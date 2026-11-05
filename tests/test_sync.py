import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import db
import main


class ForLaterMigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE todos (
              id INTEGER PRIMARY KEY,
              is_followup INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO todos (id, is_followup) VALUES (1, 1);
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_renames_legacy_column_idempotently_without_changing_value(self):
        db._migrate_rename_followup_to_for_later(self.conn)
        db._migrate_rename_followup_to_for_later(self.conn)

        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(todos)")
        }
        self.assertIn("for_later", columns)
        self.assertNotIn("is_followup", columns)
        self.assertEqual(
            self.conn.execute(
                "SELECT for_later FROM todos WHERE id = 1"
            ).fetchone()["for_later"],
            1,
        )


class SyncIdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agenda.db"
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA_SQL)
        now = "2026-08-09T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO projects "
            "(name, category, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("Existing", "side_project", now, now),
        )
        self.conn.execute(
            "INSERT INTO todos "
            "(project_id, text, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (1, "Existing todo", now, now),
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_backfills_unique_stable_ids_idempotently(self):
        db._migrate_add_sync_identity(self.conn)
        first = self.conn.execute(
            "SELECT stable_uuid FROM projects WHERE id = 1"
        ).fetchone()["stable_uuid"]
        todo = self.conn.execute(
            "SELECT stable_uuid FROM todos WHERE id = 1"
        ).fetchone()["stable_uuid"]

        db._migrate_add_sync_identity(self.conn)
        second = self.conn.execute(
            "SELECT stable_uuid FROM projects WHERE id = 1"
        ).fetchone()["stable_uuid"]

        self.assertEqual(first, second)
        self.assertNotEqual(first, todo)
        self.assertEqual(
            self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )


class SyncAPIContractTests(unittest.IsolatedAsyncioTestCase):
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

    async def _make_project_and_todo(self, project_name=main.SYNC_TEST_PROJECT_NAME):
        project = await main.create_project(
            main.ProjectCreate(name=project_name, category="side_project")
        )
        todo = await main.create_todo(
            main.TodoCreate(project_id=project["id"], text="Round trip me")
        )
        return project, todo

    async def test_snapshot_uses_stable_ids_and_excludes_local_fields(self):
        project, todo = await self._make_project_and_todo()

        snapshot = await main.sync_snapshot()

        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["projects"][0]["stable_id"], project["stable_uuid"])
        self.assertEqual(snapshot["projects"][0]["structure_revision"], 1)
        self.assertEqual(snapshot["todos"][0]["stable_id"], todo["stable_uuid"])
        self.assertNotIn("id", snapshot["projects"][0])
        self.assertNotIn("git_repo", snapshot["projects"][0])
        self.assertNotIn("paths", snapshot["todos"][0])
        self.assertNotIn("running", snapshot["todos"][0])
        self.assertFalse(snapshot["todos"][0]["for_later"])
        self.assertFalse(snapshot["todos"][0]["is_followup"])

        canonical = await main.sync_canonical_v2()
        canonical_project = next(
            record for record in canonical["records"]
            if record["entity_kind"] == "project"
        )
        self.assertEqual(canonical_project["structure_revision"], 1)
        project_payload = json.loads(canonical_project["payload_json"])
        self.assertEqual(
            project_payload["structure"],
            [{
                "stable_id": todo["stable_uuid"],
                "parent_stable_id": None,
                "display_order": 0,
                "for_later": False,
            }],
        )

    async def test_reorder_bumps_project_structure_once_and_rejects_cycles(self):
        project, root = await self._make_project_and_todo("Structure")
        second = await main.create_todo(
            main.TodoCreate(project_id=project["id"], text="Second")
        )
        child = await main.create_todo(
            main.TodoCreate(
                project_id=project["id"],
                parent_id=root["id"],
                text="Child",
            )
        )
        await main.reorder_todos(
            [
                main.TodoReorderItem(
                    id=second["id"],
                    parent_id=None,
                    display_order=0,
                ),
                main.TodoReorderItem(
                    id=root["id"],
                    parent_id=None,
                    display_order=10,
                ),
            ]
        )
        revisions = self.conn.execute(
            """
            SELECT sync_revision, sync_structure_revision
            FROM projects WHERE id=?
            """,
            (project["id"],),
        ).fetchone()
        self.assertEqual(tuple(revisions), (4, 4))

        with self.assertRaises(HTTPException) as cycle:
            await main.reorder_todos(
                [
                    main.TodoReorderItem(
                        id=root["id"],
                        parent_id=child["id"],
                        display_order=0,
                    ),
                    main.TodoReorderItem(
                        id=child["id"],
                        parent_id=root["id"],
                        display_order=0,
                    ),
                ]
            )
        self.assertEqual(cycle.exception.status_code, 400)
        unchanged = self.conn.execute(
            """
            SELECT sync_revision, sync_structure_revision
            FROM projects WHERE id=?
            """,
            (project["id"],),
        ).fetchone()
        self.assertEqual(tuple(unchanged), (4, 4))

        await main.update_todo(
            second["id"],
            main.TodoUpdate(display_order=20),
        )
        after_patch = self.conn.execute(
            """
            SELECT sync_revision, sync_structure_revision
            FROM projects WHERE id=?
            """,
            (project["id"],),
        ).fetchone()
        self.assertEqual(tuple(after_patch), (5, 5))

        await main.delete_todo(child["id"])
        after_delete = self.conn.execute(
            """
            SELECT sync_revision, sync_structure_revision
            FROM projects WHERE id=?
            """,
            (project["id"],),
        ).fetchone()
        self.assertEqual(tuple(after_delete), (6, 6))

    async def test_for_later_accepts_legacy_alias_and_children_inherit_root(self):
        project = await main.create_project(
            main.ProjectCreate(name="Placement", category="side_project")
        )
        parked = await main.create_todo(
            main.TodoCreate.model_validate(
                {
                    "project_id": project["id"],
                    "text": "Parked legacy input",
                    "is_followup": True,
                }
            )
        )
        self.assertTrue(parked["for_later"])
        self.assertTrue(parked["is_followup"])

        main_root = await main.create_todo(
            main.TodoCreate(project_id=project["id"], text="Main antecedent")
        )
        review = await main.create_todo(
            main.TodoCreate(
                project_id=project["id"],
                parent_id=main_root["id"],
                text="Review child",
                for_later=True,
                awaiting_review=True,
            )
        )
        self.assertFalse(review["for_later"])
        self.assertFalse(review["is_followup"])
        self.assertTrue(review["awaiting_review"])

    async def test_test_apply_is_scoped_and_conflict_checked(self):
        _, todo = await self._make_project_and_todo()
        payload = main.SyncTestTodoApply(
            schema_version=1,
            stable_id=todo["stable_uuid"],
            text="Edited on iPhone",
            base_updated_at=todo["updated_at"],
        )

        with patch.object(main, "_now", return_value="2026-08-09T00:00:01+00:00"):
            updated = await main.apply_sync_test_todo(payload)
        self.assertEqual(updated["text"], "Edited on iPhone")

        with self.assertRaises(HTTPException) as conflict:
            await main.apply_sync_test_todo(payload)
        self.assertEqual(conflict.exception.status_code, 409)

        _, outside = await self._make_project_and_todo("Not a sync project")
        with self.assertRaises(HTTPException) as forbidden:
            await main.apply_sync_test_todo(
                main.SyncTestTodoApply(
                    schema_version=1,
                    stable_id=outside["stable_uuid"],
                    text="Must not apply",
                    base_updated_at=outside["updated_at"],
                )
            )
        self.assertEqual(forbidden.exception.status_code, 403)

    async def test_delete_exports_explicit_tombstones_and_restore_is_ordered(self):
        project, root = await self._make_project_and_todo("Recoverable")
        child = await main.create_todo(
            main.TodoCreate(
                project_id=project["id"],
                parent_id=root["id"],
                text="Child",
            )
        )
        await main.update_project(
            project["id"], main.ProjectUpdate(archived=True)
        )
        await main.delete_project(project["id"])

        snapshot = await main.sync_tombstone_snapshot()
        records = snapshot["tombstones"]
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(
            {record["stable_id"] for record in records},
            {project["stable_uuid"], root["stable_uuid"], child["stable_uuid"]},
        )
        self.assertEqual(
            next(
                record for record in records
                if record["stable_id"] == child["stable_uuid"]
            )["parent_stable_id"],
            root["stable_uuid"],
        )

        with self.assertRaises(HTTPException) as blocked:
            await main.restore_sync_tombstone(
                "todo", root["stable_uuid"]
            )
        self.assertEqual(blocked.exception.status_code, 409)
        restored_project = await main.restore_sync_tombstone(
            "project", project["stable_uuid"]
        )
        restored_root = await main.restore_sync_tombstone(
            "todo", root["stable_uuid"]
        )
        restored_child = await main.restore_sync_tombstone(
            "todo", child["stable_uuid"]
        )
        self.assertEqual(restored_project["stable_uuid"], project["stable_uuid"])
        self.assertEqual(restored_child["parent_id"], restored_root["id"])
        self.assertEqual(
            (await main.sync_tombstone_snapshot())["tombstones"], []
        )


if __name__ == "__main__":
    unittest.main()

