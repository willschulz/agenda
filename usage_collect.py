"""
Agenda -- local AI usage collector (standard library only).

Reads usage records that AI coding tools already write on this machine and
turns them into events in Agenda's open usage format (see
agent-integration/docs/USAGE_EVENTS.md). No prompt or response text leaves
the parser: events carry ids, timestamps, model names, working directory,
and token counts; markers carry only an Agenda todo id.

The same module runs inside Agenda's embedded server (periodic scan of the
local machine) and standalone on other machines, posting to an Agenda API:

    python3 usage_collect.py --post http://127.0.0.1:8552
    python3 usage_collect.py --dry-run          # print a summary, send nothing

Adapters:
- claude_code: ~/.claude/projects/**/*.jsonl (or $CLAUDE_CONFIG_DIR/projects)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import socket
import sys
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterator

FORMAT_VERSION = 1
SOURCE_CLAUDE_CODE = "claude_code"

# Strong attribution markers only. A todo id that merely appears in a search
# result or a sibling listing is not a marker.
#   - an agent setting `running: true` on a todo via the local API
#   - a structured todo context block pasted into the session (`todo_id: N`)
_RUNNING_PATCH = re.compile(
    r"/api/todos/(\d+)\b(?=[^\n]{0,400}?[\"']?running[\"']?\s*:\s*true)"
    r"|[\"']?running[\"']?\s*:\s*true[^\n]{0,400}?/api/todos/(\d+)\b"
)
_COPY_BLOCK_TODO = re.compile(r"^\s*[\"']?todo_id[\"']?\s*[:=]\s*(\d+)\b", re.M)


# ---------------------------------------------------------------------------
# Incremental file reading
# ---------------------------------------------------------------------------

def _read_new_lines(path: str, state: dict[str, Any]) -> Iterator[str]:
    """Yield complete lines appended to `path` since the last scan.

    `state` maps path -> {"inode", "offset"}. A changed inode or a file that
    shrank is re-read from the start; ingest is idempotent, so re-reading is
    only wasted work, never double counting. A trailing partial line is left
    for the next scan.
    """
    try:
        st = os.stat(path)
    except OSError:
        return
    prev = state.get(path) or {}
    offset = prev.get("offset", 0)
    if prev.get("inode") != st.st_ino or st.st_size < offset:
        offset = 0
    if st.st_size == offset:
        state[path] = {"inode": st.st_ino, "offset": offset}
        return
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    end = data.rfind(b"\n")
    if end < 0:
        state[path] = {"inode": st.st_ino, "offset": offset}
        return
    state[path] = {"inode": st.st_ino, "offset": offset + end + 1}
    for raw in data[: end + 1].splitlines():
        if raw.strip():
            yield raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Claude Code adapter
# ---------------------------------------------------------------------------

def claude_code_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "projects"


def _user_texts(content: Any) -> Iterator[str]:
    """Text the human typed or pasted (not tool results)."""
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if isinstance(block.get("text"), str):
                    yield block["text"]


def _shell_commands(content: Any) -> Iterator[str]:
    """Shell commands the agent actually ran (tool inputs with a `command`).

    File contents an agent writes (tests, docs) can contain the same text as
    a real API call, so only executed commands count.
    """
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            command = (block.get("input") or {}).get("command")
            if isinstance(command, str):
                yield command


def _markers_from(content: Any, role: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    if role == "assistant":
        for command in _shell_commands(content):
            for m in _RUNNING_PATCH.finditer(command):
                found.append((int(m.group(1) or m.group(2)), "running"))
    elif role == "user":
        for text in _user_texts(content):
            for m in _COPY_BLOCK_TODO.finditer(text):
                found.append((int(m.group(1)), "copy_block"))
    return found


def _usage_event(rec: dict[str, Any]) -> dict[str, Any] | None:
    msg = rec.get("message") or {}
    usage = msg.get("usage") or {}
    model = msg.get("model")
    message_id = msg.get("id")
    if not usage or not model or model == "<synthetic>" or not message_id:
        return None
    cache = usage.get("cache_creation") or {}
    write_5m = cache.get("ephemeral_5m_input_tokens")
    write_1h = cache.get("ephemeral_1h_input_tokens")
    if write_5m is None and write_1h is None:
        # Older records only report the total; Claude's default TTL is 5m.
        write_5m, write_1h = usage.get("cache_creation_input_tokens") or 0, 0
    tools = usage.get("server_tool_use") or {}
    details = usage.get("output_tokens_details") or {}
    return {
        "event_key": f"{message_id}:{rec.get('requestId') or ''}",
        "ts": rec.get("timestamp"),
        "session_id": rec.get("sessionId") or rec.get("session_id"),
        "model": model,
        "provider": "anthropic",
        "cwd": rec.get("cwd"),
        "git_branch": rec.get("gitBranch"),
        "client_version": rec.get("version"),
        "speed": usage.get("speed"),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write_5m_tokens": int(write_5m or 0),
        "cache_write_1h_tokens": int(write_1h or 0),
        "reasoning_tokens": int(details.get("thinking_tokens") or 0),
        "web_search_requests": int(tools.get("web_search_requests") or 0),
    }


def collect_claude_code(
    state: dict[str, Any], root: Path | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Scan Claude Code transcripts incrementally.

    Returns {"events", "sessions", "markers"}, each a list of open-format
    records with source "claude_code". Content blocks of one response repeat
    the same usage; the event_key collapses them.
    """
    root = root or claude_code_root()
    files_state = state.setdefault(SOURCE_CLAUDE_CODE, {})
    events: dict[str, dict[str, Any]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    markers: dict[tuple, dict[str, Any]] = {}
    for path in sorted(glob.glob(str(root / "**" / "*.jsonl"), recursive=True)):
        for line in _read_new_lines(path, files_state):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            sid = rec.get("sessionId") or rec.get("session_id")
            if kind == "cost-state" and sid:
                model_usage = rec.get("modelUsage") or {}
                sessions[sid] = {
                    "session_id": sid,
                    "reported_cost_usd": rec.get("totalCostUSD"),
                    "reported_model_usage": {
                        m: {
                            "input_tokens": u.get("inputTokens", 0),
                            "output_tokens": u.get("outputTokens", 0),
                            "cache_read_tokens": u.get("cacheReadInputTokens", 0),
                            "cache_write_tokens": u.get("cacheCreationInputTokens", 0),
                            "cost_usd": u.get("costUSD"),
                        }
                        for m, u in model_usage.items()
                        if isinstance(u, dict)
                    },
                }
                continue
            if kind not in ("assistant", "user") or not sid:
                continue
            msg = rec.get("message") or {}
            for todo_id, marker_kind in _markers_from(msg.get("content"), kind):
                key = (sid, rec.get("timestamp"), todo_id, marker_kind)
                markers[key] = {
                    "session_id": sid,
                    "ts": rec.get("timestamp"),
                    "todo_id": todo_id,
                    "kind": marker_kind,
                }
            if kind == "assistant":
                ev = _usage_event(rec)
                if ev and ev["ts"] and ev["session_id"]:
                    events[ev["event_key"]] = ev
    return {
        "events": list(events.values()),
        "sessions": list(sessions.values()),
        "markers": list(markers.values()),
    }


# ---------------------------------------------------------------------------
# Batch assembly + CLI
# ---------------------------------------------------------------------------

def build_batch(machine_id: str, machine_label: str, source: str, found: dict) -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "machine_id": machine_id,
        "machine_label": machine_label,
        "source": source,
        **found,
    }


