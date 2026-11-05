"""
Agenda -- FastAPI backend.

Run locally:
    uvicorn main:app --reload --port 8551

In the native app, PythonSupervisor (mac/Agenda) runs this server on
127.0.0.1:8552.

The server binds to localhost, which is the auth perimeter; there is no
app-level auth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field, field_validator

import board_order
import db
import insights as native_insights
import sync_recovery
import sync_tombstones
import todo_structure
import usage_collect
import usage_ledger
import usage_cursor
import usage_quota

LOCAL_AI_HELPER = os.environ.get("AGENDA_LOCAL_AI_HELPER", "").strip()
LLM_TITLE_THRESHOLD = 60  # chars; inputs longer than this trigger LLM generation
TITLE_MAX_WORDS = 8
TITLE_MAX_CHARACTERS = 60
LOCAL_AI_TIMEOUT_SECONDS = 45
_local_ai_availability: dict[str, Any] = {
    "status": "unavailable",
    "reason": "helper-not-configured",
}
# Completed todos with effort unset (UI "–") count at this weight in /api/stats.
DEFAULT_UNPOINTED_EFFORT = 0.25
SYNC_WIRE_SCHEMA_VERSION = 1
SYNC_TEST_PROJECT_NAME = "CloudKit Sync Test"

log = logging.getLogger("agenda")

STATIC_DIR = Path(__file__).resolve().parent / "static"
READ_ONLY = os.environ.get("AGENDA_READ_ONLY", "").lower() in {"1", "true", "yes"}

# ── Live-update (SSE) ─────────────────────────────────────────────────────────
# Every write endpoint calls _notify_change() so connected browser tabs reload
# immediately. The version counter lets the browser skip redundant reloads.
_board_version: int = 0
_board_event: asyncio.Event = asyncio.Event()


def _notify_change() -> None:
    """Increment the board version and wake all waiting SSE clients."""
    global _board_version, _board_event
    _board_version += 1
    old = _board_event
    _board_event = asyncio.Event()  # fresh event for the next wait cycle
    old.set()                        # wake all current waiters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """ISO-8601 UTC timestamp, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def note_entry(text: str, author: str = "human", ts: str | None = None) -> dict:
    """Create a canonical lab-note entry."""
    return {"ts": ts or _now(), "author": author, "text": text}


def note_text(entry) -> str:
    """Extract plain text from a lab-note entry or a legacy bare string."""
    if isinstance(entry, dict):
        return entry.get("text", "")
    return str(entry) if entry is not None else ""


def normalize_notes(items, default_ts: str | None = None) -> list:
    """Coerce a list (or JSON string) of notes to canonical entry dicts.

    Bare strings are wrapped as {ts: default_ts, author: 'human', text: s}.
    Dicts that already have a 'text' key are passed through unchanged.
    """
    if isinstance(items, str):
        try:
            items = json.loads(items or "[]")
        except Exception:
            items = []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if isinstance(item, dict) and "text" in item:
            result.append(item)
        elif isinstance(item, str):
            result.append({"ts": default_ts, "author": "human", "text": item})
    return result


def _compute_project_deadlines(todos_by_project: dict[int, list[dict]]) -> dict[int, str | None]:
    """Return a mapping of project_id -> soonest non-completed todo deadline (or None)."""
    result: dict[int, str | None] = {}
    for pid, todos in todos_by_project.items():
        dates = [t["deadline"] for t in todos if not t["completed"] and t.get("deadline")]
        result[pid] = min(dates) if dates else None
    return result


def _project_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["paths"] = json.loads(d.pop("paths_json") or "[]")
    except Exception:
        d["paths"] = []
    d["notes"] = normalize_notes(d.get("notes") or "[]")
    try:
        ns = json.loads(d.get("northstars") or "[]")
        d["northstars"] = ns if isinstance(ns, list) else []
    except Exception:
        d["northstars"] = []
    d["archived"] = bool(d["archived"])
    return d


def _todo_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    d["is_blocker"] = bool(d["is_blocker"])
    d["for_later"] = bool(d["for_later"])
    # One-release compatibility mirror for older app clients.
    d["is_followup"] = d["for_later"]
    d["in_progress"] = bool(d.get("in_progress", 0))
    d["flagged"] = bool(d.get("flagged", 0))
    d["running"] = bool(d.get("running", 0))
    d["awaiting_review"] = bool(d.get("awaiting_review", 0))
    d["completed"] = bool(d["completed"])
    d["title_status"] = d.get("title_status") or ""
    d["suggestions_status"] = d.get("suggestions_status") or ""
    try:
        d["suggestions_json"] = json.loads(d.get("suggestions_json") or "[]")
        if not isinstance(d["suggestions_json"], list):
            d["suggestions_json"] = []
    except Exception:
        d["suggestions_json"] = []
    try:
        d["paths"] = json.loads(d.pop("paths_json") or "[]")
    except Exception:
        d["paths"] = []
    d["notes"] = normalize_notes(d.get("notes") or "[]")
    return d


