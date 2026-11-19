#!/usr/bin/env python3
"""Agenda MCP server: the local Agenda API as MCP tools, over stdio.

Standard library only (Python 3.9+), so any agent client that can start a
local process can use it without installing anything. It talks to the
running Agenda app at AGENDA_URL (default http://127.0.0.1:8552).

The tools follow the agent workflow in this pack, and some of its rules are
enforced here rather than left to the agent:

- No tool sets or clears `in_progress`; that flag belongs to the user.
- `start_work` sets the agent-owned `running` flag; `close_out` always
  leaves a lab note and clears `running` (or completes the todo).
- Completing a todo needs a stated basis (the user approved it, or the agent
  created the todo for a request and verified the work) plus a project
  rollup note.
- New todos are never placed in `Later…`; `park_in_later` needs the user's
  approval quoted.
- Notes are appended, never replaced. Todo titles are at most eight words.

Register it with an MCP client as a stdio server, for example:

    claude mcp add agenda -- python3 /path/to/agent-integration/mcp/agenda_mcp.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

SERVER_NAME = "agenda"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_URL = "http://127.0.0.1:8552"
TIMEOUT_SECONDS = 8
MAX_TITLE_WORDS = 8

INSTRUCTIONS = """\
Agenda is the user's local project tracker. Workflow:
- Pickup: get_todo (or search_todos to find it) gives the todo, its notes, and
  the project's northstars, paths and recent notes. Read northstars first.
  Summarize and propose a plan; wait for the user's go-ahead before changing
  anything.
- Work the user asked for without naming a todo: search_todos first. Reuse
  an open todo that clearly is this work; otherwise create_todo with
  start_work=true and say "Tracking this as Agenda #N on <project>". Ask when
  several todos or projects could fit. Skip this for questions and read-only
  checks.
- Call start_work immediately before the first change (edit, commit, deploy,
  job), not after.
- Closeout: create any approved follow-ups first, then close_out with a terse
  2-4 sentence note. Complete only with the user's approval, or for a todo you
  created for their request once the work is verified.
- Use create_review_card only when a decision or judgment is left that only
  the user can make, never for checks you can run yourself.
- in_progress is the user's flag; there is no tool to change it. Later... is
  the user's parking area; park_in_later needs their explicit approval.
