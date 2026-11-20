# Working on Agenda's source code (for AI coding agents)

This file is for an agent changing Agenda itself: Claude Code reads it as
`CLAUDE.md`, and Cursor, Codex and others read the same file as `AGENTS.md`.
The person you're working with may be an experienced developer or someone
building their first feature. Explain what you're doing in plain language,
show them the result in a browser before calling anything done, and never
assume they know git, Swift, or this codebase.

`agent-integration/claude/CLAUDE.md` is a different thing: a file Agenda
ships so users' agents can track work on their board. It is product content,
not instructions for this repository.

**Maintainers:** these instructions are written for contributors. If you're
working with the project's maintainer and their own workspace has rules for
Agenda (for example, about merging PRs or deploying to their installed app),
follow those rules where they differ from this file.

## The one rule that protects the user's data

There are two Agendas on this Mac, and you only ever test against the second.

| | Installed Agenda | Your development copy |
|---|---|---|
| What | `~/Applications/Agenda.app`, the user's daily app | this checkout |
| Data | `~/Library/Application Support/Agenda/agenda.db`, their real board | `data/dev.db` (gitignored) |
| Port | `127.0.0.1:8552` | `127.0.0.1:8551` (`scripts/dev.sh`) or 8562 (Agenda Sandbox) |

- **Never** read from, write to, copy, migrate, or delete the real database,
  and never send test requests to port 8552.
- **Never** run `mac/Agenda/install.sh` unless the user explicitly asks for
  it after you've explained the cost. It replaces their installed app with an
  unsigned build that uses their real board, has no iCloud sync, and stops
  updating itself. It refuses to replace a signed install unless given
  `--replace-signed`; don't add that flag on your own.
- If the user's agent setup includes Agenda's pickup/closeout workflow or
  its MCP server (tools such as `start_work` and `close_out`), using them to
  track *this* work on their board is fine. That's using Agenda, not testing
  it.
- The MCP server (`agent-integration/mcp/agenda_mcp.py`) talks to port 8552
  unless `AGENDA_URL` says otherwise. When you change or try it, run it
  against the dev server, `AGENDA_URL=http://127.0.0.1:8551`, never the real
  board. Its tests (`tests/test_agenda_mcp.py`) already use a temporary
  database.

## Run it

```bash
scripts/dev.sh            # server + web UI at http://127.0.0.1:8551, data/dev.db
scripts/dev.sh --fresh    # start over from the starter board
```

The first run downloads the pinned Python the app bundles (no Xcode needed).
Python edits reload on their own; after editing `static/index.html`, reload
the page. Most features (anything in the board, dialogs, the API) can be
built and checked entirely this way.

Changes to the native Mac app (`mac/Agenda/Sources/`: windows, menus,
Settings panes, sync) need Xcode or the Command Line Tools. Try them in the
separate sandbox app, which has its own data and port and runs beside the
real one:

```bash
mac/Agenda/scripts/try-first-run.sh          # build + launch with a fresh board
mac/Agenda/scripts/try-first-run.sh --keep   # relaunch, keeping its data
```

## Where things live

| To change… | Edit |
|---|---|
| Anything you see on the board | `static/index.html` (one file of vanilla HTML/CSS/JS, no build step) |
| The HTTP API | `main.py` |
| Stored data | `db.py`: additive, idempotent migrations only |
| Board ordering / todo hierarchy | `board_order.py`, `todo_structure.py` |
| The Mac app shell | `mac/Agenda/Sources/Agenda/` (SwiftUI) |
| Agent setup files users install | `agent-integration/` (rules, docs, API reference) |
| The MCP server for agents | `agent-integration/mcp/agenda_mcp.py`, docs in `agent-integration/docs/MCP.md` |

`README.md` has the architecture and data model; `CONTRIBUTING.md` has the
project's ground rules.

## Ground rules (from CONTRIBUTING.md)

- **Local-first.** No new network requests, analytics, or third-party model
  calls. Board content never leaves the Mac.
- **Keep the stack simple.** No frameworks, bundlers, or `node_modules`. New
  Python dependencies need a strong reason, because each one ships in the app.
- **The database is user data.** Never drop, rename, or rewrite columns.
- **Light and dark mode.** Use the existing CSS variables; check both.
- **Maintainer-only areas.** Don't change signing, entitlements, bundle IDs,
  release scripts, the Sparkle update feed, or CloudKit sync behavior
  (`sync_*.py`, `apple/AgendaSyncCore`, `CloudKit*` files). If a feature
  seems to need that, stop and explain why to the user so they can raise it
  in an issue.

## Before you say it's done

1. Show the user the change running in `scripts/dev.sh` (or the sandbox app).
2. Add or update a test where practical: Python in `tests/test_*.py`
   (`unittest`), Swift in `mac/Agenda/Tests/` or `apple/AgendaSyncCore/Tests/`.
3. Run `scripts/check.sh` (the same checks as CI) and fix what fails. Never
   weaken a test or the privacy gate to get it passing.

## Sharing the change

The repository is public, and `main` is protected: changes arrive by pull
request and need the maintainer's review.

1. Work on a branch, never on `main`:
   `git switch -c <short-description>`.
2. Keep each pull request to one feature or fix. Small PRs get reviewed and
   released fastest.
3. Commit, push, and open the PR with `gh pr create`. Fill in the template in
   plain English: what changed, why, how it was tested, and a screenshot for
   anything visible.
4. Don't merge it yourself. Tell the user it's waiting for review and that,
   once merged, it reaches every installed Agenda, theirs included, through
   the next automatic update.

Everything in this repo is public, including commit messages and PR text.
Never include the user's real projects, todos, names, file paths, hostnames,
or credentials. `scripts/verify-public.sh` catches some of this, not all of
it. A user can list extra private words in `.privacy-patterns.local`
(gitignored), one regex per line.

## Personal tweaks without changing the app

If the user wants something only for themselves, such as a script that
files todos from email or a weekly report, it may not need a code change at
all. While Agenda is running, its local API at `http://127.0.0.1:8552` reads
and updates their board (`agent-integration/docs/API.md`), and the MCP
server gives any MCP-capable agent the same access as tools
(`agent-integration/docs/MCP.md`). Suggest those routes when they fit, and
say so before writing to their real board.