def _state_path() -> Path:
    base = os.environ.get("AGENDA_USAGE_STATE") or os.path.expanduser(
        "~/.config/agenda-usage/state.json"
    )
    return Path(base)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def _post(url: str, batch: dict, chunk: int = 2000) -> dict[str, int]:
    """POST the batch in chunks; return summed counts from the server."""
    totals: dict[str, int] = {}
    events = batch["events"]
    parts = [events[i : i + chunk] for i in range(0, len(events), chunk)] or [[]]
    for i, part in enumerate(parts):
        body = dict(batch, events=part)
        if i:  # sessions/markers ride along with the first chunk only
            body["sessions"], body["markers"] = [], []
        req = urllib.request.Request(
            url.rstrip("/") + "/api/usage/events",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            for k, v in json.load(resp).items():
                if isinstance(v, int):
                    totals[k] = totals.get(k, 0) + v
    return totals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--post", metavar="AGENDA_URL", help="Agenda base URL to send events to")
    ap.add_argument("--dry-run", action="store_true", help="scan and summarize; send nothing")
    ap.add_argument("--rescan", action="store_true", help="ignore saved offsets and read everything")
    args = ap.parse_args(argv)
    if not args.post and not args.dry_run:
        ap.error("pass --post AGENDA_URL or --dry-run")

    state_file = _state_path()
    state = {} if args.rescan else _load_state(state_file)
    machine_id = state.setdefault("machine_id", str(uuid.uuid4()))
    found = collect_claude_code(state)
    batch = build_batch(machine_id, socket.gethostname(), SOURCE_CLAUDE_CODE, found)
    counts = {k: len(batch[k]) for k in ("events", "sessions", "markers")}
    if args.dry_run:
        print(json.dumps({"found": counts}))
        return 0
    result = _post(args.post, batch)
    _save_state(state_file, state)  # only advance offsets after a successful send
    print(json.dumps({"found": counts, "server": result}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