"""


class AgendaError(Exception):
    """A failure to report back to the agent as a tool error."""


# ---------------------------------------------------------------------------
# HTTP client for the local Agenda API
# ---------------------------------------------------------------------------

class AgendaClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or os.environ.get("AGENDA_URL") or DEFAULT_URL).rstrip("/")

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read() or b"{}")
                detail = payload.get("detail", "") if isinstance(payload, dict) else ""
            except Exception:
                pass
            raise AgendaError(f"Agenda API {method} {path} failed: HTTP {exc.code} {detail}".rstrip())
        except (urllib.error.URLError, OSError) as exc:
            raise AgendaError(
                f"Agenda isn't reachable at {self.base_url} ({exc}). "
                "Ask the user to open the Agenda app, then retry once."
            )
        if not raw:
            return None
        return json.loads(raw)

    def get(self, path: str, **params: Any) -> Any:
        if params:
            path += "?" + urllib.parse.urlencode(
                {k: ("true" if v is True else "false" if v is False else v) for k, v in params.items()}
            )
        return self.request("GET", path)


# ---------------------------------------------------------------------------
# Board helpers
# ---------------------------------------------------------------------------

def _row_labels(board: Dict[str, Any]) -> Dict[str, str]:
    return {r["key"]: r["label"] for r in board.get("rows", [])}


def _all_projects(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for projects in board.get("projects", {}).values():
        out.extend(projects)
    return out


def _flags(todo: Dict[str, Any]) -> List[str]:
    names = []
    if todo.get("completed"):
        names.append("completed")
    for key in ("running", "in_progress", "awaiting_review", "flagged", "is_blocker", "for_later"):
        if todo.get(key):
            names.append(key)
    return names


def _brief(todo: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"id": todo["id"], "title": todo["text"]}
    flags = _flags(todo)
    if flags:
        out["flags"] = flags
    for key in ("deadline", "effort"):
        if todo.get(key) is not None:
            out[key] = todo[key]
    return out


def _agent_notes(texts: List[str]) -> List[Dict[str, Any]]:
    """Creation notes as agent-authored entries (bare strings would be stored as the user's)."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [{"ts": ts, "author": "agent", "text": t} for t in texts]


def _notes(notes: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    return [{"ts": n.get("ts"), "author": n.get("author"), "text": n.get("text")} for n in notes[:limit]]


class Agenda:
    """The tool implementations. Each returns a JSON-serializable result."""

    def __init__(self, client: AgendaClient):
        self.client = client

    # -- lookups ------------------------------------------------------------

    def _board(self) -> Dict[str, Any]:
        return self.client.get("/api/projects", todo_notes="summary")

    def _locate(self, board: Dict[str, Any], todo_id: int):
        for project in _all_projects(board):
            for todo in project.get("todos", []):
                if todo["id"] == todo_id:
                    return project, todo
        raise AgendaError(f"Todo {todo_id} not found (it may be in an archived project).")

    def _project(self, board: Dict[str, Any], project_id: int) -> Dict[str, Any]:
        for project in _all_projects(board):
            if project["id"] == project_id:
                return project
        raise AgendaError(f"Project {project_id} not found (or archived).")

    def _todo_notes(self, ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        if not ids:
            return {}
        data = self.client.get("/api/todo-notes", ids=",".join(str(i) for i in ids))
        todos = (data or {}).get("todos", {}) if isinstance(data, dict) else {}
        return {int(tid): item.get("notes", []) for tid, item in todos.items()}

    @staticmethod
    def _by_id(project: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
        return {t["id"]: t for t in project.get("todos", [])}

    def _root(self, project: Dict[str, Any], todo: Dict[str, Any]) -> Dict[str, Any]:
        by_id = self._by_id(project)
        node = todo
        while node.get("parent_id") is not None and node["parent_id"] in by_id:
            node = by_id[node["parent_id"]]
        return node

    # -- read tools ---------------------------------------------------------

    def list_projects(self) -> Any:
        board = self._board()
        labels = _row_labels(board)
        out = []
        for key in board.get("categories", []):
            for project in board.get("projects", {}).get(key, []):
                todos = project.get("todos", [])
                open_todos = [t for t in todos if not t.get("completed")]
                out.append({
                    "id": project["id"],
                    "name": project["name"],
                    "row": labels.get(key, key),
                    "open_todos": len(open_todos),
                    "running_todos": [t["id"] for t in open_todos if t.get("running")],
                })
        return {"projects": out}

    def get_project(self, project_id: int, notes_limit: int = 5, include_later: bool = False) -> Any:
        board = self._board()
        project = self._project(board, project_id)
        todos = [t for t in project.get("todos", []) if not t.get("completed")]
        children: Dict[Optional[int], List[Dict[str, Any]]] = {}
        for t in todos:
            children.setdefault(t.get("parent_id"), []).append(t)

        outline: List[str] = []
        later_count = 0

        def walk(parent: Optional[int], depth: int) -> None:
            nonlocal later_count
            for t in children.get(parent, []):
                if depth == 0 and t.get("for_later") and not include_later:
                    later_count += 1
                    continue
                flags = [f for f in _flags(t) if f != "for_later" or depth == 0]
                tag = f" [{', '.join(flags)}]" if flags else ""
                outline.append(f"{'  ' * depth}#{t['id']} {t['text']}{tag}")
                walk(t["id"], depth + 1)

        walk(None, 0)
        result = {
            "id": project["id"],
            "name": project["name"],
            "row": _row_labels(board).get(project.get("category"), project.get("category")),
            "northstars": project.get("northstars", []),
            "paths": project.get("paths", []),
            "git_repo": project.get("git_repo"),
            "recent_notes": _notes(project.get("notes", []), notes_limit),
            "open_todos": outline,
        }
        if later_count:
            result["later_todos_hidden"] = later_count
        return result

    def get_todo(self, todo_id: int, notes_limit: int = 10) -> Any:
        board = self._board()
        project, todo = self._locate(board, todo_id)
        by_id = self._by_id(project)
        ancestors = []
        node = todo
        while node.get("parent_id") is not None and node["parent_id"] in by_id:
            node = by_id[node["parent_id"]]
            ancestors.insert(0, {"id": node["id"], "title": node["text"]})
        notes = self._todo_notes([todo_id]).get(todo_id, [])
        children = [
            _brief(t) for t in project.get("todos", [])
            if t.get("parent_id") == todo_id and not t.get("completed")
        ]
        detail = _brief(todo)
        detail.update({
            "paths": todo.get("paths", []),
            "notes": _notes(notes, notes_limit),
            "notes_total": len(notes),
        })
        return {
            "todo": detail,
            "ancestors": ancestors,
            "open_children": children,
            "project": {
                "id": project["id"],
                "name": project["name"],
                "northstars": project.get("northstars", []),
                "paths": project.get("paths", []),
                "git_repo": project.get("git_repo"),
                "recent_notes": _notes(project.get("notes", []), 5),
            },
        }

    def search_todos(self, query: str, include_completed: bool = False, limit: int = 20) -> Any:
        data = self.client.get("/api/search", q=query, include_completed=include_completed)
        results = []
        for r in (data or {}).get("results", [])[:limit]:
            item = _brief(r["todo"])
            item["project"] = {"id": r["project"]["id"], "name": r["project"]["name"]}
            if r.get("ancestors"):
                item["ancestors"] = [a.get("text") for a in r["ancestors"]]
            results.append(item)
        total = len((data or {}).get("results", []))
        out: Dict[str, Any] = {"query": query, "results": results}
        if total > limit:
            out["more_results"] = total - limit
        return out

    # -- write tools --------------------------------------------------------

    def start_work(self, todo_id: int) -> Any:
        board = self._board()
        _, todo = self._locate(board, todo_id)
        if todo.get("completed"):
            raise AgendaError(
                f"Todo {todo_id} is completed. Ask the user whether to reopen it or "
                "track the new work on a new todo."
            )
        self.client.request("PATCH", f"/api/todos/{todo_id}", {"running": True})
        msg = f"Todo {todo_id} is marked running."
        if todo.get("in_progress"):
            msg += " The user also has it in progress; that flag is theirs and was left alone."
        return msg

    def add_note(self, text: str, todo_id: Optional[int] = None, project_id: Optional[int] = None) -> Any:
        if (todo_id is None) == (project_id is None):
            raise AgendaError("Give exactly one of todo_id or project_id.")
        if todo_id is not None:
            self.client.request("POST", f"/api/todos/{todo_id}/notes", {"text": text, "author": "agent"})
            return f"Note added to todo {todo_id}."
        self.client.request("POST", f"/api/projects/{project_id}/notes", {"text": text, "author": "agent"})
        return f"Note added to project {project_id}."

    @staticmethod
    def _check_title(title: str) -> str:
        title = " ".join(title.split())
        if len(title.split()) > MAX_TITLE_WORDS:
            raise AgendaError(
                f"Titles are at most {MAX_TITLE_WORDS} words; put the detail in notes."
            )
        return title

    def _siblings(self, project: Dict[str, Any], todo: Dict[str, Any]) -> List[Dict[str, Any]]:
        group = [
            t for t in project.get("todos", [])
            if t.get("parent_id") == todo.get("parent_id")
            and bool(t.get("for_later")) == bool(todo.get("for_later"))
        ]
        return sorted(group, key=lambda t: (t.get("display_order", 0), t["id"]))

    def create_todo(
        self,
        project_id: int,
        title: str,
        notes: Optional[List[str]] = None,
        paths: Optional[List[str]] = None,
        parent_id: Optional[int] = None,
        follows_todo_id: Optional[int] = None,
        start_work: bool = False,
    ) -> Any:
        title = self._check_title(title)
        if parent_id is not None and follows_todo_id is not None:
            raise AgendaError("Give parent_id or follows_todo_id, not both.")
        board = self._board()
        project = self._project(board, project_id)
        antecedent = None
        if follows_todo_id is not None:
            antecedent_project, antecedent = self._locate(board, follows_todo_id)
            if antecedent_project["id"] != project_id:
                raise AgendaError(f"Todo {follows_todo_id} is in a different project.")
            parent_id = antecedent.get("parent_id")
            if parent_id is None and antecedent.get("for_later"):
                # New work is never parked; it goes at the top of the main list.
                antecedent = None

        body: Dict[str, Any] = {
            "project_id": project_id,
            "text": title,
            "for_later": False,
            "running": bool(start_work),
            "notes": _agent_notes(list(notes or [])),
            "paths": list(paths or []),
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        created = self.client.request("POST", "/api/todos", body)
        new_id = created["id"]

        placement = "top of its list"
        if antecedent is not None:
            fresh = self._project(self._board(), project_id)
            new_todo = self._by_id(fresh)[new_id]
            group = [t for t in self._siblings(fresh, new_todo) if t["id"] != new_id]
            ordered: List[Dict[str, Any]] = []
            for t in group:
                ordered.append(t)
                if t["id"] == antecedent["id"]:
                    ordered.append(new_todo)
            self.client.request("POST", "/api/todos/reorder", [
                {"id": t["id"], "parent_id": t.get("parent_id"), "display_order": i * 10}
                for i, t in enumerate(ordered)
            ])
            placement = f"directly below #{antecedent['id']}"
        elif follows_todo_id is not None:
            placement = f"top of the main list (#{follows_todo_id} is parked in Later)"

        where = f"child of #{parent_id}" if parent_id is not None and antecedent is None else project["name"]
        msg = f"Created todo #{new_id} \"{title}\" ({where}, {placement})."
        if start_work:
            msg += " It is marked running."
        return msg

    def create_review_card(
        self,
        todo_id: int,
        title: str,
        substance: str,
        decisions: str = "",
        your_check: str = "",
        paths: Optional[List[str]] = None,
    ) -> Any:
        title = self._check_title(title)
        board = self._board()
        project, todo = self._locate(board, todo_id)
        parent_id: Optional[int] = todo_id
        if self._root(project, todo).get("for_later"):
            parent_id = None  # a review under a parked todo would be hidden too
        notes = [f"Substance: {substance}"]
        if decisions:
            notes.append(f"Decisions: {decisions}")
        if your_check:
            notes.append(f"Your check: {your_check}")
        body: Dict[str, Any] = {
            "project_id": project["id"],
            "text": title,
            "for_later": False,
            "awaiting_review": True,
            "notes": _agent_notes(notes),
            "paths": list(paths or []) + [f"todo #{todo_id}"],
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        created = self.client.request("POST", "/api/todos", body)
        where = f"top child of #{todo_id}" if parent_id is not None else "top of the project's main list"
        return f"Created needs-review todo #{created['id']} ({where}). Leave #{todo_id} open."

    def close_out(
        self,
        todo_id: int,
        note: str = "",
        complete: bool = False,
        completion_basis: Optional[str] = None,
        project_rollup: str = "",
    ) -> Any:
        board = self._board()
        project, todo = self._locate(board, todo_id)
        done: List[str] = []
        if complete:
            if completion_basis not in ("user_approved", "agent_created_request_verified"):
                raise AgendaError(
                    "Completing needs completion_basis: 'user_approved' (the user agreed "
                    "in this conversation) or 'agent_created_request_verified' (you created "
                    "this todo for their request and verified the work). Otherwise call "
                    "close_out without complete."
                )
            if not project_rollup.strip():
                raise AgendaError("Completing needs project_rollup: 2-4 sentences for the project's notes.")
            if note.strip():
                self.client.request("POST", f"/api/todos/{todo_id}/notes", {"text": note, "author": "agent"})
                done.append("todo note added")
            self.client.request(
                "POST", f"/api/projects/{project['id']}/notes", {"text": project_rollup, "author": "agent"}
            )
            done.append(f"rollup added to project {project['id']}")
            if not todo.get("completed"):
                self.client.request("POST", f"/api/todos/{todo_id}/complete")
            done.append("todo completed")
        else:
            if not note.strip():
                raise AgendaError("Leave a 2-4 sentence note: what changed, where it stands, what's next.")
            self.client.request("POST", f"/api/todos/{todo_id}/notes", {"text": note, "author": "agent"})
            done.append("todo note added")
            if todo.get("running"):
                self.client.request("PATCH", f"/api/todos/{todo_id}", {"running": False})
            done.append("running cleared")
        return f"Todo {todo_id}: " + "; ".join(done) + "."

    def park_in_later(self, todo_id: int, user_approval: str) -> Any:
        if not user_approval.strip():
            raise AgendaError("Later is the user's parking area: quote their approval for this todo.")
        board = self._board()
        _, todo = self._locate(board, todo_id)
        if todo.get("parent_id") is not None:
            raise AgendaError("Only top-level todos can be parked; children follow their top-level todo.")
        self.client.request("PATCH", f"/api/todos/{todo_id}", {"for_later": True})
        self.client.request(
            "POST", f"/api/todos/{todo_id}/notes",
            {"text": f"Parked in Later with the user's approval: \"{user_approval.strip()}\"", "author": "agent"},
        )
        return f"Todo {todo_id} is parked in Later."


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _int(desc: str) -> Dict[str, Any]:
    return {"type": "integer", "description": desc}


def _str(desc: str) -> Dict[str, Any]:
    return {"type": "string", "description": desc}


def _bool(desc: str) -> Dict[str, Any]:
    return {"type": "boolean", "description": desc}


def _strs(desc: str) -> Dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_projects",
        "description": "List active projects with their board row, open-todo count and running todos.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": READ_ONLY,
    },
    {
        "name": "get_project",
        "description": "A project's northstars, paths, recent lab notes and open todos as an outline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _int("Project id."),
                "notes_limit": _int("Newest project notes to include (default 5)."),
                "include_later": _bool("Include todos parked in Later (default false)."),
            },
            "required": ["project_id"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "get_todo",
        "description": (
            "Everything needed to pick up a todo: the todo with its notes and paths, its "
            "ancestors and open children, and the project's northstars, paths and recent notes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "todo_id": _int("Todo id (the number after #)."),
                "notes_limit": _int("Newest todo notes to include (default 10)."),
            },
            "required": ["todo_id"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "search_todos",
        "description": (
            "Find todos by keyword. Use 2-4 distinctive nouns; any one matching is enough. "
            "'#123' or '123' matches that todo id. Run once with include_completed to catch "
            "work that's already done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": _str("Space-separated key nouns."),
                "include_completed": _bool("Also search completed todos (default false)."),
                "limit": _int("Maximum results (default 20)."),
            },
            "required": ["query"],
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "start_work",
        "description": (
            "Mark a todo running (agent is actively working it). Call immediately before the "
            "first change: edit, commit, deploy or job. Never touches the user's in_progress flag."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"todo_id": _int("Todo id.")},
            "required": ["todo_id"],
        },
        "annotations": {**WRITE, "idempotentHint": True},
    },
    {
        "name": "add_note",
        "description": (
            "Append a terse lab note (2-4 sentences: what changed, current state, next step, "
            "gotchas) to a todo or a project. Notes are appended, never replaced."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _str("The note."),
                "todo_id": _int("Todo to note (give this or project_id)."),
                "project_id": _int("Project to note (give this or todo_id)."),
            },
            "required": ["text"],
        },
        "annotations": WRITE,
    },
    {
        "name": "create_todo",
        "description": (
            "Create a todo. Search first; reuse an open todo that already covers the work. "
            "Placement: parent_id makes it a child (blocking work, distinct sub-deliverable); "
            "follows_todo_id places it directly below that todo as a sibling (later work that "
            "doesn't block it); neither puts it at the top of the project's main list. "
            "Follow-ups the agent proposes need the user's approval first; a todo for work the "
            "user just asked for does not. Never parked in Later."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": _int("Project id."),
                "title": _str(f"Short title, at most {MAX_TITLE_WORDS} words."),
                "notes": _strs("Initial notes, oldest first; detail goes here."),
                "paths": _strs("Related file or directory paths."),
                "parent_id": _int("Make it a child of this todo."),
                "follows_todo_id": _int("Place it directly below this todo, as its sibling."),
                "start_work": _bool("Also mark it running, for work starting now (default false)."),
            },
            "required": ["project_id", "title"],
        },
        "annotations": WRITE,
    },
    {
        "name": "create_review_card",
        "description": (
            "Hand agent-built work back to the user as a gold-star needs-review todo, placed at "
            "the top of the reviewed todo's children. Only when a decision the user hasn't "
            "endorsed, or a judgment only they can make, is still open. Never for counts, "
            "hashes, tests or other checks you can run yourself: run them and report results."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "todo_id": _int("The todo whose work is being reviewed (stays open)."),
                "title": _str(f"Short title, at most {MAX_TITLE_WORDS} words, e.g. 'Review copy-button placement'."),
                "substance": _str("What was achieved, in plain language, with check results as facts."),
                "decisions": _str("Each unendorsed choice made, and why it matters."),
                "your_check": _str("Short list for the user: only things only they can settle."),
                "paths": _strs("Touched files and artifacts."),
            },
            "required": ["todo_id", "title", "substance"],
        },
        "annotations": WRITE,
    },
    {
        "name": "close_out",
        "description": (
            "End the session on a todo. Create approved follow-ups first. Without complete: "
            "appends the note and clears running. With complete: needs completion_basis and a "
            "project_rollup (2-4 sentences: what this delivered, lessons that matter beyond it); "
            "appends both, then completes the todo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "todo_id": _int("Todo id."),
                "note": _str("2-4 sentence todo note. Required unless completing."),
                "complete": _bool("Mark the todo completed (default false)."),
                "completion_basis": {
                    "type": "string",
                    "enum": ["user_approved", "agent_created_request_verified"],
                    "description": (
                        "Why completing is allowed: the user agreed in this conversation, or you "
                        "created this todo for their request and verified the work, with nothing "
                        "left for their judgment."
                    ),
                },
                "project_rollup": _str("Project lab note. Required when completing."),
            },
            "required": ["todo_id"],
        },
        "annotations": WRITE,
    },
    {
        "name": "park_in_later",
        "description": (
            "Park a top-level todo in Later. Later is the user's own parking area: only with "
            "their explicit approval for this specific todo, never for follow-up work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "todo_id": _int("Top-level todo id."),
                "user_approval": _str("The user's words approving this, quoted."),
            },
            "required": ["todo_id", "user_approval"],
        },
        "annotations": WRITE,
    },
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def _validate(tool: Dict[str, Any], args: Any) -> Dict[str, Any]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise AgendaError("Arguments must be an object.")
    schema = tool["inputSchema"]
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args or args[key] is None:
            raise AgendaError(f"Missing required argument: {key}")
    clean: Dict[str, Any] = {}
    for key, value in args.items():
        if key not in props:
            raise AgendaError(f"Unknown argument: {key}")
        if value is None:
            continue
        kind = props[key]["type"]
        ok = {
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "array": isinstance(value, list) and all(isinstance(v, str) for v in value),
        }[kind]
        if not ok:
            raise AgendaError(f"Argument {key} must be {'a list of strings' if kind == 'array' else kind}.")
        if kind == "string" and key in schema.get("required", []) and not value.strip():
            raise AgendaError(f"Argument {key} can't be empty.")
        if "enum" in props[key] and value not in props[key]["enum"]:
            raise AgendaError(f"Argument {key} must be one of: {', '.join(props[key]['enum'])}.")
        clean[key] = value
    return clean


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, agenda: Agenda):
        self.agenda = agenda
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "initialize": self._initialize,
            "ping": lambda params: {},
            "tools/list": lambda params: {"tools": TOOLS},
            "tools/call": self._call_tool,
        }

    @staticmethod
    def _initialize(params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        tool = _TOOLS_BY_NAME.get(name)
        if tool is None:
            raise _RpcError(-32602, f"Unknown tool: {name}")
        try:
            args = _validate(tool, params.get("arguments"))
            result = getattr(self.agenda, name)(**args)
        except AgendaError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        text = result if isinstance(result, str) else json.dumps(result, indent=1, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """Return the response for one JSON-RPC message, or None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid request")
        if "method" not in message:
            return None  # a response to something we never send; ignore
        msg_id = message.get("id")
        is_notification = "id" not in message
        handler = self.handlers.get(message["method"])
        if handler is None:
            return None if is_notification else _error(msg_id, -32601, f"Method not found: {message['method']}")
        try:
            result = handler(message.get("params") or {})
        except _RpcError as exc:
            return None if is_notification else _error(msg_id, exc.code, exc.message)
        except Exception as exc:  # keep serving after an unexpected failure
            print(f"agenda-mcp: {message['method']} failed: {exc!r}", file=sys.stderr)
            return None if is_notification else _error(msg_id, -32603, f"Internal error: {exc}")
        return None if is_notification else {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                response: Optional[Dict[str, Any]] = _error(None, -32700, "Parse error")
            else:
                if isinstance(message, list):  # batch (2025-03-26 only)
                    responses = [r for r in (self.handle(m) for m in message) if r is not None]
                    response = responses or None  # type: ignore[assignment]
                else:
                    response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def main() -> None:
    Server(Agenda(AgendaClient())).serve()


if __name__ == "__main__":
    main()
