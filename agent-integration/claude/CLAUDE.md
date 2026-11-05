# Agenda integration

Use these instructions only when the user identifies an Agenda todo (for
example by Task number and title) or pastes a structured block that already
contains `todo_id` / `project_id`. Agenda is local at
`http://127.0.0.1:8552`; do not query it for unrelated work.

## Pickup

1. If a pasted prompt contains `todo_id`, `project_id`, notes, paths, and phase
   instructions, use that context directly. Otherwise resolve the todo with:

   ```bash
   curl -sS --max-time 8 http://127.0.0.1:8552/api/projects
   ```

   If unavailable, ask the user to launch Agenda and retry once.

2. Read project northstars, newest project notes, project paths, newest todo
   notes, then todo paths. Read only the small set needed for the task.
3. When siblings or prior work may matter, search 2–4 key nouns with
   `/api/search?q=<terms>&include_completed=true`.
4. Summarize the task and relevant context, then propose a short plan or a
   2–5-item breakdown. Wait for explicit confirmation before changing files or
   external state.
5. Immediately before approved substantive work, set the todo's `running`
   field to true. `in_progress` is human-owned; never set or clear it.

## Closeout

1. Always append a concise lab note first. Use the todo note endpoint when
   leaving it open; use the project note endpoint for a durable rollup when
   completing it.
2. If agent-built work was not reviewed live, leave the original todo open and
   propose a needs-review todo with `awaiting_review: true` and
   `for_later: false` as the top child of the original todo. If the original is
   parked in `Later…`, put review at the top of the project main list instead.
   Include concrete verification steps and paths to the artifacts.
3. Propose five approval-gated buckets: follow-up todos, completion,
   project-context updates, reprioritization, and documentation updates.
4. After approval, create todos first, then reorder, update project context,
   update documentation, and complete the current todo last.
5. Always clear the agent-owned `running` flag when the session ends.

Use short todo titles (at most eight words) and put detail in notes. Never
replace notes arrays; use the append endpoints. Never alter `in_progress`.
Express follow-up through hierarchy/order: blocking work is a child; genuine
subsequent work is the sibling immediately below its antecedent. `for_later`
is human-owned parking and requires explicit per-item approval.

See `docs/API.md` in the Agenda integration pack for endpoint examples.
