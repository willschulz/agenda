#!/usr/bin/env python3
"""Check semantic parity of portable Agenda pickup/closeout rules."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PICKUP = (
    ROOT
    / "agent-integration"
    / "cursor"
    / ".cursor"
    / "rules"
    / "agenda-todo-pickup.mdc"
)
CLOSEOUT = (
    ROOT
    / "agent-integration"
    / "cursor"
    / ".cursor"
    / "rules"
    / "agenda-todo-closeout.mdc"
)
CLAUDE = ROOT / "agent-integration" / "claude" / "CLAUDE.md"


def require(text: str, phrases: tuple[str, ...], label: str) -> None:
    missing = [phrase for phrase in phrases if phrase not in text]
    if missing:
        raise SystemExit(f"{label} is missing required phrases: {missing}")


for path, label in (
    (PICKUP, "Cursor pickup rule"),
    (CLOSEOUT, "Cursor closeout rule"),
    (CLAUDE, "Claude Code instructions"),
):
    text = path.read_text()
    require(text, ("in_progress", "running", "lab note"), label)

require(PICKUP.read_text(), ("include_completed=true", "explicit user confirmation"), "Cursor pickup rule")
require(
    CLOSEOUT.read_text(),
    (
        "needs-review",
        "for_later: false",
        "top child",
        "sibling immediately below",
        "human-owned",
        "clear the agent's own `running`",
    ),
    "Cursor closeout rule",
)
require(
    CLAUDE.read_text(),
    (
        "include_completed=true",
        "needs-review todo",
        "top child",
        "sibling immediately below",
        "human-owned parking",
    ),
    "Claude Code instructions",
)

print("Portable Agenda rules passed parity checks.")
