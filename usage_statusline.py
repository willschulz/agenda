"""
Agenda -- Claude Code status-line bridge for usage-limit pacing.

Claude Code runs this as its `statusLine` command and pipes session JSON to
stdin. The bridge keeps only `rate_limits` (5-hour and 7-day subscription
windows), writes a timestamped snapshot for Agenda, and prints a one-line
status. It makes no network calls, so it can never slow Claude Code down.

Configure in ~/.claude/settings.json:

    "statusLine": {
      "type": "command",
      "command": "<Agenda.app>/Contents/Resources/python/bin/python3 -I -B <Agenda.app>/Contents/Resources/server/usage_statusline.py"
    }

Snapshots: <Agenda data dir>/quota/claude_statusline-<session>.json, one per
session so concurrent sessions never overwrite each other. Missing windows
stay missing (unknown), never zero.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time

WINDOWS = ("five_hour", "seven_day")
LABELS = {"five_hour": "5h", "seven_day": "7d"}
DURATIONS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
HEARTBEAT_SECONDS = 30  # rewrite unchanged data at most this often


def data_dir() -> str:
    return os.environ.get("AGENDA_DATA_DIR") or os.path.expanduser(
        "~/Library/Application Support/Agenda"
    )


def extract(payload: dict) -> dict:
    """Return {window: {used_percentage, resets_at}} for windows present."""
    limits = payload.get("rate_limits")
    out = {}
    if not isinstance(limits, dict):
        return out
    for name in WINDOWS:
        w = limits.get(name)
        if not isinstance(w, dict):
            continue
        used, resets = w.get("used_percentage"), w.get("resets_at")
        if isinstance(used, (int, float)) and isinstance(resets, (int, float)):
            out[name] = {"used_percentage": float(used), "resets_at": int(resets)}
    return out


def write_snapshot(session_id: str, windows: dict, now: float) -> None:
    folder = os.path.join(data_dir(), "quota")
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "unknown")[:80] or "unknown"
    path = os.path.join(folder, f"claude_statusline-{safe}.json")
    prev = None
    try:
        with open(path, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        pass
    if prev and prev.get("windows") == windows and now - prev.get("seen_at", 0) < HEARTBEAT_SECONDS:
        return
    changed_at = prev.get("changed_at") if prev and prev.get("windows") == windows else now
    snap = {
        "format_version": 1,
        "provider": "claude",
        "source": "claude_statusline",
        "session_id": session_id,
        "windows": windows,
        "changed_at": changed_at,  # when these values were first seen
        "seen_at": now,            # last time Claude Code reported them
    }
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(snap, fh)
    os.replace(tmp, path)


def pace_word(name: str, w: dict, now: float) -> str:
    duration = DURATIONS[name]
    remaining = w["resets_at"] - now
    if remaining <= 0:
        return ""
    elapsed = max(0.0, min(100.0, 100.0 * (duration - remaining) / duration))
    delta = w["used_percentage"] - elapsed
    return "▲" if delta > 5 else ("▼" if delta < -5 else "=")


def render(payload: dict, windows: dict, now: float) -> str:
    model = ((payload.get("model") or {}).get("display_name")) or ""
    parts = [model] if model else []
    for name in WINDOWS:
        w = windows.get(name)
        if w and w["resets_at"] > now:
            parts.append(f"{LABELS[name]} {w['used_percentage']:.0f}%{pace_word(name, w, now)}")
    return " · ".join(parts)


def main() -> int:
    now = time.time()
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    windows = extract(payload)
    if windows:
        try:
            write_snapshot(str(payload.get("session_id") or ""), windows, now)
        except OSError:
            pass  # the status line must still render
    print(render(payload, windows, now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
