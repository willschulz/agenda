# Agenda agent integration

Agenda is a local-first project tracker. While Agenda is running, agents can
read and update the user's board through:

```text
http://127.0.0.1:8552
```

No Agenda account, cloud backend, or API key is required.

## Choose an integration

- **Cursor:** follow [`docs/CURSOR.md`](docs/CURSOR.md).
- **Claude Code:** follow [`docs/CLAUDE_CODE.md`](docs/CLAUDE_CODE.md).
- **Any agent:** a todo's **Copy** button produces a self-contained pickup
  prompt that can be pasted into an agent conversation.

The portable workflow keeps ownership boundaries explicit:

- Humans own `in_progress`; agents never change it.
- Agents set `running` only while actively working and clear it at closeout.
- Agents read project goals and recent lab notes before acting.
- Changes and completion remain approval-gated.
- Unreviewed agent-built work is handed back through a needs-review todo.
- Follow-up relationships are expressed through hierarchy and order.
- `for_later` is human-owned parking; agents require explicit per-item
  approval before placing anything in `Later…`.

See [`docs/API.md`](docs/API.md) for the supported local API subset.

Agenda can also keep an opt-in, local ledger of AI token usage. Any tool can
submit usage in the open format described in
[`docs/USAGE_EVENTS.md`](docs/USAGE_EVENTS.md).

## Privacy

The pack contains workflow instructions only. It does not contain or upload
the user's database, projects, notes, file paths, credentials, or server logs.
