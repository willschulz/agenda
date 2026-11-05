# Local API quick reference

Agenda serves its API at `http://127.0.0.1:8552` while the app is running.
Use an eight-second timeout for reads so a closed app fails quickly.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/projects` | Read projects and todos |
| `GET` | `/api/search?q=<terms>&include_completed=true` | Find related work |
| `PATCH` | `/api/todos/<id>` | Update `running` or ordinary todo fields |
| `POST` | `/api/todos/<id>/notes` | Append a todo lab note |
| `POST` | `/api/projects/<id>/notes` | Append a project lab-note rollup |
| `POST` | `/api/todos` | Create a todo or review handoff |
| `POST` | `/api/todos/reorder` | Reorder or reparent todos |
| `PATCH` | `/api/projects/<id>` | Update project northstars or paths |
| `POST` | `/api/todos/<id>/complete` | Complete a todo |
| `POST` | `/api/todos/<id>/uncomplete` | Reopen a todo |
| `POST` | `/api/usage/events` | Submit AI usage (see [`USAGE_EVENTS.md`](USAGE_EVENTS.md)) |
| `GET` | `/api/rows` | Read the board's rows (priority tiers), top to bottom |

Todo placement fields:

- `parent_id` and `display_order` express relationships to antecedent work.
- `for_later: true` parks a root and its descendants in `Later…`. It is a
  human-owned choice, not a synonym for follow-up.
- `is_followup` is accepted temporarily as a deprecated input alias for
  `for_later`; new integrations must use `for_later`.

Rows are user-defined. A project's `category` is a row `key` from
`GET /api/rows` (or the `rows` list in `GET /api/projects`). Show users the
row's `label`, never the key. Agents shouldn't rename, add, or delete rows;
people do that in **Settings → Edit Rows**.

## Health check

```bash
curl -sS --max-time 8 http://127.0.0.1:8552/api/projects >/dev/null
```

If this fails, launch Agenda and retry after the board appears.

## Status ownership

- `in_progress` is human-owned. Agents must never set or clear it.
- `running` is agent-owned. Set it immediately before substantive work and
  clear it whenever the agent session ends.
- Completing a todo clears all status flags automatically.

## Notes

Append notes; never replace the existing notes array:

```bash
curl -sS -X POST -H 'Content-Type: application/json' \
  -d '{"text":"A concise 2–4 sentence update.","author":"agent"}' \
  http://127.0.0.1:8552/api/todos/<id>/notes
```