def _sync_snapshot(conn) -> dict[str, Any]:
    """Serialize device-portable board fields without local integer IDs."""
    projects = conn.execute(
        "SELECT * FROM projects WHERE sync_deleted_at IS NULL "
        "ORDER BY category, display_order, id"
    ).fetchall()
    todos = conn.execute(
        "SELECT t.*, p.stable_uuid AS project_stable_uuid, "
        "parent.stable_uuid AS parent_stable_uuid "
        "FROM todos t "
        "JOIN projects p ON p.id = t.project_id "
        "LEFT JOIN todos parent ON parent.id = t.parent_id "
        "WHERE t.sync_deleted_at IS NULL AND p.sync_deleted_at IS NULL "
        "ORDER BY t.project_id, t.display_order, t.id"
    ).fetchall()
    return {
        "schema_version": SYNC_WIRE_SCHEMA_VERSION,
        "generated_at": _now(),
        "projects": [
            {
                "stable_id": row["stable_uuid"],
                "name": row["name"],
                "category": row["category"],
                "display_order": row["display_order"],
                "structure_revision": row["sync_structure_revision"],
                "notes": normalize_notes(row["notes"] or "[]"),
                "northstars": json.loads(row["northstars"] or "[]"),
                "archived": bool(row["archived"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in projects
        ],
        "todos": [
            {
                "stable_id": row["stable_uuid"],
                "project_stable_id": row["project_stable_uuid"],
                "parent_stable_id": row["parent_stable_uuid"],
                "text": row["text"],
                "is_blocker": bool(row["is_blocker"]),
                "for_later": bool(row["for_later"]),
                "is_followup": bool(row["for_later"]),
                "in_progress": bool(row["in_progress"]),
                "flagged": bool(row["flagged"]),
                "awaiting_review": bool(row["awaiting_review"]),
                "display_order": row["display_order"],
                "completed": bool(row["completed"]),
                "completed_at": row["completed_at"],
                "notes": normalize_notes(row["notes"] or "[]"),
                "effort": row["effort"],
                "deadline": row["deadline"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in todos
        ],
    }


def _root_for_later(conn, parent_id: int) -> bool:
    """Walk up the parent chain to find the root todo's for_later value."""
    row = conn.execute("SELECT parent_id, for_later FROM todos WHERE id = ?", (parent_id,)).fetchone()
    if row is None:
        return False
    if row["parent_id"] is None:
        return bool(row["for_later"])
    return _root_for_later(conn, row["parent_id"])


def _next_display_order(table: str, where_sql: str, params: tuple) -> int:
    cur = db.get_conn().execute(
        f"SELECT COALESCE(MAX(display_order), -1) + 1 AS next FROM {table} WHERE {where_sql}",
        params,
    )
    return int(cur.fetchone()["next"])


def _prepend_todo_display_order(
    project_id: int, parent_id: Optional[int], for_later: bool
) -> int:
    """Place a new todo at the front of its sibling group (MIN - 10; empty → 0)."""
    cur = db.get_conn().execute(
        "SELECT COALESCE(MIN(display_order), 10) - 10 AS next FROM todos "
        "WHERE project_id = ? AND parent_id IS ? AND for_later = ?",
        (project_id, parent_id, int(for_later)),
    )
    return int(cur.fetchone()["next"])


_KNOWN_TAGS = {"fr": "FR", "bug": "BUG", "nit": "NIT"}
_TAG_RE = re.compile(r'^(fr|bug|nit):\s*', re.IGNORECASE)


def _extract_tag(raw: str) -> tuple[str, str]:
    """Return (normalized_prefix, body) if raw starts with fr:/bug:/nit:, else ('', raw)."""
    m = _TAG_RE.match(raw)
    if m:
        return f"{_KNOWN_TAGS[m.group(1).lower()]}: ", raw[m.end():]
    return "", raw


def _normalize_title_candidate(raw: str, prefix: str = "") -> str | None:
    """Accept only one-line, compact provider output."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    body = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", lines[0]).strip()
    if (
        len(body) >= 2
        and body[0] == body[-1]
        and body[0] in {'"', "'", "`"}
    ):
        body = body[1:-1].strip()
    body = body.rstrip(".!?;").strip()
    for known_prefix in ("FR: ", "BUG: ", "NIT: "):
        if body.upper().startswith(known_prefix):
            body = body[len(known_prefix):].strip()
            break
    title = prefix + body
    if (
        not body
        or len(body.split()) > TITLE_MAX_WORDS
        or len(title) > TITLE_MAX_CHARACTERS
    ):
        return None
    return title


def _fallback_title(raw: str, prefix: str = "") -> str:
    """Return a deterministic word-boundary title satisfying the same contract."""
    body = raw.strip()
    for known_prefix in ("FR: ", "BUG: ", "NIT: "):
        if body.upper().startswith(known_prefix):
            body = body[len(known_prefix):].strip()
            break
    words = body.split()
    available = max(1, TITLE_MAX_CHARACTERS - len(prefix))
    selected: list[str] = []
    for word in words:
        if len(selected) == TITLE_MAX_WORDS:
            break
        candidate = " ".join([*selected, word])
        if len(candidate) > available:
            break
        selected.append(word)
    truncated = len(selected) < len(words)
    result = " ".join(selected)
    if not result and words:
        result = words[0][: max(1, available - 1)]
        truncated = len(result) < len(words[0])
    if truncated:
        result = result[: max(1, available - 1)].rstrip() + "…"
    return prefix + result


async def _invoke_local_ai(
    payload: dict[str, Any],
    timeout_seconds: Optional[float] = None,
) -> dict[str, Any]:
    if not LOCAL_AI_HELPER or not os.access(LOCAL_AI_HELPER, os.X_OK):
        return {"status": "unavailable", "reason": "helper-not-configured"}
    process = await asyncio.create_subprocess_exec(
        LOCAL_AI_HELPER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()),
            timeout=(
                timeout_seconds
                if timeout_seconds is not None
                else LOCAL_AI_TIMEOUT_SECONDS
            ),
        )
    except (TimeoutError, asyncio.TimeoutError):
        process.kill()
        await process.wait()
        return {"status": "error", "reason": "timeout"}
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        return {"status": "error", "reason": detail or f"exit-{process.returncode}"}
    try:
        result = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": "error", "reason": "invalid-json"}
    return result if isinstance(result, dict) else {
        "status": "error",
        "reason": "invalid-response",
    }


async def _probe_local_ai() -> dict[str, Any]:
    return await _invoke_local_ai({"operation": "availability"})


def _title_provider_configured() -> bool:
    return _local_ai_availability.get("status") == "available"


async def _local_title(body: str, prefix: str) -> str | None:
    result = await _invoke_local_ai(
        {"operation": "title", "text": body, "prefix": prefix}
    )
    if result.get("status") != "ok":
        log.info("local title unavailable: %s", result.get("reason") or result.get("status"))
        return None
    return _normalize_title_candidate(str(result.get("title") or ""), prefix)


async def _select_generated_title(raw_input: str) -> tuple[str | None, str | None]:
    """Use Apple Foundation Models when available; never make a network call."""
    if _local_ai_availability.get("status") != "available":
        return None, None
    prefix, body = _extract_tag(raw_input)
    try:
        title = await _local_title(body, prefix)
    except Exception as exc:
        log.warning("local title provider failed: %s", exc)
        return None, None
    if title:
        return title, "apple-foundation-models"
    log.warning("local title provider returned invalid or empty output")
    return None, None


async def _generate_title(todo_id: int, raw_input: str) -> None:
    """Generate a bounded title without ever replacing the safe fallback on failure."""
    new_text, provider = await _select_generated_title(raw_input)
    new_status = "generated" if new_text else "failed"
    if provider:
        log.info("_generate_title todo_id=%d provider=%s", todo_id, provider)

    conn = db.get_conn()
    now = _now()
    if new_text:
        conn.execute(
            "UPDATE todos SET text=?, title_status=?, updated_at=? WHERE id=?",
            (new_text, new_status, now, todo_id),
        )
    else:
        conn.execute(
            "UPDATE todos SET title_status=?, updated_at=? WHERE id=?",
            (new_status, now, todo_id),
        )
    _notify_change()


async def _run_backup_loop() -> None:
    """Create retained local snapshots without blocking API requests."""
    interval = max(300, int(os.environ.get("AGENDA_BACKUP_INTERVAL", "21600")))
    while True:
        await asyncio.sleep(interval)
        try:
            path = await asyncio.to_thread(db.create_backup)
            log.info("automatic database backup created: %s", path)
        except Exception:
            log.exception("automatic database backup failed")


_usage_scan_lock = asyncio.Lock()


def _usage_state_path() -> Path:
    return db.DB_PATH.parent / "usage-collector-state.json"


def _scan_local_usage() -> dict[str, int]:
    """Collect this Mac's AI usage and ingest it directly (runs in a thread)."""
    import socket

    state_path = _usage_state_path()
    state = usage_collect._load_state(state_path)
    found = usage_collect.collect_claude_code(state)
    config = usage_ledger.get_config()
    totals = {"inserted": 0, "duplicates": 0, "sessions": 0, "markers_inserted": 0}
    for part in usage_ledger.iter_batches(found):
        batch = usage_collect.build_batch(
            config["local_machine_id"], socket.gethostname(),
            usage_collect.SOURCE_CLAUDE_CODE, part,
        )
        for k, v in usage_ledger.ingest(batch).items():
            totals[k] += v
    usage_collect._save_state(state_path, state)  # only after ingest committed
    return totals


async def _run_usage_scan(force: bool = False) -> dict[str, Any]:
    if not force and not usage_ledger.get_config()["enabled"]:
        return {"status": "disabled"}
    async with _usage_scan_lock:
        try:
            counts = await asyncio.to_thread(_scan_local_usage)
        except Exception:
            log.exception("AI usage scan failed")
            usage_ledger.record_scan("error", None)
            return {"status": "error"}
    usage_ledger.record_scan("ok", counts)
    return {"status": "ok", **counts}


async def _run_quota_ingest_loop() -> None:
    """Pick up status-line usage-limit snapshots every 30 seconds."""
    while True:
        try:
            await asyncio.to_thread(usage_quota.ingest_spool)
        except Exception:
            log.exception("quota snapshot ingest failed")
        await asyncio.sleep(30)


async def _run_cursor_quota_loop() -> None:
    """Poll Cursor's usage-limit percentages every 10 minutes."""
    await asyncio.sleep(10)
    while True:
        try:
            status = await asyncio.to_thread(usage_cursor.poll_once)
            if status == "ok":
                await asyncio.to_thread(usage_quota.ingest_spool)
        except Exception:
            log.exception("Cursor usage poll failed")
        await asyncio.sleep(usage_cursor.POLL_SECONDS)


async def _run_usage_scan_loop() -> None:
    """Scan local AI tool logs periodically while the user has opted in."""
    await asyncio.sleep(30)
    while True:
        try:
            await _run_usage_scan()
            minutes = usage_ledger.get_config()["scan_interval_minutes"]
        except Exception:
            log.exception("AI usage scan loop error")
            minutes = 15
        await asyncio.sleep(max(1, minutes) * 60)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    category: str
    notes: list = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    northstars: list[str] = Field(default_factory=list)
    git_repo: Optional[str] = None  # absolute path to the git repo root, if tracked
    display_order: Optional[int] = None

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str) -> str:
        keys = db.row_keys()
        if v not in keys:
            raise ValueError(f"category must be one of the board's rows: {keys}")
        return v


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    category: Optional[str] = None
    notes: Optional[list] = None
    paths: Optional[list[str]] = None
    northstars: Optional[list[str]] = None
    git_repo: Optional[str] = None       # pass "" to clear
    git_repo_set: bool = False           # explicit: True means use git_repo (incl null/empty)
    display_order: Optional[int] = None
    archived: Optional[bool] = None

    @field_validator("category")
    @classmethod
    def _check_category(cls, v):
        if v is not None:
            keys = db.row_keys()
            if v not in keys:
                raise ValueError(f"category must be one of the board's rows: {keys}")
        return v


class BoardRowIn(BaseModel):
    key: Optional[str] = None            # existing row
    id: Optional[str] = None             # "new:<n>" for a row created in this edit
    label: str = Field(..., max_length=200)


class BoardRowDeletion(BaseModel):
    key: str
    move_to: str                         # existing key or a "new:<n>" id


class BoardRowsPlan(BaseModel):
    rows: list[BoardRowIn] = Field(..., min_length=1, max_length=50)
    deletions: list[BoardRowDeletion] = Field(default_factory=list, max_length=50)


class TodoReorderItem(BaseModel):
    id: int
    project_id: Optional[int] = None   # when set, may differ from current DB value to move todo
    parent_id: Optional[int] = None
    display_order: int
    for_later: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices("for_later", "is_followup"),
    )


class TodoCreate(BaseModel):
    project_id: int
    text: str = Field(..., min_length=1)
    parent_id: Optional[int] = None
    is_blocker: bool = False
    for_later: bool = Field(
        default=False,
        validation_alias=AliasChoices("for_later", "is_followup"),
    )
    in_progress: bool = False
    flagged: bool = False
    running: bool = False
    awaiting_review: bool = False
    display_order: Optional[int] = None
    notes: list = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    effort: Optional[int] = None
    deadline: Optional[str] = None  # ISO date "YYYY-MM-DD"
    raw_input: Optional[str] = None  # if provided and long, triggers LLM title generation

    @field_validator("effort")
    @classmethod
    def _check_effort(cls, v):
        if v is not None and v not in (1, 2, 3, 5, 8, 13):
            raise ValueError("effort must be one of 1, 2, 3, 5, 8, 13")
        return v


class TodoUpdate(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1)
    parent_id: Optional[int] = None
    parent_id_set: bool = False
    is_blocker: Optional[bool] = None
    for_later: Optional[bool] = Field(
        default=None,
        validation_alias=AliasChoices("for_later", "is_followup"),
    )
    in_progress: Optional[bool] = None
    flagged: Optional[bool] = None
    running: Optional[bool] = None
    awaiting_review: Optional[bool] = None
    display_order: Optional[int] = None
    completed: Optional[bool] = None
    notes: Optional[list] = None
    paths: Optional[list[str]] = None
    effort: Optional[int] = None
    effort_set: bool = False   # True means write effort (including null to clear)
    deadline: Optional[str] = None  # ISO date "YYYY-MM-DD"
    deadline_set: bool = False      # True means write deadline (including null to clear)
    title_status: Optional[str] = None       # internal: ''|'pending'|'generated'|'failed'
    suggestions_json: Optional[str] = None   # JSON-encoded list[str]; pass '[]' to clear
    suggestions_status: Optional[str] = None  # internal: ''|'pending'|'generated'|'failed'

    @field_validator("effort")
    @classmethod
    def _check_effort(cls, v):
        if v is not None and v not in (1, 2, 3, 5, 8, 13):
            raise ValueError("effort must be one of 1, 2, 3, 5, 8, 13")
        return v

    @field_validator("title_status")
    @classmethod
    def _check_title_status(cls, v):
        if v is not None and v not in ("", "pending", "generated", "failed"):
            raise ValueError("title_status must be one of '', 'pending', 'generated', 'failed'")
        return v

    @field_validator("suggestions_status")
    @classmethod
    def _check_suggestions_status(cls, v):
        if v is not None and v not in ("", "pending", "generated", "failed"):
            raise ValueError("suggestions_status must be one of '', 'pending', 'generated', 'failed'")
        return v


class SyncTestTodoApply(BaseModel):
    schema_version: int
    stable_id: str
    text: str = Field(..., min_length=1, max_length=500)
    base_updated_at: str

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != SYNC_WIRE_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SYNC_WIRE_SCHEMA_VERSION}"
            )
        return value

    @field_validator("stable_id")
    @classmethod
    def _check_stable_id(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("stable_id must be a UUID") from exc


class SyncMutationApply(BaseModel):
    schema_version: int
    mutation_id: str
    device_id: str
    entity_kind: Optional[str] = None
    entity_id: str
    operation: str
    base_revision: int
    base_structure_revision: Optional[int] = None
    note_id: Optional[str] = None
    note_payload: Optional[str] = None
    proposed_payload: Optional[str] = None


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _local_ai_availability
    db.init_schema()
    _local_ai_availability = await _probe_local_ai()
    log.info("local AI title availability: %s", _local_ai_availability)
    try:
        backup_path = await asyncio.to_thread(db.create_backup)
        log.info("startup database backup created: %s", backup_path)
    except Exception:
        # A backup problem must be visible in logs but must never make the
        # user's local board unavailable.
        log.exception("startup database backup failed")
    backup_task = asyncio.create_task(_run_backup_loop())
    usage_task = asyncio.create_task(_run_usage_scan_loop())
    quota_task = asyncio.create_task(_run_quota_ingest_loop())
    cursor_task = asyncio.create_task(_run_cursor_quota_loop())
    conn = db.get_conn()
    now = _now()
    abandoned_titles = conn.execute(
        "SELECT COUNT(*) FROM todos WHERE title_status = 'pending'"
    ).fetchone()[0]
    if abandoned_titles:
        terminal_status = "failed" if _title_provider_configured() else ""
        conn.execute(
            "UPDATE todos SET title_status=?, updated_at=? WHERE title_status='pending'",
            (terminal_status, now),
        )
        print(
            f"[startup] reset {abandoned_titles} abandoned pending-title todos "
            f"to {terminal_status or 'idle'}"
        )
    abandoned_suggestions = conn.execute(
        "SELECT COUNT(*) FROM todos WHERE suggestions_status = 'pending'"
    ).fetchone()[0]
    if abandoned_suggestions:
        terminal_status = ""
        conn.execute(
            "UPDATE todos SET suggestions_status=?, updated_at=? WHERE suggestions_status='pending'",
            (terminal_status, now),
        )
        print(
            f"[startup] reset {abandoned_suggestions} abandoned pending-suggestions "
            f"todos to {terminal_status or 'idle'}"
        )
    try:
        yield
    finally:
        for task in (backup_task, usage_task, quota_task, cursor_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Agenda", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _read_only_guard(request: Request, call_next):
    """Prevent replica fallback from accumulating unsyncable local writes."""
    if READ_ONLY and request.method != "GET":
        return JSONResponse(
            status_code=503,
            content={"detail": "This local Agenda replica is read-only until reconnect sync is available."},
        )
    return await call_next(request)


@app.middleware("http")
async def _board_version_header(request: Request, call_next):
    """Attach X-Board-Version to every mutation response.

    The frontend reads this after any write and updates _liveVersion so the
    pending SSE event for that same mutation is seen as v <= _liveVersion and
    skipped — eliminating the redundant reload/flash after user actions.
    Background SSE events (from other clients or async tasks) still trigger
    reloads because they produce a version higher than anything the client has
    seen.
    """
    response = await call_next(request)
    if request.method in ("POST", "PATCH", "DELETE"):
        response.headers["X-Board-Version"] = str(_board_version)
    return response


# ---------------------------------------------------------------------------
# Static / PWA routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/apple-touch-icon.png", include_in_schema=False)
async def apple_touch_icon():
    return FileResponse(STATIC_DIR / "icons" / "apple-touch-icon.png", media_type="image/png")


@app.get("/icon-192.png", include_in_schema=False)
async def icon_192():
    return FileResponse(STATIC_DIR / "icons" / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png", include_in_schema=False)
async def icon_512():
    return FileResponse(STATIC_DIR / "icons" / "icon-512.png", media_type="image/png")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav = STATIC_DIR / "icons" / "favicon.ico"
    if fav.exists():
        return FileResponse(fav, media_type="image/x-icon")
    return FileResponse(STATIC_DIR / "icons" / "icon-192.png", media_type="image/png")


# ---------------------------------------------------------------------------
# API: projects
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    """Lightweight health check — no DB I/O."""
    return {"ok": True}


@app.get("/api/events")
async def board_events(request: Request):
    """Server-Sent Events: push board_version to all connected clients on any write.

    Clients reconnect automatically (SSE spec); a 25 s keepalive comment prevents
    proxy/browser timeout between real events. The X-Accel-Buffering header disables
    nginx response buffering so events are flushed immediately.
    """
    async def generator():
        yield f"data: {_board_version}\n\n"
        while True:
            ev = _board_event
            try:
                await asyncio.wait_for(asyncio.shield(ev.wait()), timeout=25.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if await request.is_disconnected():
                return
            yield f"data: {_board_version}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/projects")
async def list_projects(include_archived: bool = Query(default=False)):
    """All projects with their todos, grouped by category."""
    conn = db.get_conn()
    where = "" if include_archived else "WHERE archived = 0"
    proj_rows = conn.execute(
        f"SELECT * FROM projects {where} "
        "ORDER BY category, display_order, id"
    ).fetchall()
    todo_rows = conn.execute(
        "SELECT * FROM todos ORDER BY project_id, "
        "for_later, (parent_id IS NOT NULL), display_order, id"
    ).fetchall()

    todos_by_project: dict[int, list[dict]] = {}
    for r in todo_rows:
        todos_by_project.setdefault(r["project_id"], []).append(_todo_to_dict(r))

    computed_deadlines = _compute_project_deadlines(todos_by_project)

    rows = db.list_rows(conn)
    grouped: dict[str, list[dict]] = {r["key"]: [] for r in rows}
    for r in proj_rows:
        proj = _project_to_dict(r)
        proj["todos"] = todos_by_project.get(proj["id"], [])
        proj["deadline"] = computed_deadlines.get(proj["id"])
        grouped.setdefault(proj["category"], []).append(proj)

    return {
        "categories": [r["key"] for r in rows],
        "rows": [{"key": r["key"], "label": r["label"], "locked": r["locked"]} for r in rows],
        "projects": grouped,
    }


@app.get("/api/sync/snapshot")
async def sync_snapshot():
    """Versioned, read-only device snapshot keyed only by stable UUIDs."""
    return _sync_snapshot(db.get_conn())


@app.get("/api/sync/board-order")
async def sync_board_order():
    """Mac-parity per-category project order keyed by stable UUID."""
    return {
        "schema_version": 2,
        "generated_at": _now(),
        "rows": board_order.build_board_order(db.get_conn()),
        # Optional (older clients ignore it): the user's rows, top to bottom.
        "row_definitions": [
            {"key": r["key"], "label": r["label"]} for r in db.list_rows()
        ],
    }


@app.get("/api/sync/tombstones")
async def sync_tombstone_snapshot():
    """Active schema-2 tombstones emitted from Mac-canonical deletion state."""
    return {
        "schema_version": sync_tombstones.SCHEMA_VERSION,
        "generated_at": _now(),
        "tombstones": sync_tombstones.list_active(db.get_conn()),
    }


@app.get("/api/sync/canonical-v2")
async def sync_canonical_v2():
    """Mac-authored live envelopes that can supersede older tombstones."""
    conn = db.get_conn()
    snapshot = _sync_snapshot(conn)
    project_revisions = {
        row["stable_uuid"]: row["sync_revision"]
        for row in conn.execute(
            "SELECT stable_uuid, sync_revision FROM projects"
        ).fetchall()
    }
    project_structure_revisions = {
        row["stable_uuid"]: row["sync_structure_revision"]
        for row in conn.execute(
            "SELECT stable_uuid, sync_structure_revision FROM projects"
        ).fetchall()
    }
    todo_revisions = {
        row["stable_uuid"]: row["sync_revision"]
        for row in conn.execute(
            "SELECT stable_uuid, sync_revision FROM todos"
        ).fetchall()
    }
    records = [
        {
            "schema_version": 2,
            "entity_kind": "project",
            "stable_id": value["stable_id"],
            "revision": project_revisions[value["stable_id"]],
            "structure_revision": project_structure_revisions[value["stable_id"]],
            "deleted": False,
            "payload_json": json.dumps(
                {
                    **value,
                    "structure": [
                        {
                            "stable_id": todo["stable_id"],
                            "parent_stable_id": todo["parent_stable_id"],
                            "display_order": todo["display_order"],
                            "for_later": todo["for_later"],
                        }
                        for todo in snapshot["todos"]
                        if todo["project_stable_id"] == value["stable_id"]
                    ],
                },
                sort_keys=True,
            ),
        }
        for value in snapshot["projects"]
    ] + [
        {
            "schema_version": 2,
            "entity_kind": "todo",
            "stable_id": value["stable_id"],
            "revision": todo_revisions[value["stable_id"]],
            "deleted": False,
            "payload_json": json.dumps(value, sort_keys=True),
        }
        for value in snapshot["todos"]
    ]
    return {"schema_version": 2, "generated_at": _now(), "records": records}


@app.post("/api/sync/mutations")
async def apply_sync_mutation(payload: SyncMutationApply):
    entity_kind = payload.entity_kind
    if entity_kind is None:
        conn = db.get_conn()
        if conn.execute(
            "SELECT 1 FROM projects WHERE stable_uuid = ?", (payload.entity_id,)
        ).fetchone():
            entity_kind = "project"
        elif conn.execute(
            "SELECT 1 FROM todos WHERE stable_uuid = ?", (payload.entity_id,)
        ).fetchone():
            entity_kind = "todo"
        elif conn.execute(
            "SELECT entity_kind FROM sync_tombstones "
            "WHERE stable_uuid = ? ORDER BY revision DESC LIMIT 1",
            (payload.entity_id,),
        ).fetchone() is not None:
            entity_kind = conn.execute(
                "SELECT entity_kind FROM sync_tombstones "
                "WHERE stable_uuid = ? ORDER BY revision DESC LIMIT 1",
                (payload.entity_id,),
            ).fetchone()["entity_kind"]
    if entity_kind not in {"project", "todo"}:
        raise HTTPException(status_code=400, detail="unsupported entity kind")
    try:
        mutation = sync_recovery.proposal_from_dict(payload.model_dump())
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="invalid mutation") from error
    with db.transaction() as txn:
        result = sync_recovery.apply_mutation(
            txn,
            entity_kind=entity_kind,
            mutation=mutation,
        )
    if result["outcome"] == "accept":
        _notify_change()
    return result


@app.get("/api/sync/results")
async def sync_results():
    return {
        "schema_version": 2,
        "results": sync_recovery.list_results(db.get_conn()),
    }


@app.get("/api/sync/conflicts")
async def sync_conflicts():
    return {
        "schema_version": 2,
        "conflicts": sync_recovery.list_conflicts(db.get_conn()),
    }


@app.post("/api/sync/tombstones/{entity_kind}/{stable_id}/restore")
async def restore_sync_tombstone(entity_kind: str, stable_id: str):
    """Explicitly resurrect one entity; parents must be restored first."""
    try:
        with db.transaction() as txn:
            row = sync_tombstones.restore(txn, entity_kind, stable_id)
    except sync_tombstones.TombstoneConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _notify_change()
    if entity_kind == "project":
        return _project_with_todos(row["id"])
    return _todo_to_dict(row)


@app.post("/api/sync/test-todo")
async def apply_sync_test_todo(payload: SyncTestTodoApply):
    """Apply one optimistic text edit inside the designated test project."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT t.id, t.updated_at, p.name AS project_name "
        "FROM todos t JOIN projects p ON p.id = t.project_id "
        "WHERE t.stable_uuid = ? AND t.sync_deleted_at IS NULL",
        (payload.stable_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="sync todo not found")
    if row["project_name"] != SYNC_TEST_PROJECT_NAME:
        raise HTTPException(
            status_code=403,
            detail="inbound sync is limited to the CloudKit Sync Test project",
        )
    if row["updated_at"] != payload.base_updated_at:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "concurrent_change",
                "current_updated_at": row["updated_at"],
            },
        )
    now = _now()
    cur = conn.execute(
        "UPDATE todos SET text = ?, updated_at = ? "
        "WHERE id = ? AND updated_at = ?",
        (payload.text.strip(), now, row["id"], payload.base_updated_at),
    )
    if cur.rowcount != 1:
        raise HTTPException(status_code=409, detail="concurrent_change")
    updated = conn.execute(
        "SELECT * FROM todos WHERE id = ?", (row["id"],)
    ).fetchone()
    _notify_change()
    return {
        "schema_version": SYNC_WIRE_SCHEMA_VERSION,
        "stable_id": updated["stable_uuid"],
        "text": updated["text"],
        "updated_at": updated["updated_at"],
    }


@app.post("/api/projects", status_code=201)
async def create_project(payload: ProjectCreate):
    conn = db.get_conn()
    now = _now()
    order = (
        payload.display_order
        if payload.display_order is not None
        else _next_display_order(
            "projects", "category = ? AND archived = 0", (payload.category,)
        )
    )
    cur = conn.execute(
        "INSERT INTO projects "
        "(name, category, display_order, deadline, notes, paths_json, git_repo, northstars, archived, created_at, updated_at, stable_uuid) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            payload.name.strip(),
            payload.category,
            order,
            json.dumps(normalize_notes(payload.notes, default_ts=now)),
            json.dumps(payload.paths),
            payload.git_repo or None,
            json.dumps(payload.northstars),
            now,
            now,
            str(uuid.uuid4()),
        ),
    )
    pid = cur.lastrowid
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    proj = _project_to_dict(row)
    proj["todos"] = []
    return proj


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: int, payload: ProjectUpdate):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")

    fields: list[str] = []
    values: list[Any] = []
    if payload.name is not None:
        fields.append("name = ?")
        values.append(payload.name.strip())
    if payload.category is not None:
        fields.append("category = ?")
        values.append(payload.category)
    if payload.notes is not None:
        fields.append("notes = ?")
        values.append(json.dumps(normalize_notes(payload.notes, default_ts=_now())))
    if payload.northstars is not None:
        fields.append("northstars = ?")
        values.append(json.dumps(payload.northstars))
    if payload.paths is not None:
        fields.append("paths_json = ?")
        values.append(json.dumps(payload.paths))
    if payload.git_repo_set:
        fields.append("git_repo = ?")
        values.append(payload.git_repo.strip() if payload.git_repo else None)
    if payload.display_order is not None:
        fields.append("display_order = ?")
        values.append(payload.display_order)
    if payload.archived is not None:
        fields.append("archived = ?")
        values.append(1 if payload.archived else 0)

    if not fields:
        return _project_with_todos(project_id)

    fields.append("updated_at = ?")
    values.append(_now())
    values.append(project_id)
    conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", values)
    _notify_change()
    return _project_with_todos(project_id)


@app.delete("/api/projects/{project_id}", status_code=204)
async def delete_project(project_id: int):
    with db.transaction() as txn:
        row = txn.execute(
            "SELECT archived FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not row["archived"]:
            raise HTTPException(
                status_code=409, detail="archive the project before deleting"
            )
        sync_tombstones.delete_project(txn, project_id)
    _notify_change()
    return None


def _project_with_todos(project_id: int) -> dict[str, Any]:
    conn = db.get_conn()
    pr = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if pr is None:
        raise HTTPException(status_code=404, detail="project not found")
    proj = _project_to_dict(pr)
    todos = conn.execute(
        "SELECT * FROM todos WHERE project_id = ? "
        "ORDER BY for_later, (parent_id IS NOT NULL), display_order, id",
        (project_id,),
    ).fetchall()
    proj["todos"] = [_todo_to_dict(t) for t in todos]
    todos_by_project = {project_id: proj["todos"]}
    proj["deadline"] = _compute_project_deadlines(todos_by_project).get(project_id)
    return proj


# ---------------------------------------------------------------------------
# API: todos
# ---------------------------------------------------------------------------

@app.post("/api/todos", status_code=201)
async def create_todo(payload: TodoCreate):
    conn = db.get_conn()
    proj = conn.execute("SELECT id FROM projects WHERE id = ?", (payload.project_id,)).fetchone()
    if proj is None:
        raise HTTPException(status_code=404, detail="project not found")
    # Children inherit Later placement from their root ancestor.
    effective_for_later = payload.for_later
    if payload.parent_id is not None:
        parent = conn.execute(
            "SELECT id, project_id FROM todos WHERE id = ?", (payload.parent_id,)
        ).fetchone()
        if parent is None:
            raise HTTPException(status_code=404, detail="parent todo not found")
        if parent["project_id"] != payload.project_id:
            raise HTTPException(status_code=400, detail="parent todo belongs to a different project")
        effective_for_later = _root_for_later(conn, payload.parent_id)

    now = _now()
    order = (
        payload.display_order
        if payload.display_order is not None
        else _prepend_todo_display_order(
            payload.project_id, payload.parent_id, effective_for_later
        )
    )

    # Determine text, notes, and title_status based on raw_input length.
    raw = payload.raw_input.strip() if payload.raw_input else None
    has_long_raw = bool(raw and len(raw) > LLM_TITLE_THRESHOLD)
    use_llm = bool(has_long_raw and _title_provider_configured())
    if has_long_raw:
        tag, body = _extract_tag(raw)
        todo_text = _fallback_title(body, prefix=tag)
        # Store raw as first entry so retry-title can recover it via note_text(notes[0]).
        todo_notes = [note_entry(raw, "human", now)] + normalize_notes(payload.notes, default_ts=now)
        title_status = "pending" if use_llm else ""
        suggestions_status = ""
    else:
        todo_text = payload.text.strip()
        todo_notes = normalize_notes(payload.notes, default_ts=now)
        title_status = ""
        suggestions_status = ""

    with db.transaction() as txn:
        cur = txn.execute(
            "INSERT INTO todos "
            "(project_id, parent_id, text, is_blocker, for_later, in_progress, flagged, running, awaiting_review, display_order, "
            " completed, completed_at, notes, paths_json, title_status, suggestions_json, "
            " suggestions_status, deadline, created_at, updated_at, stable_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, '[]', ?, ?, ?, ?, ?)",
            (
                payload.project_id,
                payload.parent_id,
                todo_text,
                int(payload.is_blocker),
                int(effective_for_later),
                int(payload.in_progress),
                int(payload.flagged),
                int(payload.running),
                int(payload.awaiting_review),
                order,
                json.dumps(todo_notes),
                json.dumps(payload.paths),
                title_status,
                suggestions_status,
                payload.deadline,
                now,
                now,
                str(uuid.uuid4()),
            ),
        )
        tid = cur.lastrowid
        todo_structure.bump_project_structure_revisions(
            txn,
            [payload.project_id],
            updated_at=now,
        )

    if use_llm:
        asyncio.create_task(_generate_title(tid, raw))

    row = conn.execute("SELECT * FROM todos WHERE id = ?", (tid,)).fetchone()
    _notify_change()
    return _todo_to_dict(row)


@app.patch("/api/todos/{todo_id}")
async def update_todo(todo_id: int, payload: TodoUpdate):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="todo not found")
    structural_change = payload.parent_id_set or payload.display_order is not None
    if payload.parent_id_set and payload.parent_id is not None:
        parent = conn.execute(
            "SELECT id, project_id FROM todos WHERE id = ?",
            (payload.parent_id,),
        ).fetchone()
        if parent is None:
            raise HTTPException(status_code=404, detail="parent todo not found")
        if parent["project_id"] != row["project_id"]:
            raise HTTPException(
                status_code=400,
                detail="parent todo belongs to a different project",
            )
        try:
            todo_structure.validate_no_cycles(
                conn,
                {todo_id: payload.parent_id},
            )
        except todo_structure.StructureValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    fields: list[str] = []
    values: list[Any] = []
    if payload.text is not None:
        fields.append("text = ?")
        values.append(payload.text.strip())
    if payload.parent_id_set:
        fields.append("parent_id = ?")
        values.append(payload.parent_id)
        # When reparenting, inherit Later placement from the new root ancestor.
        if payload.parent_id is not None:
            inherited = _root_for_later(conn, payload.parent_id)
            fields.append("for_later = ?")
            values.append(int(inherited))
    if payload.is_blocker is not None:
        fields.append("is_blocker = ?")
        values.append(int(payload.is_blocker))
    if payload.for_later is not None:
        fields.append("for_later = ?")
        values.append(int(payload.for_later))
    if payload.in_progress is not None:
        fields.append("in_progress = ?")
        values.append(int(payload.in_progress))
    if payload.flagged is not None:
        fields.append("flagged = ?")
        values.append(int(payload.flagged))
    if payload.running is not None:
        fields.append("running = ?")
        values.append(int(payload.running))
    if payload.awaiting_review is not None:
        fields.append("awaiting_review = ?")
        values.append(int(payload.awaiting_review))
    if payload.display_order is not None:
        fields.append("display_order = ?")
        values.append(payload.display_order)
    if payload.completed is not None:
        fields.append("completed = ?")
        values.append(int(payload.completed))
        if payload.completed and not row["completed"]:
            fields.append("completed_at = ?")
            values.append(_now())
            fields.append("in_progress = ?")
            values.append(0)
            fields.append("flagged = ?")
            values.append(0)
            fields.append("running = ?")
            values.append(0)
            fields.append("awaiting_review = ?")
            values.append(0)
        elif not payload.completed:
            fields.append("completed_at = ?")
            values.append(None)
    if payload.notes is not None:
        fields.append("notes = ?")
        values.append(json.dumps(normalize_notes(payload.notes, default_ts=_now())))
    if payload.paths is not None:
        fields.append("paths_json = ?")
        values.append(json.dumps(payload.paths))
    if payload.effort_set:
        fields.append("effort = ?")
        values.append(payload.effort)
    if payload.deadline_set:
        fields.append("deadline = ?")
        values.append(payload.deadline)
    if payload.title_status is not None:
        fields.append("title_status = ?")
        values.append(payload.title_status)
    if payload.suggestions_json is not None:
        fields.append("suggestions_json = ?")
        values.append(payload.suggestions_json)
    if payload.suggestions_status is not None:
        fields.append("suggestions_status = ?")
        values.append(payload.suggestions_status)

    if not fields:
        return _todo_to_dict(row)

    now = _now()
    fields.append("updated_at = ?")
    values.append(now)
    values.append(todo_id)
    with db.transaction() as txn:
        txn.execute(
            f"UPDATE todos SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        if structural_change:
            todo_structure.bump_project_structure_revisions(
                txn,
                [int(row["project_id"])],
                updated_at=now,
            )
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    _notify_change()
    return _todo_to_dict(row)


@app.delete("/api/todos/{todo_id}", status_code=204)
async def delete_todo(todo_id: int):
    with db.transaction() as txn:
        row = txn.execute(
            "SELECT project_id FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="todo not found")
        deleted = sync_tombstones.delete_todo_tree(txn, todo_id)
        if deleted == 0:
            raise HTTPException(status_code=404, detail="todo not found")
        todo_structure.bump_project_structure_revisions(
            txn,
            [int(row["project_id"])],
            updated_at=_now(),
        )
    _notify_change()
    return None


@app.post("/api/todos/{todo_id}/retry-title")
async def retry_todo_title(todo_id: int):
    """
    Re-trigger LLM title generation for a todo whose title_status is 'failed'.

    The raw input is recovered from notes[0] (where create_todo stores it when
    raw_input is provided).  Returns 409 if the todo is not in a retryable state.
    """
    if not _title_provider_configured():
        raise HTTPException(status_code=503, detail="No title provider is available")

    conn = db.get_conn()
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="todo not found")

    todo = _todo_to_dict(row)
    if todo["title_status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"todo title_status is '{todo['title_status']}', expected 'failed'",
        )

    notes = todo.get("notes") or []
    raw = note_text(notes[0]).strip() if notes else ""
    if not raw:
        raise HTTPException(
            status_code=409,
            detail="no raw_input preserved in notes[0]; cannot retry",
        )

    conn.execute(
        "UPDATE todos SET title_status=?, updated_at=? WHERE id=?",
        ("pending", _now(), todo_id),
    )
    asyncio.create_task(_generate_title(todo_id, raw))
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    _notify_change()
    return _todo_to_dict(row)


@app.post("/api/todos/reorder")
async def reorder_todos(updates: list[TodoReorderItem]):
    """
    Atomically update project_id, parent_id, and display_order for a set of todos.
    Frontend sends the full card(s) worth of todos after each drag, re-indexed in
    10s gaps.  project_id may differ from the current DB value to move a todo to a
    different project card; all descendants not in the payload are cascade-updated.
    """
    if not updates:
        return {"updated": 0}
    conn = db.get_conn()

    # Validate all ids exist; record their current project.
    ids = [u.id for u in updates]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, project_id FROM todos WHERE id IN ({placeholders})", ids
    ).fetchall()
    found = {r["id"]: r["project_id"] for r in rows}
    if len(found) != len(ids):
        missing = set(ids) - set(found)
        raise HTTPException(status_code=404, detail=f"todo ids not found: {missing}")

    # Resolve each item's intended project_id (payload wins; fall back to current DB).
    intended_project: dict[int, int] = {
        u.id: (u.project_id if u.project_id is not None else found[u.id])
        for u in updates
    }

    # Validate parent-child project consistency.
    for u in updates:
        if u.parent_id is None:
            continue
        child_proj = intended_project[u.id]
        if u.parent_id in intended_project:
            parent_proj = intended_project[u.parent_id]
        else:
            pr = conn.execute(
                "SELECT project_id FROM todos WHERE id = ?", (u.parent_id,)
            ).fetchone()
            if not pr:
                raise HTTPException(
                    status_code=404, detail=f"parent todo {u.parent_id} not found"
                )
            parent_proj = pr["project_id"]
        if parent_proj != child_proj:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"todo {u.id} targets project {child_proj} but its parent "
                    f"{u.parent_id} belongs to project {parent_proj}"
                ),
            )

    now = _now()
    payload_ids = set(ids)
    try:
        todo_structure.validate_no_cycles(
            conn,
            {update.id: update.parent_id for update in updates},
        )
    except todo_structure.StructureValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    affected_project_ids = set(found.values()) | set(intended_project.values())

    with db.transaction() as txn:
        for u in updates:
            txn.execute(
                "UPDATE todos SET project_id = ?, parent_id = ?, display_order = ?, updated_at = ? WHERE id = ?",
                (intended_project[u.id], u.parent_id, u.display_order, now, u.id),
            )
        # Apply explicit for_later for root items FIRST so that child inheritance
        # below reads the already-updated parent value. (Doing this after child
        # inheritance was the bug: children read the stale parent flag.)
        for u in updates:
            if u.parent_id is None and u.for_later is not None:
                txn.execute(
                    "UPDATE todos SET for_later = ? WHERE id = ?",
                    (int(u.for_later), u.id),
                )
        # Children inherit for_later from the (now-updated) root ancestor.
        for u in updates:
            if u.parent_id is not None:
                inherited = _root_for_later(txn, u.parent_id)
                txn.execute(
                    "UPDATE todos SET for_later = ? WHERE id = ?",
                    (int(inherited), u.id),
                )
        # Cascade project_id to any descendants NOT in the payload (safety net:
        # the frontend normally includes all card todos, but guard against gaps).
        _cascade_project_to_orphans(txn, intended_project, found, payload_ids, now)
        # Cascade for_later to any descendants NOT in the payload.
        _cascade_for_later_to_orphans(txn, updates, payload_ids, now)
        todo_structure.bump_project_structure_revisions(
            txn,
            affected_project_ids,
            updated_at=now,
        )

    _notify_change()
    return {"updated": len(updates)}


def _cascade_for_later_to_orphans(
    txn, updates: list, payload_ids: set[int], now: str,
) -> None:
    """BFS: for roots in the payload that have an explicit for_later value,
    cascade that value to any descendants NOT already in the payload.  Mirrors
    _cascade_project_to_orphans so a frontend that sends only the dragged root
    (and not all its children) still produces a consistent DB state."""
    from collections import deque
    queue: deque[tuple[int, int]] = deque()
    for u in updates:
        if u.parent_id is None and u.for_later is not None:
            queue.append((u.id, int(u.for_later)))
    seen = set(payload_ids)
    while queue:
        parent_id, for_later_value = queue.popleft()
        children = txn.execute(
            "SELECT id FROM todos WHERE parent_id = ?", (parent_id,)
        ).fetchall()
        for child in children:
            cid = child["id"]
            if cid not in seen:
                seen.add(cid)
                txn.execute(
                    "UPDATE todos SET for_later = ?, updated_at = ? WHERE id = ?",
                    (for_later_value, now, cid),
                )
                queue.append((cid, for_later_value))


def _cascade_project_to_orphans(
    txn, intended_project: dict[int, int], found: dict[int, int],
    payload_ids: set[int], now: str,
) -> None:
    """BFS: for todos that changed project, cascade to any descendants missing from payload."""
    from collections import deque
    queue: deque[tuple[int, int]] = deque()
    for tid, new_proj in intended_project.items():
        if new_proj != found.get(tid, new_proj):
            queue.append((tid, new_proj))
    seen = set(payload_ids)
    while queue:
        parent_id, new_proj = queue.popleft()
        children = txn.execute(
            "SELECT id FROM todos WHERE parent_id = ?", (parent_id,)
        ).fetchall()
        for child in children:
            cid = child["id"]
            if cid not in seen:
                seen.add(cid)
                txn.execute(
                    "UPDATE todos SET project_id = ?, updated_at = ? WHERE id = ?",
                    (new_proj, now, cid),
                )
                queue.append((cid, new_proj))


@app.post("/api/todos/{todo_id}/complete")
async def complete_todo(todo_id: int):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="todo not found")
    now = _now()
    conn.execute(
        "UPDATE todos SET completed = 1, in_progress = 0, flagged = 0, running = 0, awaiting_review = 0, completed_at = ?, updated_at = ? WHERE id = ?",
        (now, now, todo_id),
    )
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    _notify_change()
    return _todo_to_dict(row)


@app.post("/api/todos/{todo_id}/uncomplete")
async def uncomplete_todo(todo_id: int):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="todo not found")
    now = _now()
    conn.execute(
        "UPDATE todos SET completed = 0, completed_at = NULL, updated_at = ? WHERE id = ?",
        (now, todo_id),
    )
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    _notify_change()
    return _todo_to_dict(row)


# ---------------------------------------------------------------------------
# API: stats (data ready for v2 progress viz)
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def stats(
    since: Optional[str] = Query(default=None),
    tz_offset_minutes: int = Query(default=0),
):
    """
    Return completed-todo effort sums grouped by local calendar day.

    `since` is a local ISO date; defaults to 30 days ago.
    `tz_offset_minutes` is JS Date.getTimezoneOffset() — minutes UTC is ahead
    of local time (e.g. 420 for UTC-7). The backend negates it to shift UTC
    timestamps into the caller's local date before grouping.
    """
    conn = db.get_conn()
    if since is None:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()

    # SQLite modifier to convert UTC stored timestamps → local calendar date.
    # getTimezoneOffset() is positive when local is behind UTC, so negate it.
    offset_modifier = f"{-tz_offset_minutes} minutes"

    rows = conn.execute(
        "SELECT date(completed_at, ?) AS day, project_id, "
        "       SUM(COALESCE(effort, ?)) AS n "
        "FROM todos "
        "WHERE completed = 1 AND completed_at IS NOT NULL "
        "  AND date(completed_at, ?) >= date(?) "
        "GROUP BY day, project_id "
        "ORDER BY day, project_id",
        (offset_modifier, DEFAULT_UNPOINTED_EFFORT, offset_modifier, since),
    ).fetchall()

    by_day: dict[str, dict[str, float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], {})[str(r["project_id"])] = float(r["n"])

    accomplishment_rows = conn.execute(
        "SELECT date(t.completed_at, ?) AS day, t.id AS todo_id, "
        "       t.project_id, p.name AS project_name, t.text, "
        "       COALESCE(t.effort, ?) AS effort "
        "FROM todos t JOIN projects p ON p.id = t.project_id "
        "WHERE t.completed = 1 AND t.completed_at IS NOT NULL "
        "  AND date(t.completed_at, ?) >= date(?) "
        "ORDER BY day DESC, t.completed_at DESC, t.id DESC",
        (offset_modifier, DEFAULT_UNPOINTED_EFFORT, offset_modifier, since),
    ).fetchall()
    accomplishments_by_day: dict[str, list[dict[str, Any]]] = {}
    for row in accomplishment_rows:
        accomplishments_by_day.setdefault(row["day"], []).append(
            {
                "todo_id": row["todo_id"],
                "project_id": row["project_id"],
                "project_name": row["project_name"],
                "text": row["text"],
                "effort": float(row["effort"]),
            }
        )

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM todos "
        "WHERE completed = 1 AND date(completed_at, ?) >= date(?)",
        (offset_modifier, since),
    ).fetchone()["n"]

    return {
        "since": since,
        "total_completed": int(total),
        "by_day": by_day,
        "accomplishments_by_day": accomplishments_by_day,
    }


# ---------------------------------------------------------------------------
# Day overrides (click-to-toggle holiday / workday on heatmap dots)
# ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    daily_goal: Optional[int] = Field(default=None, ge=1, le=50)
    vel_goal: Optional[float] = Field(default=None, ge=0.1, le=20.0)
    retire_hours: Optional[int] = Field(default=None, ge=1, le=720)
    ip_effort_limit: Optional[int] = Field(default=None, ge=1, le=200)


class DayOverrideIn(BaseModel):
    date: str  # YYYY-MM-DD


@app.get("/api/day-overrides")
async def get_day_overrides():
    rows = db.get_conn().execute("SELECT date FROM day_overrides").fetchall()
    return {"dates": [r["date"] for r in rows]}


@app.post("/api/day-overrides/toggle")
async def toggle_day_override(body: DayOverrideIn):
    conn = db.get_conn()
    existing = conn.execute(
        "SELECT 1 FROM day_overrides WHERE date = ?", (body.date,)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM day_overrides WHERE date = ?", (body.date,))
    else:
        conn.execute("INSERT INTO day_overrides (date) VALUES (?)", (body.date,))
    rows = conn.execute("SELECT date FROM day_overrides").fetchall()
    _notify_change()
    return {"dates": [r["date"] for r in rows]}


# ---------------------------------------------------------------------------
# Settings (server-synced across devices; theme stays client-only)
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def search_todos(
    q: str = Query(default=""),
    include_completed: bool = Query(default=False),
):
    """
    Keyword search over todos (matches text and notes fields).

    q is split on whitespace into tokens; a todo matches if ANY token appears
    in its text or notes (OR semantics). Single-token queries behave as before.
    A token of the form "#123" or "123" also matches todo id 123 exactly; such
    exact-id hits sort ahead of other results.

    Returns enriched context per result for use by agents / RAG pipelines:
      - todo:                 full todo object
      - project:              id, name, category
      - ancestors:            chain of parent todos from root → direct parent
      - recently_completed:   up to 10 most recently completed todos in same project
      - sibling_count:        active (non-completed) todos sharing the same parent
      - effort:               todo.effort (may be null)

    By default only active (non-completed) todos are searched.
    Pass include_completed=true to also match completed todos.
    """
    q = q.strip()
    if not q:
        return {"q": q, "results": []}
    tokens = [t for t in q.lower().split() if t]
    if not tokens:
        return {"q": q, "results": []}

    conn = db.get_conn()

    # Non-archived projects
    proj_rows = conn.execute(
        "SELECT * FROM projects WHERE archived = 0 ORDER BY category, display_order, id"
    ).fetchall()
    if not proj_rows:
        return {"q": q, "results": []}
    projects_by_id: dict[int, dict] = {r["id"]: _project_to_dict(r) for r in proj_rows}
    pid_placeholders = ",".join("?" * len(projects_by_id))

    # All todos for those projects (needed for ancestor chain, sibling count, recently_completed)
    all_rows = conn.execute(
        f"SELECT * FROM todos WHERE project_id IN ({pid_placeholders})",
        list(projects_by_id.keys()),
    ).fetchall()
    todos_by_id: dict[int, dict] = {}
    todos_by_project: dict[int, list[dict]] = {}
    for r in all_rows:
        t = _todo_to_dict(r)
        todos_by_id[t["id"]] = t
        todos_by_project.setdefault(t["project_id"], []).append(t)

    id_tokens = {int(m.group(1)) for t in tokens if (m := re.fullmatch(r"#?(\d+)", t))}

    def _todo_matches(todo: dict) -> bool:
        if todo["id"] in id_tokens:
            return True
        text = (todo["text"] or "").lower()
        notes_lower = [note_text(n).lower() for n in (todo.get("notes") or [])]
        return any(
            tok in text or any(tok in n for n in notes_lower)
            for tok in tokens
        )

    results = []
    for todo in todos_by_id.values():
        if not include_completed and todo["completed"]:
            continue
        if not _todo_matches(todo):
            continue

        project = projects_by_id.get(todo["project_id"])
        if project is None:
            continue

        # Ancestor chain: root → direct parent (outermost first)
        ancestors: list[dict] = []
        cur_id = todo.get("parent_id")
        while cur_id is not None:
            anc = todos_by_id.get(cur_id)
            if anc is None:
                break
            ancestors.insert(0, {
                "id": anc["id"],
                "text": anc["text"],
                "completed": anc["completed"],
            })
            cur_id = anc.get("parent_id")

        # Recently completed in same project (last 10 by completed_at DESC)
        proj_todos = todos_by_project.get(todo["project_id"], [])
        recently_completed = sorted(
            [t for t in proj_todos if t["completed"] and t.get("completed_at")],
            key=lambda t: t.get("completed_at") or "",
            reverse=True,
        )[:10]

        # Active sibling count (same parent, not completed, not self)
        parent_id = todo.get("parent_id")
        sibling_count = sum(
            1 for t in proj_todos
            if t["id"] != todo["id"]
            and t.get("parent_id") == parent_id
            and not t["completed"]
        )

        results.append({
            "todo": todo,
            "project": {
                "id": project["id"],
                "name": project["name"],
                "category": project["category"],
            },
            "ancestors": ancestors,
            "recently_completed": [
                {
                    "id": t["id"],
                    "text": t["text"],
                    "completed_at": t.get("completed_at"),
                    "effort": t.get("effort"),
                }
                for t in recently_completed
            ],
            "sibling_count": sibling_count,
            "effort": todo.get("effort"),
        })

    # Exact-id hits first, then active, then completed; otherwise preserve insertion order
    results.sort(key=lambda r: (r["todo"]["id"] not in id_tokens, bool(r["todo"]["completed"])))
    return {"q": q, "results": results}


@app.get("/api/rows")
async def list_board_rows():
    """The board's rows (priority tiers), top to bottom, with project counts."""
    return {"rows": db.list_rows()}


@app.put("/api/rows")
async def apply_board_rows(payload: BoardRowsPlan):
    """Apply a row-editor session (rename, add, reorder, delete + move) atomically."""
    try:
        rows = db.apply_row_plan(
            [item.model_dump() for item in payload.rows],
            [item.model_dump() for item in payload.deletions],
        )
    except db.RowError as exc:
        raise HTTPException(400, str(exc)) from exc
    _notify_change()
    return {"rows": rows}


@app.post("/api/onboarding/complete")
async def complete_onboarding():
    """Dismiss the first-run welcome tour."""
    db.set_onboarding_pending(False)
    _notify_change()
    return db.get_settings()


@app.post("/api/onboarding/restart")
async def restart_onboarding():
    """Show the welcome tour again (Settings → Show welcome tour)."""
    db.set_onboarding_pending(True)
    _notify_change()
    return db.get_settings()


@app.get("/api/settings")
async def get_settings():
    """Return the current server-side settings."""
    return db.get_settings()


@app.patch("/api/settings")
async def patch_settings(payload: SettingsUpdate):
    """Update one or more settings; broadcasts an SSE event so other clients refresh."""
    data = payload.model_dump(exclude_none=True)
    result = db.patch_settings(data)
    _notify_change()
    return result


# ---------------------------------------------------------------------------
# API: AI usage ledger (opt-in, local only)
# ---------------------------------------------------------------------------

_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


class UsageConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    scan_interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)


class UsageEventIn(BaseModel):
    event_key: str = Field(..., min_length=1, max_length=300)
    ts: str = Field(..., min_length=10, max_length=40)
    model: str = Field(..., min_length=1, max_length=200)
    session_id: Optional[str] = Field(default=None, max_length=300)
    provider: Optional[str] = Field(default=None, max_length=100)
    cwd: Optional[str] = Field(default=None, max_length=4096)
    git_branch: Optional[str] = Field(default=None, max_length=500)
    client_version: Optional[str] = Field(default=None, max_length=100)
    speed: Optional[str] = Field(default=None, max_length=50)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    web_search_requests: int = Field(default=0, ge=0)
    reported_cost_usd: Optional[float] = Field(default=None, ge=0)


class UsageSessionIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=300)
    reported_cost_usd: Optional[float] = Field(default=None, ge=0)
    reported_model_usage: dict[str, Any] = Field(default_factory=dict)


class UsageMarkerIn(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=300)
    ts: str = Field(..., min_length=10, max_length=40)
    todo_id: int = Field(..., ge=1)
    kind: str = Field(..., pattern=r"^(running|copy_block|manual)$")


class UsageBatchIn(BaseModel):
    format_version: int = Field(..., ge=1, le=1)
    source: str = Field(..., pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    machine_id: str = Field(..., min_length=1, max_length=100)
    machine_label: Optional[str] = Field(default=None, max_length=200)
    events: list[UsageEventIn] = Field(default_factory=list, max_length=usage_ledger.MAX_EVENTS_PER_BATCH)
    sessions: list[UsageSessionIn] = Field(default_factory=list, max_length=usage_ledger.MAX_EVENTS_PER_BATCH)
    markers: list[UsageMarkerIn] = Field(default_factory=list, max_length=usage_ledger.MAX_EVENTS_PER_BATCH)


@app.get("/api/usage/config")
async def get_usage_config():
    """Report whether local AI usage collection is enabled and the last scan."""
    return usage_ledger.get_config()


@app.patch("/api/usage/config")
async def patch_usage_config(payload: UsageConfigUpdate):
    result = usage_ledger.patch_config(payload.model_dump(exclude_none=True))
    if payload.enabled:
        asyncio.create_task(_run_usage_scan())
    return result


@app.post("/api/usage/scan")
async def scan_usage():
    """Scan this Mac's AI tool logs now (works even while disabled)."""
    return await _run_usage_scan(force=True)


@app.post("/api/usage/events")
async def ingest_usage_events(payload: UsageBatchIn):
    """Ingest one batch in Agenda's open usage-event format (idempotent)."""
    return await asyncio.to_thread(usage_ledger.ingest, payload.model_dump())


@app.get("/api/usage/summary")
async def usage_summary(
    group_by: str = Query(default="day", pattern=r"^(day|model|source|machine|session)$"),
    since: Optional[str] = Query(default=None, pattern=_ISO_DATE),
    until: Optional[str] = Query(default=None, pattern=_ISO_DATE),
    tz_offset_minutes: int = Query(default=0, ge=-900, le=900),
):
    """Token totals and API-equivalent estimates (not billed spend)."""
    return usage_ledger.summary(group_by, since, until, tz_offset_minutes)


@app.get("/api/usage/sessions")
async def usage_sessions(
    since: Optional[str] = Query(default=None, pattern=_ISO_DATE),
    tz_offset_minutes: int = Query(default=0, ge=-900, le=900),
):
    """Per-session totals, the tool's own reported cost, and todo markers."""
    return {"sessions": usage_ledger.sessions(since, tz_offset_minutes)}


class UsageAssignment(BaseModel):
    project_id: Optional[int] = Field(default=None, ge=1)
    todo_id: Optional[int] = Field(default=None, ge=1)


@app.get("/api/usage/by-project")
async def usage_by_project(
    since: Optional[str] = Query(default=None, pattern=_ISO_DATE),
    until: Optional[str] = Query(default=None, pattern=_ISO_DATE),
    tz_offset_minutes: int = Query(default=0, ge=-900, le=900),
):
    """API-equivalent usage per project and local day, with attribution shares."""
    return await asyncio.to_thread(usage_ledger.by_project, since, until, tz_offset_minutes)


@app.get("/api/usage/todos/{todo_id}")
async def usage_for_todo(todo_id: int):
    """All-time usage attributed to a todo and its subtasks."""
    return await asyncio.to_thread(usage_ledger.for_todo, todo_id)


@app.get("/api/usage/unattributed")
async def usage_unattributed(
    since: Optional[str] = Query(default=None, pattern=_ISO_DATE),
    tz_offset_minutes: int = Query(default=0, ge=-900, le=900),
):
    """Sessions no rule could attribute, for manual assignment."""
    return {"sessions": await asyncio.to_thread(
        usage_ledger.unattributed_sessions, since, tz_offset_minutes)}


@app.put("/api/usage/sessions/{source}/{session_id}/assignment")
async def assign_usage_session(source: str, session_id: str, payload: UsageAssignment):
    """Manually attribute a session to a project or todo (overrides markers)."""
    conn = db.get_conn()
    if payload.todo_id is not None:
        if conn.execute("SELECT 1 FROM todos WHERE id = ?", (payload.todo_id,)).fetchone() is None:
            raise HTTPException(404, "todo not found")
    elif payload.project_id is not None:
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (payload.project_id,)).fetchone() is None:
            raise HTTPException(404, "project not found")
    else:
        raise HTTPException(422, "project_id or todo_id is required")
    usage_ledger.assign_session(source, session_id, payload.project_id, payload.todo_id)
    _notify_change()
    return {"ok": True}


@app.delete("/api/usage/sessions/{source}/{session_id}/assignment", status_code=204)
async def clear_usage_assignment(source: str, session_id: str):
    usage_ledger.assign_session(source, session_id, None, None)
    _notify_change()


class UsageBillingIn(BaseModel):
    source: str = Field(..., pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    label: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(default="monthly", pattern=r"^(monthly|one_off)$")
    amount_usd: float = Field(..., ge=0, le=1_000_000)
    start_date: str = Field(..., pattern=_ISO_DATE)
    end_date: Optional[str] = Field(default=None, pattern=_ISO_DATE)

    def to_row(self) -> dict[str, Any]:
        if self.end_date and self.end_date < self.start_date:
            raise HTTPException(422, "end_date is before start_date")
        row = self.model_dump()
        row["amount_micros"] = usage_ledger._usd_to_micros(row.pop("amount_usd"))
        return row


@app.get("/api/usage/billing")
async def list_usage_billing():
    """What the user actually pays for each AI tool."""
    return {"charges": usage_ledger.list_billing()}


@app.post("/api/usage/billing", status_code=201)
async def add_usage_billing(payload: UsageBillingIn):
    result = usage_ledger.add_billing(payload.to_row())
    _notify_change()
    return result


@app.put("/api/usage/billing/{billing_id}")
async def replace_usage_billing(billing_id: int, payload: UsageBillingIn):
    result = usage_ledger.update_billing(billing_id, payload.to_row())
    if result is None:
        raise HTTPException(404, "charge not found")
    _notify_change()
    return result


@app.delete("/api/usage/billing/{billing_id}", status_code=204)
async def delete_usage_billing(billing_id: int):
    if not usage_ledger.delete_billing(billing_id):
        raise HTTPException(404, "charge not found")
    _notify_change()


class QuotaConfigUpdate(BaseModel):
    target_pct: Optional[float] = Field(default=None, gt=0, le=100)
    tolerance_pp: Optional[float] = Field(default=None, ge=0, le=50)


@app.get("/api/quota")
async def quota_state():
    """Usage-limit windows with pace (quota used vs. window time elapsed)."""
    def _read():
        usage_quota.ingest_spool()
        return usage_quota.state()
    return await asyncio.to_thread(_read)


@app.patch("/api/quota/config")
async def patch_quota_config(payload: QuotaConfigUpdate):
    return usage_quota.patch_config(payload.model_dump(exclude_none=True))


@app.get("/api/quota/history")
async def quota_history(
    provider: str = Query(..., pattern=r"^[a-z][a-z0-9_.-]{0,63}$"),
    window: str = Query(..., pattern=r"^[a-z][a-z0-9_]{0,63}$"),
    hours: int = Query(default=24, ge=1, le=24 * 60),
):
    import time as _time
    return {"observations": usage_quota.history(provider, window, _time.time() - hours * 3600)}


@app.get("/api/usage/pricing")
async def usage_pricing():
    """The price table in effect (bundled, or the user's editable copy)."""
    return {"path_kind": "user" if usage_ledger.pricing_path() != usage_ledger.BUNDLED_PRICING else "bundled",
            **usage_ledger.load_pricing()}


# ---------------------------------------------------------------------------
# API: lab-note append endpoints
# ---------------------------------------------------------------------------

class NoteAppend(BaseModel):
    text: str = Field(..., min_length=1)
    author: str = "human"  # "human" or "agent"


@app.post("/api/projects/{project_id}/notes", status_code=201)
async def append_project_note(project_id: int, payload: NoteAppend):
    """Prepend a single timestamped lab-note entry to a project's notes (newest-first)."""
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    now = _now()
    entry = note_entry(payload.text.strip(), payload.author, now)
    try:
        existing = json.loads(row["notes"] or "[]")
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []
    conn.execute(
        "UPDATE projects SET notes = ?, updated_at = ? WHERE id = ?",
        (json.dumps([entry] + existing), now, project_id),
    )
    _notify_change()
    return _project_with_todos(project_id)


@app.post("/api/todos/{todo_id}/notes", status_code=201)
async def append_todo_note(todo_id: int, payload: NoteAppend):
    """Prepend a single timestamped lab-note entry to a todo's notes (newest-first)."""
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="todo not found")
    now = _now()
    entry = note_entry(payload.text.strip(), payload.author, now)
    try:
        existing = json.loads(row["notes"] or "[]")
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []
    conn.execute(
        "UPDATE todos SET notes = ?, updated_at = ? WHERE id = ?",
        (json.dumps([entry] + existing), now, todo_id),
    )
    _notify_change()
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return _todo_to_dict(row)


# ---------------------------------------------------------------------------
# Local AI capabilities and Native Insights
# ---------------------------------------------------------------------------

@app.get("/api/capabilities")
async def capabilities():
    """Report Apple-local and deterministic capabilities."""
    title_provider = (
        "apple-foundation-models"
        if _local_ai_availability.get("status") == "available"
        else "deterministic"
    )
    return {
        "llm_titles": _title_provider_configured(),
        "title_provider": title_provider,
        "local_ai_titles": _local_ai_availability,
        "native_insights": {
            "status": _local_ai_availability.get("status", "unavailable"),
            "provider": _local_ai_availability.get(
                "provider", "deterministic"
            ),
            "fallback": "deterministic",
        },
    }


class InsightRefreshRequest(BaseModel):
    mode: str

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, value):
        if value not in native_insights.MODES:
            raise ValueError(f"mode must be one of {native_insights.MODES}")
        return value


