"""
Agenda -- connect Claude Code's status line to the usage-limit dials.

Claude Code reports subscription usage limits (5-hour and weekly) only to its
`statusLine` command. Connecting sets that command in ~/.claude/settings.json
to the usage_statusline.py bridge inside this Agenda install. Nothing else in
the file changes.

A status line the user already had is kept: its setting is saved in Agenda's
data folder, the bridge still runs it and prints its output, and
disconnecting puts the setting back exactly as it was.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import db
import usage_statusline

BRIDGE_NAME = "usage_statusline.py"
BRIDGE = Path(__file__).resolve().parent / BRIDGE_NAME


class SetupError(Exception):
    """A problem the user has to fix; the message is shown in Settings."""


def claude_dir() -> Path:
    return Path(os.environ.get("AGENDA_CLAUDE_DIR") or Path.home() / ".claude")


def settings_path() -> Path:
    return claude_dir() / "settings.json"


def data_dir() -> Path:
    return db.DB_PATH.parent


def previous_path() -> Path:
    return data_dir() / usage_statusline.PREVIOUS_FILE


def bridge_command() -> str:
    """The statusLine command for this install."""
    cmd = f"{shlex.quote(sys.executable)} -I -B {shlex.quote(str(BRIDGE))}"
    # The bridge looks for Agenda's data folder at its default path; any
    # other folder (development servers) is passed on the command line.
    if os.path.realpath(data_dir()) != os.path.realpath(usage_statusline.DEFAULT_DATA_DIR):
        cmd = f"AGENDA_DATA_DIR={shlex.quote(str(data_dir()))} {cmd}"
    return cmd


def _command_of(status_line: Any) -> str:
    if isinstance(status_line, dict) and isinstance(status_line.get("command"), str):
        return status_line["command"]
    return ""


def _is_agenda_bridge(command: str) -> bool:
    return BRIDGE_NAME in command


def _bridge_script(command: str) -> str | None:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return None
    return next((p for p in parts if p.endswith(BRIDGE_NAME)), None)


def _runs_this_bridge(command: str) -> bool:
    """Exact match, or a hand-written command that runs this install's bridge."""
    if command == bridge_command():
        return True
    script = _bridge_script(command)
    return bool(script) and os.path.realpath(script) == os.path.realpath(BRIDGE)


def _read_settings() -> dict[str, Any]:
    path = settings_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise SetupError(f"Couldn't read {path}: {e.strerror}") from e
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SetupError(f"{path} isn't valid JSON, so Agenda won't change it. Fix it and try again.") from e
    if not isinstance(data, dict):
        raise SetupError(f"{path} isn't a JSON object, so Agenda won't change it.")
    return data


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(tmp)
        raise


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _last_reading_at(conn=None) -> float | None:
    conn = conn or db.get_conn()
    try:
        return conn.execute(
            "SELECT MAX(seen_at) FROM quota_observations WHERE provider = 'claude'").fetchone()[0]
    except Exception:
        return None


def status(conn=None) -> dict[str, Any]:
    """Where Claude Code's status line points, for the Settings control.

    state: not_installed | not_connected | other (someone else's status
    line) | connected | moved (an Agenda bridge at another path) | broken
    (an Agenda bridge whose script no longer exists) | error.
    """
    out: dict[str, Any] = {
        "settings_path": str(settings_path()),
        "bridge_command": bridge_command(),
        "last_reading_at": _last_reading_at(conn),
        "keeps_previous": False,
    }
    if not claude_dir().is_dir():
        return {**out, "state": "not_installed"}
    try:
        settings = _read_settings()
    except SetupError as e:
        return {**out, "state": "error", "error": str(e)}
    command = _command_of(settings.get("statusLine"))
    out["current_command"] = command or None
    if not command:
        return {**out, "state": "not_connected"}
    if not _is_agenda_bridge(command):
        return {**out, "state": "other"}
    out["keeps_previous"] = bool(_saved_previous_command())
    if _runs_this_bridge(command):
        return {**out, "state": "connected"}
    script = _bridge_script(command)
    if script and not os.path.exists(script):
        return {**out, "state": "broken"}
    return {**out, "state": "moved"}


def _saved_previous() -> tuple[bool, Any]:
    """(saved?, the statusLine value from before Agenda connected, or None)."""
    try:
        with open(previous_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False, None
    if not isinstance(data, dict):
        return False, None
    return True, data.get("statusLine")


def _saved_previous_command() -> str:
    return _command_of(_saved_previous()[1])


def connect(conn=None) -> dict[str, Any]:
    if not claude_dir().is_dir():
        raise SetupError("Claude Code isn't set up on this Mac (no ~/.claude folder). Install it and sign in first.")
    settings = _read_settings()
    current = settings.get("statusLine")
    command = _command_of(current)
    if _runs_this_bridge(command):
        return status(conn)
    if not _is_agenda_bridge(command):
        # Remember the user's own status line (or that there was none).
        data_dir().mkdir(parents=True, exist_ok=True)
        _write_json(previous_path(), {"statusLine": current})
        if settings_path().exists():
            shutil.copy2(settings_path(), data_dir() / "claude-settings-before-agenda.json")
    new: dict[str, Any] = {"type": "command", "command": bridge_command()}
    if isinstance(current, dict) and "padding" in current:
        new["padding"] = current["padding"]
    settings["statusLine"] = new
    _write_json(settings_path(), settings)
    return status(conn)


def disconnect(conn=None) -> dict[str, Any]:
    settings = _read_settings()
    if _is_agenda_bridge(_command_of(settings.get("statusLine"))):
        saved, previous = _saved_previous()
        if saved and previous is not None and not _is_agenda_bridge(_command_of(previous)):
            settings["statusLine"] = previous
        else:
            settings.pop("statusLine", None)
        _write_json(settings_path(), settings)
    _unlink_quietly(str(previous_path()))
    return status(conn)


def repair_if_broken() -> bool:
    """At startup: if the status line points at an Agenda bridge that no
    longer exists (the app was moved or reinstalled elsewhere), point it at
    this install. The user already connected, so this keeps their choice."""
    try:
        if status().get("state") != "broken":
            return False
        connect()
        return True
    except (SetupError, OSError):
        return False
