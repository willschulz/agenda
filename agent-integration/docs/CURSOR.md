# Cursor setup

1. Keep Agenda running and verify the local API:

   ```bash
   curl -sS --max-time 8 http://127.0.0.1:8552/api/projects >/dev/null
   ```

2. Copy the two files from `cursor/.cursor/rules/` into the `.cursor/rules/`
   directory of the workspace where Cursor should work with Agenda.
3. Start a new Cursor chat and ask it to pick up a named Agenda todo.
4. For the most reliable handoff, use the todo card's **Copy** button and paste
   the resulting prompt into the chat.

The rules activate only for requests that refer to Agenda todos. Ordinary code
questions and unrelated work do not query Agenda.

## Verification

During a test pickup, confirm that Cursor:

- reads northstars and recent notes before proposing work;
- asks for confirmation before editing;
- sets `running`, not `in_progress`, when work begins;
- appends a concise lab note at closeout;
- asks before creating follow-ups or completing the todo; and
- clears `running` when the session ends.
