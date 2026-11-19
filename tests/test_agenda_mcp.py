"""The agent-integration MCP server, end to end against a real local API."""

import asyncio
import importlib.util
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn

import db
import main

_MCP_PATH = Path(__file__).resolve().parent.parent / "agent-integration" / "mcp" / "agenda_mcp.py"
_spec = importlib.util.spec_from_file_location("agenda_mcp", _MCP_PATH)
agenda_mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agenda_mcp)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AgendaMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_path, cls.old_conn = db.DB_PATH, db._conn
        db.DB_PATH = Path(cls.temp.name) / "agenda.db"
        db._conn = db._connect(db.DB_PATH)
        db.init_schema()
        port = _free_port()
        config = uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="error")
        cls.uvicorn = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.uvicorn.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 10
        while not cls.uvicorn.started and time.time() < deadline:
            time.sleep(0.02)
        cls.client = agenda_mcp.AgendaClient(f"http://127.0.0.1:{port}")
        cls.server = agenda_mcp.Server(agenda_mcp.Agenda(cls.client))

    @classmethod
    def tearDownClass(cls):
        cls.uvicorn.should_exit = True
        cls.thread.join(timeout=5)
        db._conn.close()
        db.DB_PATH, db._conn = cls.old_path, cls.old_conn
        cls.temp.cleanup()

    def setUp(self):
        project = asyncio.run(main.create_project(main.ProjectCreate(name=f"P{self.id()[-12:]}", category="side_project")))
        self.pid = project["id"]
        self._next_id = 0

    # -- helpers ------------------------------------------------------------

    def rpc(self, method, params=None, notify=False):
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = self._next_id
        return self.server.handle(msg)

    def call(self, name, **args):
        resp = self.rpc("tools/call", {"name": name, "arguments": args})
        result = resp["result"]
        text = result["content"][0]["text"]
        return result["isError"], text

    def ok(self, name, **args):
        is_error, text = self.call(name, **args)
        self.assertFalse(is_error, text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def todo(self, tid):
        row = db.get_conn().execute("SELECT * FROM todos WHERE id = ?", (tid,)).fetchone()
        return main._todo_to_dict(row)

    def make(self, text, **fields):
        return asyncio.run(main.create_todo(main.TodoCreate(project_id=self.pid, text=text, **fields)))["id"]

    # -- protocol -----------------------------------------------------------

    def test_initialize_negotiates_version_and_sends_instructions(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("in_progress", result["instructions"])
        unknown = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(unknown["protocolVersion"], agenda_mcp.SUPPORTED_PROTOCOL_VERSIONS[0])

    def test_notifications_get_no_response_and_unknown_methods_error(self):
        self.assertIsNone(self.rpc("notifications/initialized", notify=True))
        self.assertEqual(self.rpc("resources/list")["error"]["code"], -32601)

    def test_serve_reads_lines_and_reports_parse_errors(self):
        out = io.StringIO()
        self.server.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n'), out)
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(lines[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(lines[1]["error"]["code"], -32700)

    def test_no_tool_can_set_in_progress(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        for tool in tools:
            self.assertNotIn("in_progress", tool["inputSchema"]["properties"], tool["name"])
        tid = self.make("Task")
        is_error, text = self.call("start_work", todo_id=tid, in_progress=True)
        self.assertTrue(is_error)
        self.assertIn("Unknown argument", text)
        self.assertFalse(self.todo(tid)["in_progress"])

    def test_argument_types_are_checked(self):
        is_error, text = self.call("get_todo", todo_id="12")
        self.assertTrue(is_error)
        self.assertIn("integer", text)
        is_error, _ = self.call("get_todo")
        self.assertTrue(is_error)

    def test_unreachable_agenda_is_a_tool_error(self):
        server = agenda_mcp.Server(agenda_mcp.Agenda(agenda_mcp.AgendaClient(f"http://127.0.0.1:{_free_port()}")))
        resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "list_projects", "arguments": {}}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("open the Agenda app", resp["result"]["content"][0]["text"])

    # -- reads --------------------------------------------------------------

    def test_get_todo_returns_pickup_context(self):
        parent = self.make("Parent")
        child = self.make("Child", parent_id=parent, paths=["src/x.py"])
        self.make("Grandchild", parent_id=child)
        self.ok("add_note", todo_id=child, text="Older note")
        self.ok("add_note", todo_id=child, text="Newest note")
        self.ok("add_note", project_id=self.pid, text="Project note")
        asyncio.run(main.update_project(self.pid, main.ProjectUpdate(northstars=["Goal"])))

        ctx = self.ok("get_todo", todo_id=child)
        self.assertEqual(ctx["todo"]["title"], "Child")
        self.assertEqual(ctx["todo"]["paths"], ["src/x.py"])
        self.assertEqual([n["text"] for n in ctx["todo"]["notes"]], ["Newest note", "Older note"])
        self.assertEqual(ctx["ancestors"], [{"id": parent, "title": "Parent"}])
        self.assertEqual([c["title"] for c in ctx["open_children"]], ["Grandchild"])
        self.assertEqual(ctx["project"]["northstars"], ["Goal"])
        self.assertEqual(ctx["project"]["recent_notes"][0]["text"], "Project note")

    def test_get_project_outline_hides_later_and_completed(self):
        keep = self.make("Keep")
        self.make("Sub", parent_id=keep)
        self.make("Parked", for_later=True)
        done = self.make("Done")
        asyncio.run(main.complete_todo(done))
        proj = self.ok("get_project", project_id=self.pid)
        self.assertEqual(proj["open_todos"], [f"#{keep} Keep", f"  #{keep + 1} Sub"])
        self.assertEqual(proj["later_todos_hidden"], 1)
        with_later = self.ok("get_project", project_id=self.pid, include_later=True)
        self.assertTrue(any("Parked [for_later]" in line for line in with_later["open_todos"]))

    def test_search_and_list_projects(self):
        tid = self.make("Zebrafish pipeline")
        found = self.ok("search_todos", query="zebrafish")
        self.assertEqual([r["id"] for r in found["results"]], [tid])
        self.assertEqual(found["results"][0]["project"]["id"], self.pid)
        self.ok("start_work", todo_id=tid)
        listed = {p["id"]: p for p in self.ok("list_projects")["projects"]}
        self.assertEqual(listed[self.pid]["running_todos"], [tid])

    # -- writes -------------------------------------------------------------

    def test_start_work_sets_running_only(self):
        tid = self.make("Task", in_progress=True)
        text = self.ok("start_work", todo_id=tid)
        self.assertIn("left alone", text)
        todo = self.todo(tid)
        self.assertTrue(todo["running"])
        self.assertTrue(todo["in_progress"])

    def test_start_work_refuses_completed_todo(self):
        tid = self.make("Done")
        asyncio.run(main.complete_todo(tid))
        is_error, _ = self.call("start_work", todo_id=tid)
        self.assertTrue(is_error)
        self.assertFalse(self.todo(tid)["running"])

    def test_create_todo_enforces_title_length_and_never_later(self):
        is_error, text = self.call("create_todo", project_id=self.pid, title="one two three four five six seven eight nine")
        self.assertTrue(is_error)
        self.assertIn("8 words", text)
        text = self.ok("create_todo", project_id=self.pid, title="Short task", notes=["Detail"], start_work=True)
        tid = int(text.split("#")[1].split()[0])
        todo = self.todo(tid)
        self.assertFalse(todo["for_later"])
        self.assertTrue(todo["running"])
        self.assertEqual(todo["notes"][0]["text"], "Detail")
        self.assertEqual(todo["notes"][0]["author"], "agent")
        self.assertTrue(todo["notes"][0]["ts"])

    def test_create_todo_follows_antecedent(self):
        first = self.make("First")
        second = self.make("Second")  # prepends: order is Second, First
        text = self.ok("create_todo", project_id=self.pid, title="After second", follows_todo_id=second)
        new = int(text.split("#")[1].split()[0])
        rows = db.get_conn().execute(
            "SELECT id FROM todos WHERE project_id = ? AND parent_id IS NULL AND for_later = 0 "
            "ORDER BY display_order, id", (self.pid,)).fetchall()
        self.assertEqual([r["id"] for r in rows], [second, new, first])

    def test_create_todo_after_parked_antecedent_goes_to_main_list(self):
        parked = self.make("Parked", for_later=True)
        text = self.ok("create_todo", project_id=self.pid, title="Follow-up", follows_todo_id=parked)
        new = int(text.split("#")[1].split()[0])
        self.assertIn("parked in Later", text)
        self.assertFalse(self.todo(new)["for_later"])

    def test_review_card_is_top_child_unless_parked(self):
        work = self.make("Work")
        self.make("Existing child", parent_id=work)
        self.ok("create_review_card", todo_id=work, title="Review layout", substance="Built it",
                decisions="Picked A", your_check="Look at it")
        children = db.get_conn().execute(
            "SELECT * FROM todos WHERE parent_id = ? ORDER BY display_order, id", (work,)).fetchall()
        review = main._todo_to_dict(children[0])
        self.assertEqual(review["text"], "Review layout")
        self.assertTrue(review["awaiting_review"])
        self.assertFalse(review["for_later"])
        self.assertEqual([n["text"] for n in review["notes"]],
                         ["Substance: Built it", "Decisions: Picked A", "Your check: Look at it"])
        self.assertIn(f"todo #{work}", review["paths"])

        parked = self.make("Parked", for_later=True)
        text = self.ok("create_review_card", todo_id=parked, title="Review parked", substance="x")
        rid = int(text.split("#")[1].split()[0])
        review = self.todo(rid)
        self.assertIsNone(review["parent_id"])
        self.assertFalse(review["for_later"])

    def test_close_out_without_completing(self):
        tid = self.make("Task")
        self.ok("start_work", todo_id=tid)
        is_error, _ = self.call("close_out", todo_id=tid)
        self.assertTrue(is_error)  # a note is required
        self.ok("close_out", todo_id=tid, note="Did X; next Y.")
        todo = self.todo(tid)
        self.assertFalse(todo["running"])
        self.assertFalse(todo["completed"])
        self.assertEqual(todo["notes"][0]["text"], "Did X; next Y.")
        self.assertEqual(todo["notes"][0]["author"], "agent")

    def test_close_out_completion_needs_basis_and_rollup(self):
        tid = self.make("Task")
        is_error, _ = self.call("close_out", todo_id=tid, note="n", complete=True, project_rollup="r")
        self.assertTrue(is_error)
        is_error, _ = self.call("close_out", todo_id=tid, note="n", complete=True, completion_basis="user_approved")
        self.assertTrue(is_error)
        self.assertFalse(self.todo(tid)["completed"])
        self.ok("close_out", todo_id=tid, note="Final note", complete=True,
                completion_basis="agent_created_request_verified", project_rollup="Delivered X.")
        todo = self.todo(tid)
        self.assertTrue(todo["completed"])
        self.assertFalse(todo["running"])
        project = asyncio.run(main.list_projects(include_archived=False, todo_notes="full"))
        notes = next(p for ps in project["projects"].values() for p in ps if p["id"] == self.pid)["notes"]
        self.assertEqual(notes[0]["text"], "Delivered X.")

    def test_park_in_later_needs_quote_and_top_level(self):
        root = self.make("Root")
        child = self.make("Child", parent_id=root)
        is_error, _ = self.call("park_in_later", todo_id=root, user_approval="  ")
        self.assertTrue(is_error)
        is_error, _ = self.call("park_in_later", todo_id=child, user_approval="park it")
        self.assertTrue(is_error)
        self.ok("park_in_later", todo_id=root, user_approval="yes, park the root")
        todo = self.todo(root)
        self.assertTrue(todo["for_later"])
        self.assertIn("yes, park the root", todo["notes"][0]["text"])

    def test_add_note_needs_exactly_one_target(self):
        tid = self.make("Task")
        is_error, _ = self.call("add_note", text="x")
        self.assertTrue(is_error)
        is_error, _ = self.call("add_note", text="x", todo_id=tid, project_id=self.pid)
        self.assertTrue(is_error)



class McpSetupEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_bundled_python_and_server_path(self):
        setup = await main.mcp_setup()
        self.assertTrue(setup["available"])
        server = setup["client_config"]["mcpServers"]["agenda"]
        self.assertEqual(server["args"][:2], ["-I", "-B"])
        self.assertEqual(Path(server["args"][2]).resolve(), _MCP_PATH)
        self.assertIn("claude mcp add --scope user agenda -- ", setup["claude_code_command"])
        self.assertNotIn("env", server)

    async def test_non_default_port_sets_agenda_url(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"AGENDA_PORT": "8562"}):
            setup = await main.mcp_setup()
        server = setup["client_config"]["mcpServers"]["agenda"]
        self.assertEqual(server["env"], {"AGENDA_URL": "http://127.0.0.1:8562"})
        self.assertIn("--env AGENDA_URL=http://127.0.0.1:8562 agenda", setup["claude_code_command"])


if __name__ == "__main__":
    unittest.main()
