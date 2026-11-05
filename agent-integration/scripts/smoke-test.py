#!/usr/bin/env python3
"""Exercise the portable workflow against an isolated Agenda database."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "mac" / "Agenda" / "vendor" / "python" / "bin" / "python3"
PORT = 18552
BASE = f"http://127.0.0.1:{PORT}"


def request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    with urllib.request.urlopen(
        urllib.request.Request(BASE + path, data=data, headers=headers, method=method),
        timeout=8,
    ) as response:
        if response.status == 204:
            return None
        return json.load(response)


def assert_missing(method: str, path: str, body: dict | None = None):
    try:
        request(method, path, body)
    except urllib.error.HTTPError as error:
        assert error.code == 404, (path, error.code)
    else:
        raise AssertionError(f"retired endpoint still active: {method} {path}")


def assert_http_error(
    expected_status: int,
    method: str,
    path: str,
    body: dict | None = None,
):
    try:
        request(method, path, body)
    except urllib.error.HTTPError as error:
        assert error.code == expected_status, (path, error.code)
    else:
        raise AssertionError(f"expected HTTP {expected_status}: {method} {path}")


with tempfile.TemporaryDirectory(prefix="agenda-agent-smoke-") as temporary:
    temp = Path(temporary)
    environment = {
        **os.environ,
        "AGENDA_DB_PATH": str(temp / "agenda.db"),
        "AGENDA_BACKUP_DIR": str(temp / "backups"),
        "AGENDA_BACKUP_INTERVAL": "86400",
        "AGENDA_LOCAL_AI_HELPER": "",
    }

    subprocess.run(
        [str(PYTHON), "seed.py"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    server_log = (temp / "server.log").open("w")
    server = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        cwd=ROOT,
        env=environment,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )

    try:
        for _ in range(80):
            try:
                board = request("GET", "/api/projects")
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise RuntimeError(f"isolated Agenda server did not start; see {temp / 'server.log'}")

        capabilities = request("GET", "/api/capabilities")
        assert capabilities["title_provider"] == "deterministic"
        assert "detail_suggestions" not in capabilities
        assert "assistant" not in capabilities
        assert capabilities["native_insights"]["fallback"] == "deterministic"
        assert_missing("GET", "/api/assistant/conversations")
        assert_missing("POST", "/api/assistant/chat", {"message": "hello"})
        assert_missing("POST", "/api/todos/1/suggest-details")
        assert_http_error(422, "GET", "/api/insights?mode=weekly_review")

        pulse = request("GET", "/api/insights?mode=plan_today")
        assert pulse["provider"] == "deterministic"
        assert pulse["needs_refresh"] is True
        refreshed = request(
            "POST",
            "/api/insights/refresh",
            {"mode": "plan_today"},
        )
        assert refreshed["provider"] == "deterministic"
        assert refreshed["needs_refresh"] is False
        if refreshed["insights"]:
            insight_id = refreshed["insights"][0]["id"]
            request(
                "PATCH",
                f"/api/insights/{insight_id}",
                {"action": "dismiss"},
            )
            after_dismiss = request("GET", "/api/insights?mode=plan_today")
            assert insight_id not in {
                item["id"] for item in after_dismiss["insights"]
            }
            request(
                "PATCH",
                f"/api/insights/{insight_id}",
                {"action": "clear"},
            )

        project = next(
            project
            for projects in board["projects"].values()
            for project in projects
        )
        raw_title = (
            "BUG: Investigate why a deliberately unavailable title provider "
            "must preserve a safe deterministic fallback"
        )
        deterministic = request(
            "POST",
            "/api/todos",
            {
                "project_id": project["id"],
                "text": raw_title,
                "raw_input": raw_title,
            },
        )
        assert deterministic["title_status"] == ""
        assert deterministic["text"].startswith("BUG: ")
        assert len(deterministic["text"]) <= 60
        assert len(deterministic["text"].removeprefix("BUG: ").split()) <= 8
        assert deterministic["notes"][0]["text"] == raw_title

        todo = request(
            "POST",
            "/api/todos",
            {
                "project_id": project["id"],
                "text": "Verify portable agent workflow",
                "notes": ["Created in an isolated integration test."],
                "paths": ["docs/example.md"],
            },
        )

        running = request("PATCH", f"/api/todos/{todo['id']}", {"running": True})
        assert running["running"] is True
        assert running["in_progress"] is False

        review = request(
            "POST",
            "/api/todos",
            {
                "project_id": project["id"],
                "parent_id": todo["id"],
                "text": "Review portable workflow",
                "for_later": False,
                "awaiting_review": True,
                "notes": ["Verify the isolated workflow behavior."],
                "paths": ["docs/example.md", f"todo #{todo['id']}"],
            },
        )
        assert review["parent_id"] == todo["id"]
        assert review["awaiting_review"] is True
        assert review["for_later"] is False
        assert review["is_followup"] is False
        request(
            "POST",
            "/api/todos/reorder",
            [{
                "id": review["id"],
                "project_id": project["id"],
                "parent_id": todo["id"],
                "display_order": 0,
            }],
        )

        legacy_parked = request(
            "POST",
            "/api/todos",
            {
                "project_id": project["id"],
                "text": "Legacy parked idea",
                "is_followup": True,
            },
        )
        assert legacy_parked["for_later"] is True
        assert legacy_parked["is_followup"] is True

        request(
            "POST",
            f"/api/todos/{todo['id']}/notes",
            {"text": "Pickup, context read, and running-state checks passed.", "author": "agent"},
        )
        query = urllib.parse.urlencode(
            {"q": "portable workflow", "include_completed": "true"}
        )
        search = request("GET", f"/api/search?{query}")
        assert str(todo["id"]) in json.dumps(search)

        request("PATCH", f"/api/todos/{todo['id']}", {"running": False})
        request("POST", f"/api/todos/{todo['id']}/complete")
        stats = request(
            "GET",
            "/api/stats?since=2000-01-01&tz_offset_minutes=0",
        )
        accomplishments = [
            item
            for items in stats["accomplishments_by_day"].values()
            for item in items
        ]
        assert any(item["todo_id"] == todo["id"] for item in accomplishments)
        assert any(
            item["project_name"] == project["name"]
            for item in accomplishments
            if item["todo_id"] == todo["id"]
        )
        final_board = request("GET", "/api/projects")
        final_todo = next(
            item
            for projects in final_board["projects"].values()
            for item_project in projects
            for item in item_project["todos"]
            if item["id"] == todo["id"]
        )
        assert final_todo["completed"] is True
        assert final_todo["running"] is False
        assert final_todo["in_progress"] is False

        workspace = temp / "workspace"
        rules = workspace / ".cursor" / "rules"
        rules.mkdir(parents=True)
        source_rules = ROOT / "agent-integration" / "cursor" / ".cursor" / "rules"
        for source in source_rules.glob("*.mdc"):
            shutil.copy2(source, rules / source.name)
        shutil.copy2(
            ROOT / "agent-integration" / "claude" / "CLAUDE.md",
            workspace / "CLAUDE.md",
        )
        assert len(list(rules.glob("*.mdc"))) == 2
        assert (workspace / "CLAUDE.md").is_file()

        print("Portable agent workflow smoke test passed.")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        server_log.close()