class InsightStateRequest(BaseModel):
    action: str
    hours: int = Field(default=24, ge=1, le=720)

    @field_validator("action")
    @classmethod
    def _check_action(cls, value):
        if value not in {"dismiss", "snooze", "clear"}:
            raise ValueError("action must be dismiss, snooze, or clear")
        return value


def _cached_insights(mode: str, source_hash: str) -> tuple[list[dict], str] | None:
    row = db.get_conn().execute(
        "SELECT * FROM insight_cache WHERE mode = ?",
        (mode,),
    ).fetchone()
    if not row or row["source_hash"] != source_hash:
        return None
    try:
        items = json.loads(row["insights_json"])
        if not isinstance(items, list):
            return None
        now = datetime.now(timezone.utc)
        for item in items:
            expires_at = datetime.fromisoformat(
                str(item["expires_at"]).replace("Z", "+00:00")
            )
            if expires_at <= now:
                return None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return items, row["provider"]


def _visible_insights(items: list[dict]) -> list[dict]:
    if not items:
        return []
    conn = db.get_conn()
    placeholders = ",".join("?" for _ in items)
    rows = conn.execute(
        f"SELECT * FROM insight_states WHERE insight_id IN ({placeholders})",
        [item["id"] for item in items],
    ).fetchall()
    states = {row["insight_id"]: dict(row) for row in rows}
    now = datetime.now(timezone.utc)
    visible = []
    for item in items:
        state = states.get(item["id"])
        if state and state.get("dismissed_at"):
            continue
        if state and state.get("snoozed_until"):
            try:
                until = datetime.fromisoformat(
                    state["snoozed_until"].replace("Z", "+00:00")
                )
                if until > now:
                    continue
            except ValueError:
                pass
        visible.append(item)
    return visible


