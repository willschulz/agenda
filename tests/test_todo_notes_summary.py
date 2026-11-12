"""Board payload without note text: /api/projects?todo_notes=summary + /api/todo-notes."""

import re
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

import db
import main

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


class TodoNotesSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agenda.db"
        self.old_path = db.DB_PATH
        self.old_conn = db._conn
        db.DB_PATH = self.path
        db._conn = db._connect(self.path)
        db.init_schema()
        self.conn = db.get_conn()
        project = await main.create_project(
            main.ProjectCreate(name="Notes", category="side_project")
        )
        self.project_id = project["id"]
        self.with_notes = await main.create_todo(
            main.TodoCreate(project_id=self.project_id, text="Has notes")
        )
        self.without_notes = await main.create_todo(
            main.TodoCreate(project_id=self.project_id, text="No notes")
        )
        await main.append_todo_note(
            self.with_notes["id"], main.NoteAppend(text="first note")
        )
        await main.append_todo_note(
            self.with_notes["id"], main.NoteAppend(text="second note", author="agent")
        )

    async def asyncTearDown(self):
        self.conn.close()
        db._conn = self.old_conn
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    async def _todos(self, mode):
        data = await main.list_projects(include_archived=True, todo_notes=mode)
        todos = {}
        for projects in data["projects"].values():
            for project in projects:
                for todo in project["todos"]:
                    todos[todo["id"]] = todo
        return todos

    async def test_full_mode_is_unchanged_default(self):
        todos = await self._todos("full")
        notes = todos[self.with_notes["id"]]["notes"]
        self.assertEqual(len(notes), 2)
        self.assertNotIn("notes_rev", todos[self.with_notes["id"]])

    async def test_summary_mode_drops_text_and_reports_metadata(self):
        full = await self._todos("full")
        todos = await self._todos("summary")
        todo = todos[self.with_notes["id"]]
        self.assertNotIn("notes", todo)
        self.assertEqual(todo["notes_count"], 2)
        latest = max(full[self.with_notes["id"]]["notes"], key=lambda n: n["ts"])["ts"]
        self.assertEqual(todo["notes_latest_ts"], latest)
        empty = todos[self.without_notes["id"]]
        self.assertEqual(empty["notes_count"], 0)
        self.assertIsNone(empty["notes_latest_ts"])
        self.assertNotEqual(todo["notes_rev"], empty["notes_rev"])

    async def test_todo_notes_endpoint_matches_summary_revisions(self):
        summary = await self._todos("summary")
        everything = (await main.list_todo_notes(ids=""))["todos"]
        tid = str(self.with_notes["id"])
        self.assertEqual(set(everything), {tid})  # todos without notes omitted
        self.assertEqual(everything[tid]["rev"], summary[self.with_notes["id"]]["notes_rev"])
        texts = [n["text"] for n in everything[tid]["notes"]]
        self.assertEqual(sorted(texts), ["first note", "second note"])

        picked = (await main.list_todo_notes(
            ids=f"{self.with_notes['id']},{self.without_notes['id']},999999"
        ))["todos"]
        self.assertEqual(set(picked), {tid, str(self.without_notes["id"])})
        self.assertEqual(picked[str(self.without_notes["id"])]["notes"], [])
        self.assertEqual(
            picked[str(self.without_notes["id"])]["rev"],
            summary[self.without_notes["id"]]["notes_rev"],
        )

    async def test_revision_changes_when_notes_change(self):
        before = (await self._todos("summary"))[self.with_notes["id"]]["notes_rev"]
        await main.append_todo_note(self.with_notes["id"], main.NoteAppend(text="third"))
        after = (await self._todos("summary"))[self.with_notes["id"]]["notes_rev"]
        self.assertNotEqual(before, after)

    async def test_invalid_ids_rejected(self):
        with self.assertRaises(HTTPException):
            await main.list_todo_notes(ids="1,abc")
        with self.assertRaises(HTTPException):
            await main.list_todo_notes(ids=",".join(str(i) for i in range(1, 502)))


class TodoNotesFrontendGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = INDEX.read_text(encoding="utf-8")

    def test_board_requests_summary_payload(self):
        self.assertIn("/api/projects?include_archived=true&todo_notes=summary", self.source)

    def test_context_editor_loads_fresh_notes_before_opening(self):
        # Saving the editor writes the whole notes list back; opening it on
        # cached or missing notes could silently drop notes.
        start = self.source.index("async function openTodoContextModal(")
        body = self.source[start:self.source.index("\nfunction ", start)]
        self.assertLess(body.index("fetchTodoNotesFresh("), body.index("_setNotes("))
        self.assertNotRegex(body, re.compile(r"_setNotes\([^)]*todo\.notes"))


if __name__ == "__main__":
    unittest.main()
