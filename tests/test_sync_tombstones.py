import sqlite3
import unittest

import db
import sync_tombstones


class TombstoneScratchStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA_SQL)
        db._migrate_add_sync_identity(self.conn)
        db._migrate_add_sync_tombstones(self.conn)
        self._seed()

    def tearDown(self):
        self.conn.close()

    def _seed(self):
        now = "2026-08-09T19:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO projects (
                id, name, category, display_order, notes, paths_json,
                archived, created_at, updated_at, stable_uuid
            ) VALUES (1, 'Scratch', 'side_project', 0, '[]', '[]',
                      1, ?, ?, 'project-1')
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO todos (
                id, project_id, parent_id, text, is_blocker, for_later,
                display_order, in_progress, completed, notes, paths_json,
                title_status, created_at, updated_at, stable_uuid
            ) VALUES (1, 1, NULL, 'Root', 0, 0, 0, 0, 0, '[]', '[]',
                      '', ?, ?, 'todo-1')
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO todos (
                id, project_id, parent_id, text, is_blocker, for_later,
                display_order, in_progress, completed, notes, paths_json,
                title_status, created_at, updated_at, stable_uuid
            ) VALUES (2, 1, 1, 'Child', 0, 0, 10, 0, 0, '[]', '[]',
                      '', ?, ?, 'todo-2')
            """,
            (now, now),
        )

    def test_migration_is_additive_and_idempotent(self):
        db._migrate_add_sync_tombstones(self.conn)
        db._migrate_add_sync_tombstones(self.conn)
        project_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(projects)")
        }
        self.assertIn("sync_revision", project_columns)
        self.assertEqual(
            self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )

    def test_project_delete_captures_explicit_children_and_restores_in_order(self):
        self.conn.execute("BEGIN")
        count = sync_tombstones.delete_project(
            self.conn, 1, deleted_at="2026-08-09T20:00:00+00:00"
        )
        self.conn.execute("COMMIT")
        self.assertEqual(count, 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
        records = sync_tombstones.list_active(self.conn)
        self.assertEqual({record["stable_id"] for record in records}, {
            "project-1", "todo-1", "todo-2"
        })
        child = next(record for record in records if record["stable_id"] == "todo-2")
        self.assertEqual(child["parent_stable_id"], "todo-1")
        self.assertEqual(child["project_stable_id"], "project-1")
        self.assertEqual(child["revision"], 1)

        with self.assertRaisesRegex(
            sync_tombstones.TombstoneConflict, "containing project"
        ):
            sync_tombstones.restore(self.conn, "todo", "todo-1")
        project = sync_tombstones.restore(self.conn, "project", "project-1")
        root = sync_tombstones.restore(self.conn, "todo", "todo-1")
        child = sync_tombstones.restore(self.conn, "todo", "todo-2")
        self.assertEqual(project["sync_revision"], 2)
        self.assertEqual(root["sync_revision"], 2)
        self.assertEqual(child["parent_id"], root["id"])
        self.assertEqual(sync_tombstones.list_active(self.conn), [])

    def test_todo_delete_captures_descendants_and_is_rollback_safe(self):
        self.conn.execute("BEGIN")
        self.assertEqual(sync_tombstones.delete_todo_tree(self.conn, 1), 2)
        self.conn.execute("ROLLBACK")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0], 2)
        self.assertEqual(sync_tombstones.list_active(self.conn), [])

    def test_repeated_tombstone_insert_is_idempotent(self):
        row = self.conn.execute("SELECT * FROM todos WHERE id = 1").fetchone()
        for _ in range(2):
            sync_tombstones._insert_tombstone(
                self.conn,
                entity_kind="todo",
                row=row,
                project_stable_uuid="project-1",
                parent_stable_uuid=None,
                deleted_at="2026-08-09T20:00:00+00:00",
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sync_tombstones").fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
