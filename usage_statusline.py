"""
Agenda -- Claude Code status-line bridge for usage-limit pacing.

Claude Code runs this as its `statusLine` command and pipes session JSON to
stdin. The bridge keeps only `rate_limits` (5-hour and 7-day subscription
windows), writes a timestamped snapshot for Agenda, and prints a one-line
status. It makes no network calls, so it can never slow Claude Code down.

Set up from Agenda's Settings ("Connect Claude Code"), which writes the
`statusLine` entry in ~/.claude/settings.json (claude_statusline_setup.py).
If the user already had a status line, the bridge runs that command with the
same input and prints its output instead of its own, so connecting Agenda
never takes it away.

Snapshots: <Agenda data dir>/quota/claude_statusline-<session>.json, one per
session so concurrent sessions never overwrite each other. Missing windows
stay missing (unknown), never zero.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

WINDOWS = ("five_hour", "seven_day")
LABELS = {"five_hour": "5h", "seven_day": "7d"}
DURATIONS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
HEARTBEAT_SECONDS = 30  # rewrite unchanged data at most this often
DEFAULT_DATA_DIR = os.path.expanduser("~/Library/Application Support/Agenda")
# The user's own statusLine setting from before Agenda connected, if any.
PREVIOUS_FILE = "claude_statusline_previous.json"
PREVIOUS_TIMEOUT = 5  # seconds; Claude Code cancels slow status lines anyway


def data_dir() -> str:
    return os.environ.get("AGENDA_DATA_DIR") or DEFAULT_DATA_DIR


def previous_command() -> str:
    try:
        with open(os.path.join(data_dir(), PREVIOUS_FILE), encoding="utf-8") as fh:
            prev = (json.load(fh) or {}).get("statusLine") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    cmd = prev.get("command") if isinstance(prev, dict) else None
    # Never call ourselves.
    return cmd if isinstance(cmd, str) and "usage_statusline.py" not in cmd else ""


def run_previous(command: str, raw: str) -> str | None:
    """The user's own status line output, or None if it failed."""
    try:
        done = subprocess.run(command, shell=True, input=raw, capture_output=True,
                              text=True, timeout=PREVIOUS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.rstrip("\n") if done.returncode == 0 else None


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
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    windows = extract(payload) if isinstance(payload, dict) else {}
    if windows:
        try:
            write_snapshot(str(payload.get("session_id") or ""), windows, now)
        except OSError:
            pass  # the status line must still render
    previous = previous_command()
    line = run_previous(previous, raw) if previous else None
    if line is None and isinstance(payload, dict):
        line = render(payload, windows, now)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
