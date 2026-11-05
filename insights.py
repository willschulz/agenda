"""Deterministic evidence and contracts for Agenda Native Insights.

The local model is deliberately not trusted to calculate dates, select arbitrary
board records, or invent actions. Python computes a compact candidate set; the
model may only select candidate IDs and rewrite their headline/message.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

MODES = ("plan_today", "stale_work")
TARGET_TYPES = {"board", "project", "todo"}
KINDS = {"priority", "stale", "blocker", "deadline", "review"}
ACTIONS = {"none", "open", "review", "complete", "snooze"}
URGENCIES = {"low", "medium", "high"}
CATEGORY_RANK = {
    "top_of_mind": 0,
    "near_publication": 1,
    "in_development": 2,
    "early_stage": 3,
    "side_project": 4,
    "future_ideas": 5,
}


def _row_rank(project: dict) -> int:
    if "row_rank" in project:
        return int(project["row_rank"])
    return CATEGORY_RANK.get(project["category"], 6)


def _row_label(project: dict) -> str:
    return project.get("row_label") or project["category"].replace("_", " ")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _days_since(raw: str | None, now: datetime) -> int | None:
    parsed = _parse_ts(raw)
    return max(0, (now - parsed).days) if parsed else None


def _candidate(
    *,
    candidate_id: str,
    target_type: str,
    target_id: int,
    kind: str,
    urgency: str,
    headline: str,
    message: str,
    evidence_ids: list[str],
    suggested_action: str = "open",
    score: int = 0,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "target_type": target_type,
        "target_id": target_id,
        "kind": kind,
        "urgency": urgency,
        "headline": headline[:80],
        "message": message[:240],
        "evidence_ids": list(dict.fromkeys(evidence_ids))[:6],
        "suggested_action": suggested_action,
        "score": score,
    }


def _load_rows(conn) -> tuple[list[dict], list[dict]]:
    projects = [
        dict(row)
        for row in conn.execute(
            """SELECT id, name, category, archived
               FROM projects WHERE archived = 0"""
        ).fetchall()
    ]
    # Rank and name by the user's own rows (board_rows), top row = 0.
    try:
        board = conn.execute(
            "SELECT key, label FROM board_rows ORDER BY position, key").fetchall()
    except sqlite3.OperationalError:
        board = []
    rank = {row["key"]: i for i, row in enumerate(board)}
    label = {row["key"]: row["label"] for row in board}
    for project in projects:
        if project["category"] in rank:
            project["row_rank"] = rank[project["category"]]
            project["row_label"] = label[project["category"]]
    todos = [
        dict(row)
        for row in conn.execute(
            """SELECT id, project_id, text, is_blocker, for_later,
                      in_progress, running, awaiting_review, flagged, completed,
                      completed_at, deadline, effort, updated_at
               FROM todos"""
        ).fetchall()
    ]
    return projects, todos


def _plan_candidates(projects: list[dict], todos: list[dict], now: datetime) -> list[dict]:
    project_by_id = {p["id"]: p for p in projects}
    candidates: list[dict] = []
    for todo in todos:
        if todo.get("completed") or todo.get("for_later"):
            continue
        project = project_by_id.get(todo["project_id"])
        if not project:
            continue
        rank = _row_rank(project)
        score = 60 - rank * 8
        kind = "priority"
        urgency = "medium" if rank <= 1 else "low"
        reason = f"{project['name']} is in {_row_label(project)}."
        action = "open"

        deadline = None
        if todo.get("deadline"):
            try:
                deadline = datetime.fromisoformat(todo["deadline"]).date()
            except ValueError:
                deadline = None
        if deadline:
            delta = (deadline - now.date()).days
            if delta < 0:
                kind, urgency, score = "deadline", "high", score + 80
                reason = f"Deadline was {abs(delta)} day(s) ago."
            elif delta <= 2:
                kind, urgency, score = "deadline", "high", score + 65
                reason = f"Deadline is in {delta} day(s)."
            elif delta <= 7:
                kind, urgency, score = "deadline", "medium", score + 35
                reason = f"Deadline is in {delta} days."
        if todo.get("is_blocker"):
            kind, urgency, score = "blocker", "high", score + 70
            reason = "This task is marked as a blocker."
        elif todo.get("awaiting_review"):
            kind, urgency, score, action = "review", "high", score + 60, "review"
            reason = "This task is waiting for human review."
        elif todo.get("in_progress"):
            score += 40
            reason = "This task is already in progress."
        elif todo.get("flagged"):
            urgency, score = "high", score + 45
            reason = "This task is marked urgent."

        candidates.append(
            _candidate(
                candidate_id=f"today-{kind}-todo-{todo['id']}-{todo.get('updated_at', '')[:10]}",
                target_type="todo",
                target_id=todo["id"],
                kind=kind,
                urgency=urgency,
                headline=todo["text"],
                message=f"{reason} Project: {project['name']}.",
                evidence_ids=[f"todo:{todo['id']}", f"project:{project['id']}"],
                suggested_action=action,
                score=score,
            )
        )
    return sorted(candidates, key=lambda item: (-item["score"], item["target_id"]))[:10]


def _stale_candidates(projects: list[dict], todos: list[dict], now: datetime) -> list[dict]:
    project_by_id = {p["id"]: p for p in projects}
    candidates: list[dict] = []
    open_by_project: dict[int, list[dict]] = {}
    latest_done_by_project: dict[int, datetime] = {}
    for todo in todos:
        if todo.get("completed"):
            completed_at = _parse_ts(todo.get("completed_at"))
            if completed_at:
                latest_done_by_project[todo["project_id"]] = max(
                    latest_done_by_project.get(todo["project_id"], completed_at),
                    completed_at,
                )
        elif not todo.get("for_later"):
            open_by_project.setdefault(todo["project_id"], []).append(todo)

    for project_id, open_todos in open_by_project.items():
        project = project_by_id.get(project_id)
        if not project:
            continue
        for todo in open_todos:
            age = _days_since(todo.get("updated_at"), now)
            if age is None:
                continue
            status = None
            threshold = 0
            action = "open"
            if todo.get("running") and age >= 2:
                status, threshold = "running", 2
            elif todo.get("awaiting_review") and age >= 2:
                status, threshold, action = "awaiting review", 2, "review"
            elif todo.get("in_progress") and age >= 7:
                status, threshold = "in progress", 7
            if status and age >= threshold:
                candidates.append(
                    _candidate(
                        candidate_id=f"stale-{status.replace(' ', '-')}-todo-{todo['id']}-{todo.get('updated_at', '')[:10]}",
                        target_type="todo",
                        target_id=todo["id"],
                        kind="review" if action == "review" else "stale",
                        urgency="high" if age >= 14 else "medium",
                        headline=f"{todo['text']} has been {status} for {age} days",
                        message=f"Project: {project['name']}. Last task update was {age} days ago.",
                        evidence_ids=[f"todo:{todo['id']}", f"project:{project_id}"],
                        suggested_action=action,
                        score=100 + age,
                    )
                )

        rank = _row_rank(project)
        latest_done = latest_done_by_project.get(project_id)
        days = (now - latest_done).days if latest_done else None
        if rank <= 2 and (days is None or days >= 14):
            inactivity = f"{days} days" if days is not None else "the recorded history"
            candidates.append(
                _candidate(
                    candidate_id=f"stalled-project-{project_id}-{latest_done.date().isoformat() if latest_done else 'none'}",
                    target_type="project",
                    target_id=project_id,
                    kind="stale",
                    urgency="medium",
                    headline=f"{project['name']} has no recent completion",
                    message=(
                        f"It has {len(open_todos)} open task(s) and no completion in "
                        f"{inactivity}."
                    ),
                    evidence_ids=[
                        f"project:{project_id}",
                        *[f"todo:{item['id']}" for item in open_todos[:3]],
                    ],
                    suggested_action="open",
                    score=80 - rank * 5 + (days or 30),
                )
            )
    return sorted(candidates, key=lambda item: (-item["score"], item["target_id"]))[:10]


def build_context(mode: str, conn, now: datetime | None = None) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"unsupported insight mode: {mode}")
    now = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    projects, todos = _load_rows(conn)
    if mode == "stale_work":
        candidates = _stale_candidates(projects, todos, now)
    else:
        candidates = _plan_candidates(projects, todos, now)
    context = {
        "mode": mode,
        "generated_for": now.date().isoformat(),
        "candidates": candidates,
    }
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"))
    context["source_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    return context


def _insight_id(mode: str, candidate_id: str) -> str:
    digest = hashlib.sha256(f"{mode}:{candidate_id}".encode()).hexdigest()[:16]
    return f"insight-{digest}"


def materialize_insights(
    context: dict[str, Any],
    drafts: list[dict[str, Any]] | None,
    *,
    provider: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = (now or _utc_now()).astimezone(timezone.utc).replace(microsecond=0)
    candidates = {
        item["candidate_id"]: item
        for item in context.get("candidates", [])
    }
    selected: list[tuple[dict[str, Any], str, str]] = []
    if drafts is not None:
        if not isinstance(drafts, list) or len(drafts) > 3:
            raise ValueError("model insights must be an array of at most 3 items")
        seen: set[str] = set()
        for draft in drafts:
            if not isinstance(draft, dict) or set(draft) != {
                "candidate_id", "headline", "message"
            }:
                raise ValueError("model insight has invalid fields")
            candidate_id = draft["candidate_id"]
            candidate = candidates.get(candidate_id)
            headline = draft["headline"]
            message = draft["message"]
            if (
                candidate is None
                or candidate_id in seen
                or not isinstance(headline, str)
                or not headline.strip()
                or len(headline) > 80
                or not isinstance(message, str)
                or not message.strip()
                or len(message) > 240
            ):
                raise ValueError("model insight violates the insight contract")
            seen.add(candidate_id)
            selected.append((candidate, headline.strip(), message.strip()))
    else:
        selected = [
            (candidate, candidate["headline"], candidate["message"])
            for candidate in list(candidates.values())[:3]
        ]

    expires = now + timedelta(hours=24 if context["mode"] == "plan_today" else 72)
    return [
        {
            "id": _insight_id(context["mode"], candidate["candidate_id"]),
            "mode": context["mode"],
            "target_type": candidate["target_type"],
            "target_id": candidate["target_id"],
            "kind": candidate["kind"],
            "urgency": candidate["urgency"],
            "headline": headline,
            "message": message,
            "evidence_ids": candidate["evidence_ids"],
            "suggested_action": candidate["suggested_action"],
            "provider": provider,
            "generated_at": now.isoformat(),
            "expires_at": expires.isoformat(),
        }
        for candidate, headline, message in selected
    ]
