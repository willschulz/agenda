# MCP server

`mcp/agenda_mcp.py` exposes the local Agenda API as
[Model Context Protocol](https://modelcontextprotocol.io) tools, so any MCP
client (Claude Code, Claude Desktop, Cursor and others) can work with Agenda
without `curl` or a shell. It is one file, uses only the Python standard
library (3.9 or newer), and runs locally over stdio. It talks to the Agenda
app at `http://127.0.0.1:8552`; set `AGENDA_URL` to point elsewhere.

The workflow instructions (`claude/CLAUDE.md`, the Cursor rules) still apply:
they cover judgment, such as reading northstars first or deciding whether to
reuse a todo. The MCP server makes the mechanical parts hard to get wrong:

- No tool can set or clear `in_progress`; that flag stays the user's.
- `start_work` sets `running`. `close_out` always leaves a lab note, then
  clears `running` or completes the todo.
- Completing needs a stated basis (the user approved, or the agent created
  the todo for the user's request and verified the work) and a project
  rollup note.
- New todos are never parked in `Later…`. `park_in_later` needs the user's
  approval, quoted, which is saved as a note.
- Titles are at most eight words. Notes are appended, never replaced.
- Review handoffs are placed as the top child of the reviewed todo, or at the
  top of the main list when that todo is parked.

## Tools

| Tool | Writes | Purpose |
|---|---|---|
| `list_projects` | no | Projects with row, open-todo count, running todos |
| `get_project` | no | Northstars, paths, recent notes, open-todo outline |
| `get_todo` | no | Pickup context: todo, notes, ancestors, children, project context |
| `search_todos` | no | Keyword search, optionally including completed todos |
| `start_work` | yes | Mark a todo `running` before the first change |
| `add_note` | yes | Append a lab note to a todo or project |
| `create_todo` | yes | New todo: top of list, child (`parent_id`), or directly below another (`follows_todo_id`) |
| `create_review_card` | yes | Needs-review handoff for decisions only the user can make |
| `close_out` | yes | Note plus clear `running`, or rollup plus complete |
| `park_in_later` | yes | Park a top-level todo in `Later…` with the user's approval |

Project northstars and paths, reordering and other edits are not tools; use
the [local API](API.md) for those, with the user's approval.

## Setup

Keep Agenda running. Then register the server with your client, using the
absolute path to `agenda_mcp.py`:

- **Claude Code:**

  ```bash
  claude mcp add agenda -- python3 /absolute/path/to/agent-integration/mcp/agenda_mcp.py
  ```

- **Claude Desktop, Cursor and other clients:** add a stdio server to the
  client's MCP configuration:

  ```json
  {
    "mcpServers": {
      "agenda": {
        "command": "python3",
        "args": ["/absolute/path/to/agent-integration/mcp/agenda_mcp.py"]
      }
    }
  }
  ```

Clients ask before each tool call unless you allow it. A reasonable setting
is to allow the four read tools and keep asking for the rest.

## Check it

Ask the agent to list your Agenda projects. If Agenda isn't running, the tools
say so and ask you to open it.
