# Claude Code setup

1. Keep Agenda running and verify the local API:

   ```bash
   curl -sS --max-time 8 http://127.0.0.1:8552/api/projects >/dev/null
   ```

2. Copy `claude/CLAUDE.md` into the root of the project where Claude Code
   should work with Agenda. If that project already has a `CLAUDE.md`, append
   the Agenda section instead of replacing existing instructions.
3. Start a new Claude Code session and ask it to pick up a named Agenda todo.
4. For a self-contained handoff, use the todo card's **Copy** button and paste
   the resulting prompt into the session.

The instructions also track work you ask for without naming a todo: before the
first change, Claude Code looks for a matching todo, adds to it, or creates a
short one and tells you in one line. It asks when the match or project is
unclear. Questions and read-only investigations don't touch Agenda. To turn
this off, delete the "Work requested without a todo" section from your
`CLAUDE.md`.

Optionally, also register the Agenda MCP server ([`MCP.md`](MCP.md)) so Claude
Code uses Agenda tools instead of `curl`. Keep the `CLAUDE.md` instructions
either way.

## Verification

During a test pickup, confirm that Claude Code:

- reads northstars and recent notes before proposing work;
- asks for confirmation before editing;
- sets `running`, not `in_progress`, when work begins;
- appends a concise lab note at closeout;
- asks before creating follow-ups or completing the todo;
- clears `running` when the session ends; and
- for a request that names no todo, finds or creates one before the first
  change and says which.
