"""
Agenda -- Cursor usage-limit fetcher.

Cursor has no status line to tap, so Agenda polls the endpoint behind
cursor.com's Spending page and writes a snapshot that usage_quota ingests
like any other. The endpoint is undocumented; if it changes or rejects the
session, nothing is written and the dials go stale, never wrong.

Credential: the Cursor desktop app's own session token, read from its
state.vscdb (opened read-only) on every poll, so Cursor's refreshes are
picked up. It is kept in memory only and never logged or written anywhere.

Windows (one billing cycle each):
- cursor_models: autoPercentUsed ("Cursor Models", Cursor Grok + Composer)
- other_models:  apiPercentUsed  ("Other Models")
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import usage_quota

log = logging.getLogger("agenda.usage_cursor")

STATE_DB = os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
SUMMARY_URL = "https://cursor.com/api/usage-summary"
POLL_SECONDS = 10 * 60
SOURCE = "cursor_dashboard"
FIELDS = {"cursor_models": "autoPercentUsed", "other_models": "apiPercentUsed"}


def read_token(path: str = STATE_DB) -> str | None:
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'").fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def session_cookie(token: str) -> str:
    """The dashboard's cookie: <user id>::<access token>, URL-encoded."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    sub = str(json.loads(base64.urlsafe_b64decode(payload))["sub"])
    return f"WorkosCursorSessionToken={sub.split('|')[-1]}%3A%3A{token}"


def fetch_summary(token: str) -> dict[str, Any]:
    req = urllib.request.Request(SUMMARY_URL, headers={
        "Accept": "application/json", "User-Agent": "Agenda",
        "Origin": "https://cursor.com", "Cookie": session_cookie(token),
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _epoch(iso: Any) -> int | None:
    if not isinstance(iso, str):
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def windows_from_summary(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return {window: {used_percentage, starts_at, resets_at}}; missing stays missing."""
    start, end = _epoch(summary.get("billingCycleStart")), _epoch(summary.get("billingCycleEnd"))
    plan = (summary.get("individualUsage") or {}).get("plan") or {}
    if start is None or end is None or end <= start:
        return {}
    out = {}
    for window, field in FIELDS.items():
        used = plan.get(field)
        if isinstance(used, (int, float)):
            out[window] = {"used_percentage": float(used), "starts_at": start, "resets_at": end}
    return out


def write_snapshot(windows: dict, now: float, folder: str | None = None) -> str:
    folder = folder or usage_quota.spool_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{SOURCE}.json")  # matches usage_quota's cursor_*.json
    changed_at = now
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
        if prev.get("windows") == windows:
            changed_at = float(prev.get("changed_at") or now)
    except (OSError, ValueError):
        pass
    snap = {"provider": "cursor", "source": SOURCE, "session_id": "",
            "windows": windows, "changed_at": changed_at, "seen_at": now}
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".cursor-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(snap, fh)
    os.replace(tmp, path)
    return path


def poll_once(now: float | None = None) -> str:
    """Fetch and spool one reading. Returns a short status word for logs."""
    token = read_token()
    if not token:
        return "no-cursor-session"
    try:
        summary = fetch_summary(token)
    except urllib.error.HTTPError as e:
        log.warning("Cursor usage fetch failed: HTTP %s", e.code)
        return f"http-{e.code}"
    except (urllib.error.URLError, OSError) as e:
        log.info("Cursor usage fetch unavailable: %s", type(e).__name__)
        return "network"
    windows = windows_from_summary(summary)
    if not windows:
        log.warning("Cursor usage response had no usable windows")
        return "unparsed"
    write_snapshot(windows, now or time.time())
    return "ok"