def _insight_response(
    mode: str,
    context: dict,
    items: list[dict],
    provider: str,
    *,
    needs_refresh: bool,
) -> dict:
    return {
        "mode": mode,
        "insights": _visible_insights(items),
        "provider": provider,
        "needs_refresh": needs_refresh,
        "source_hash": context["source_hash"],
        "candidate_count": len(context["candidates"]),
    }


@app.get("/api/insights")
async def get_native_insights(
    mode: str = Query(default="plan_today"),
):
    if mode not in native_insights.MODES:
        raise HTTPException(status_code=422, detail="Unknown insight mode")
    context = native_insights.build_context(mode, db.get_conn())
    cached = _cached_insights(mode, context["source_hash"])
    if cached:
        items, provider = cached
        return _insight_response(
            mode, context, items, provider, needs_refresh=False
        )
    items = native_insights.materialize_insights(
        context,
        None,
        provider="deterministic",
    )
    return _insight_response(
        mode, context, items, "deterministic", needs_refresh=True
    )


@app.post("/api/insights/refresh")
async def refresh_native_insights(payload: InsightRefreshRequest):
    context = native_insights.build_context(payload.mode, db.get_conn())
    provider = "deterministic"
    items: list[dict]
    if (
        context["candidates"]
        and _local_ai_availability.get("status") == "available"
    ):
        model_context = {
            "mode": context["mode"],
            "generated_for": context["generated_for"],
            "candidates": context["candidates"],
        }
        result = await _invoke_local_ai(
            {
                "operation": "insights",
                "mode": payload.mode,
                "context": json.dumps(
                    model_context,
                    separators=(",", ":"),
                ),
            }
        )
        if result.get("status") == "ok":
            try:
                drafts = result.get("insights")
                if not isinstance(drafts, list):
                    raise ValueError("local helper omitted insight drafts")
                items = native_insights.materialize_insights(
                    context,
                    drafts,
                    provider="apple-foundation-models",
                )
                provider = "apple-foundation-models"
            except ValueError as exc:
                log.warning("local insight output rejected: %s", exc)
                items = native_insights.materialize_insights(
                    context, None, provider="deterministic"
                )
        else:
            log.warning(
                "local insight generation unavailable: %s",
                result.get("reason") or result.get("status"),
            )
            items = native_insights.materialize_insights(
                context, None, provider="deterministic"
            )
    else:
        items = native_insights.materialize_insights(
            context, None, provider="deterministic"
        )

    now = _now()
    db.get_conn().execute(
        """INSERT INTO insight_cache
           (mode, source_hash, provider, insights_json, generated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(mode) DO UPDATE SET
             source_hash=excluded.source_hash,
             provider=excluded.provider,
             insights_json=excluded.insights_json,
             generated_at=excluded.generated_at""",
        (
            payload.mode,
            context["source_hash"],
            provider,
            json.dumps(items),
            now,
        ),
    )
    _notify_change()
    return _insight_response(
        payload.mode, context, items, provider, needs_refresh=False
    )


@app.patch("/api/insights/{insight_id}")
async def update_native_insight_state(
    insight_id: str,
    payload: InsightStateRequest,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if payload.action == "clear":
        db.get_conn().execute(
            "DELETE FROM insight_states WHERE insight_id = ?",
            (insight_id,),
        )
    else:
        dismissed_at = now.isoformat() if payload.action == "dismiss" else None
        snoozed_until = (
            (now + timedelta(hours=payload.hours)).isoformat()
            if payload.action == "snooze"
            else None
        )
        db.get_conn().execute(
            """INSERT INTO insight_states
               (insight_id, dismissed_at, snoozed_until, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(insight_id) DO UPDATE SET
                 dismissed_at=excluded.dismissed_at,
                 snoozed_until=excluded.snoozed_until,
                 updated_at=excluded.updated_at""",
            (insight_id, dismissed_at, snoozed_until, now.isoformat()),
        )
    _notify_change()
    return {"ok": True, "insight_id": insight_id, "action": payload.action}
