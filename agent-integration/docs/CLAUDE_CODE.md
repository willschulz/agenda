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

## Verification

During a test pickup, confirm that Claude Code:

- reads northstars and recent notes before proposing work;
- asks for confirmation before editing;
- sets `running`, not `in_progress`, when work begins;
- appends a concise lab note at closeout;
- asks before creating follow-ups or completing the todo; and
- clears `running` when the session ends.
