# Agenda

A calm, local-first project tracker for macOS, designed to make active work
visible without adding friction. Agenda keeps projects and todos on one board
with a clean, ADHD-friendly layout. It works offline, and it's built to be
picked up and driven by local coding agents such as Claude Code and Cursor.

- **Native macOS app** with a bundled Python runtime. No Homebrew, Python
  install, account, or server needed.
- **Your data stays on your Mac**: one SQLite database, with optional private
  iCloud sync to the companion iOS app on signed builds.
- **Agent-ready**: a local HTTP API plus a portable pickup/closeout workflow
  for Claude Code and Cursor.
- **On-device intelligence only**: optional Apple Foundation Models titles and
  insights. No third-party model calls.

> **Just want the app?** Download the latest signed, notarized DMG from
> [agenda-releases](https://github.com/willschulz/agenda-releases/releases).
> Installed copies update themselves through Sparkle. This repository is the
> source code.

## Contents

- [Features](#features)
- [Getting started (development)](#getting-started-development)
- [Project layout](#project-layout)
- [Architecture](#architecture)
- [Data model](#data-model)
- [API](#api)
- [AI and usage tracking](#ai-and-usage-tracking)
- [Agent integration](#agent-integration)
- [iOS app](#ios-app)
- [Backups and restore](#backups-and-restore)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Rows as priority tiers**: portrait-aspect project cards arranged in rows,
  highest priority at the top. A new board starts with Top of Mind, Near
  Publication, In Development, Early Stage, and Future Ideas.
  **Settings → Edit Rows** renames, adds, reorders, and deletes rows on a
  zoomed-out view of the board, and asks where a deleted row's projects go.
- **First-run welcome tour**: a new board opens a short tour that explains the
  tiers and lets the user shape their rows. Top of Mind starts with
  Correspondence, Life Stuff, and Work Miscellany. Replay it from Settings.
- **Project cards**: name, optional deadline badge, and an ordered todo list
  with the blocker pinned and highlighted at the top, plus a "Later…"
  disclosure for parked `for_later` items.
- **Nested todos**: drag to reorder, nest, and move todos between projects.
- **`in_progress` status**: a purple left bar with a play marker, toggled from
  each todo's hover controls. Completing a todo clears it.
- **Daily progress**: completed todos fill the daily-goal heatmap in the
  header. Hovering a past day shows per-project counts, and each project
  expands to the exact completed tasks.
- **Effort points (optional)**: **Settings → Effort points** gives each todo a
  weight (1, 2, 3, 5, 8, 13) for the heatmap, velocity, and in-progress limit.
  Unpointed completed todos count as 0.25. When it's off, the default for new
  boards, every task counts as 1 and point badges are hidden. Points already
  set are kept and return when it's switched back on.
- **Two-tier project context**:
  - **Northstars** (per project): durable goals and principles, pinned above
    the notes and read first by agents.
  - **Lab notes** (on projects and todos): reverse-chronological
    `{ ts, author, text }` entries that humans and agents append to.
- **On-device title shortening**: a long todo keeps its original text in
  `notes[0]` and gets a concise title, at most one line, eight words, and 60
  characters. Titles come from Apple's Foundation Models framework on
  supported Macs, with a deterministic fallback everywhere else.
- **Agenda Pulse**: *Plan today* and *Stale work* suggestions derived
  deterministically from the database. On supported Macs the on-device model
  can pick and rephrase up to three candidates, but it can't change tasks.
- **Recent activity**: a header list of recently created, edited, noted, or
  completed todos. Selecting one jumps to it on the board.
- **Live updates**: every open window or tab stays current through a
  Server-Sent Events stream.
- **Board zoom**: pinch the trackpad (or press ⌘− / ⌘= / ⌘0) to step the
  board between 100% and 40%, so more of it fits on screen. The header stays
  full size, a small pill shows the level (click it to reset), and each
  device remembers its zoom.
- **Usage-limit pace dials**: optional gauges for Claude and/or Cursor
  subscription limits (see [AI and usage tracking](#ai-and-usage-tracking)).

## Getting started (development)

Requirements: an Apple Silicon Mac running macOS 13 or later, Xcode 16 or
later (or the matching Command Line Tools), and Python 3.12 or later for
running the server directly. On-device AI features need macOS 26 with Apple
Intelligence enabled. Everything else works without it.

### 1. Run the server and web UI directly (fastest loop)

```bash
git clone https://github.com/willschulz/agenda.git
cd agenda
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python seed.py                      # optional: a generic example board
uvicorn main:app --reload --host 127.0.0.1 --port 8551
# open http://127.0.0.1:8551/
```

The database lives at `./data/agenda.db` (gitignored) unless you set
`AGENDA_DB_PATH`. See `.env.example` for the other settings. Run the dev
server on port 8551 so it can't collide with an installed Agenda app, which
uses 8552.

### 2. Build and install the native app

```bash
cd mac/Agenda
./scripts/vendor-python.sh   # first run only: pinned, checksum-verified CPython
./install.sh                 # swift build, assemble, ad-hoc sign, install, launch
```

`install.sh` installs to `~/Applications/Agenda.app` and launches it. The
build is ad-hoc signed, so it runs only on your Mac and has **no iCloud
sync**, which needs a signed build with CloudKit entitlements. That's fine for
development. **Warning:** if you already run the release app, `install.sh`
replaces it and uses the same database at
`~/Library/Application Support/Agenda/agenda.db`. Back it up first, or develop
against the web server from step 1.

See [`mac/Agenda/README.md`](mac/Agenda/README.md) for details on the app.
Signed, notarized releases are covered in
[`mac/Agenda/RELEASE.md`](mac/Agenda/RELEASE.md) and are made by the
maintainer.

### 3. Run the tests

```bash
# Python (server, sync contract, usage ledger, insights)
PYTHONPATH=. python -m unittest discover -s tests

# Swift (macOS app and local-AI contract)
(cd mac/Agenda && swift test)

# Swift (shared CloudKit sync core)
(cd apple/AgendaSyncCore && swift test)

# Agent-integration pack and privacy gate
agent-integration/scripts/verify-pack.sh
scripts/verify-public.sh
```

CI runs all of these on every pull request.

## Project layout

```
main.py                 FastAPI app: routes, SSE, background tasks
db.py                   SQLite connection, schema, additive migrations
seed.py                 Generic starter board for fresh installs
board_order.py          Ordering and nesting rules
todo_structure.py       Todo hierarchy helpers
insights.py             Agenda Pulse (deterministic candidates + local AI)
sync_*.py               CloudKit sync contract, tombstones, recovery
usage_*.py              AI usage ledger and usage-limit pacing
pricing.json            Price table for API-equivalent cost estimates
static/                 The single-file web UI (vanilla HTML/CSS/JS, no build)
tests/                  Python tests (unittest)
mac/Agenda/             Native macOS app (SwiftUI + SwiftPM), packaging scripts
apple/AgendaSyncCore/   Swift package shared by the Mac and iOS sync code
ios/                    iOS companion app (XcodeGen project)
agent-integration/      Claude Code / Cursor setup guides, API reference, rules
public-release/         Files published to the agenda-releases repository
scripts/                Repository tooling (privacy gate)
```

## Architecture

```text
Agenda.app (SwiftUI)
  ├─ WKWebView ─────────────► http://127.0.0.1:8552  (static/index.html)
  ├─ PythonSupervisor ──────► bundled CPython 3.13 + FastAPI (main.py)
  │                             └─ ~/Library/Application Support/Agenda/agenda.db
  ├─ AgendaAIHelper ────────► Apple Foundation Models (one-shot JSON process)
  ├─ CloudKitCoordinator ───► private iCloud database (signed builds only)
  └─ Sparkle ───────────────► agenda-releases appcast (updates)
```

FastAPI plus a single-file SPA keeps the whole stack readable end to end:
one Python service, one HTML file of vanilla JS, SQLite for state, no build
step, and no `node_modules`. The server binds only to `127.0.0.1`, and that
is the auth perimeter. There is no app-level login.

## Data model

`parent_id` on `todos` supports nesting.

```sql
projects (
  id, name, category, display_order,
  deadline,                        -- ISO date string or NULL
  northstars,                      -- JSON array of strings (durable goals + principles)
  notes,                           -- JSON array of {ts,author,text} lab-note entries (newest first)
  paths_json,                      -- JSON array of filesystem paths
  git_repo,                        -- optional path to a git checkout
  archived,                        -- 0/1; archived cards are hidden
  created_at, updated_at
)

todos (
  id, project_id, parent_id,
  text,                            -- display title (may be shortened on-device)
  is_blocker, for_later,           -- for_later is human-owned Later parking
  in_progress, running, awaiting_review, flagged, display_order,
  completed, completed_at,
  effort,                          -- optional integer effort weight
  notes,                           -- JSON array of {ts,author,text} lab-note entries (newest first)
  paths_json,                      -- JSON array of filesystem paths
  title_status,                    -- '' | 'pending' | 'generated' | 'failed'
  suggestions_json,                -- retained legacy data; no active feature
  suggestions_status,              -- retained legacy data; no active feature
  created_at, updated_at
)

day_overrides (
  date TEXT PRIMARY KEY            -- ISO YYYY-MM-DD; presence = flipped from natural workday state
)

board_rows (                       -- the user's rows ("priority tiers")
  key PRIMARY KEY,                 -- stable id stored in projects.category
  label,                           -- display name; renaming changes only this
  position,                        -- top to bottom; top_of_mind is always 0
  created_at, updated_at
)
```

The full schema and migrations live in `db.py` (`SCHEMA_SQL` plus the
`_migrate_*` functions). Migrations are additive and idempotent. Usage-ledger
and quota tables are created by `usage_ledger.py` and `usage_quota.py`. They
stay on the Mac and are never synced.

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Serve `static/index.html` |
| `GET` | `/api/health` | Liveness check |
| `GET` | `/api/capabilities` | Report on-device and deterministic capabilities |
| `GET` | `/api/events` | SSE stream: board version on every write |
| `GET` | `/api/projects` | All projects and their todos, grouped by category |
| `POST` | `/api/projects` | Create a project |
| `PATCH` | `/api/projects/{id}` | Edit name, category, deadline, notes, northstars, paths, git_repo, order, archived |
| `DELETE` | `/api/projects/{id}` | Delete (cascades to todos) |
| `POST` | `/api/projects/{id}/notes` | Prepend a lab note `{text, author}` |
| `POST` | `/api/todos` | Create a todo (long text → on-device title) |
| `PATCH` | `/api/todos/{id}` | Edit text, flags, order, completed, notes, paths, effort, … |
| `DELETE` | `/api/todos/{id}` | Delete |
| `POST` | `/api/todos/{id}/notes` | Prepend a lab note `{text, author}` |
| `POST` | `/api/todos/{id}/complete` | Complete (`completed_at=now()`, clears `in_progress`) |
| `POST` | `/api/todos/{id}/uncomplete` | Clear completion |
| `POST` | `/api/todos/{id}/retry-title` | Retry on-device title generation |
| `POST` | `/api/todos/reorder` | Atomically update `project_id`, `parent_id`, `display_order` |
| `GET` | `/api/insights?mode=…` | Cached or deterministic Pulse insights |
| `POST` | `/api/insights/refresh` | Refine a Pulse mode with the local model |
| `PATCH` | `/api/insights/{id}` | Dismiss, snooze, or clear an insight |
| `GET` | `/api/stats` | Completed effort and per-day accomplishment details |
| `GET` | `/api/day-overrides` | List overridden dates |
| `POST` | `/api/day-overrides/toggle` | Flip a date's workday/holiday status |
| `GET` | `/api/rows` | The board's rows (priority tiers) with project counts |
| `PUT` | `/api/rows` | Apply a row-editor session: rename, add, reorder, delete + move projects |
| `POST` | `/api/onboarding/complete` | Dismiss the first-run welcome tour |
| `POST` | `/api/onboarding/restart` | Show the welcome tour again |
| `GET`/`PATCH` | `/api/usage/config` | Opt-in AI usage ledger settings |
| `POST` | `/api/usage/scan` | Scan this Mac's Claude Code logs now |
| `POST` | `/api/usage/events` | Ingest usage in the open event format |
| `GET` | `/api/usage/summary` | Token totals and API-equivalent estimates |
| `GET` | `/api/usage/sessions` | Per-session totals and todo markers |
| `GET` | `/api/usage/by-project` | Estimates per project and day |
| `GET` | `/api/usage/todos/{id}` | Usage attributed to a todo and its subtasks |
| `GET` | `/api/usage/unattributed` | Sessions no rule could attribute |
| `PUT`/`DELETE` | `/api/usage/sessions/{source}/{session_id}/assignment` | Manually attribute a session |
| `GET` | `/api/usage/pricing` | The price table in effect |
| `GET` | `/api/quota` | Usage-limit windows with pace |
| `PATCH` | `/api/quota/config` | Pace target and tolerance |
| `GET` | `/api/quota/history` | Raw usage-limit observations |

[`agent-integration/docs/API.md`](agent-integration/docs/API.md) is the
agent-facing reference.

### Live updates (SSE)

`GET /api/events` holds a `text/event-stream` connection open. Every write
calls `_notify_change()`, which increments a monotonic `_board_version` and
wakes all waiters through an `asyncio.Event`. Each tab opens one
`EventSource` and calls `reload()` only when the version increases, and
never while the user is mid-entry. A 25-second keepalive comment keeps idle
connections open.

## AI and usage tracking

Agenda stores no model-provider credentials and makes no third-party model
calls.

- **Titles and Pulse** use Apple Foundation Models on eligible Macs, through
  the bundled `AgendaAIHelper`, with deterministic fallbacks. Model output can
  only reference candidate IDs that Python supplies; targets, actions, and
  evidence stay deterministic. `GET /api/capabilities` reports which provider
  is active.
- **AI usage ledger (opt-in)** reads the token counts that local coding agents
  already log (Claude Code today) and accepts an
  [open event format](agent-integration/docs/USAGE_EVENTS.md) from any other
  tool. It stores no prompt text. Costs are API-equivalent estimates from
  `pricing.json`, which you can override per install.
- **Usage-limit pace dials** show how fast you're using your subscription
  limits, for whichever tools you use:
  - **Claude**: point Claude Code's `statusLine` at the bundled
    `usage_statusline.py` bridge. The bridge makes no network calls.
  - **Cursor**: automatic when the Cursor desktop app is installed and signed
    in. Agenda reads Cursor's local session token (read-only, held in memory
    only) and polls `cursor.com`'s usage summary every 10 minutes.

  A provider's dials appear only after it has reported data. Setup and the
  exact privacy behavior are in
  [`USAGE_EVENTS.md`](agent-integration/docs/USAGE_EVENTS.md#usage-limit-pacing-claude-and-cursor).

## Agent integration

[`agent-integration/`](agent-integration/) is a portable pack for Claude Code,
Cursor, and other local agents. It has setup guides, the local API
reference, and pickup/closeout rules. Release builds bundle it (**Agenda →
Settings → Reveal Agent Integration Pack…**), and each GitHub release
attaches it as a ZIP.

The workflow: an agent reads a project's northstars and recent lab notes,
proposes a plan, and waits for confirmation. Humans own `in_progress`, while
agents set `running` only while they're actively working. At closeout the
agent appends a concise lab note and asks before creating follow-ups or
completing work.

Hovering a todo's number copies `Task #<id> "title"`. Paste that into an
agent session to have the pickup rule resolve the todo through the API.

## iOS app

`ios/` contains a companion iPhone/iPad app that syncs with the Mac through
the user's **private** CloudKit database. It uses the shared
`apple/AgendaSyncCore` package. Generate the Xcode project with
[XcodeGen](https://github.com/yonaskolb/XcodeGen):

```bash
cd ios
xcodegen generate
open AgendaMobile.xcodeproj
```

To run on a device or use CloudKit, change `DEVELOPMENT_TEAM`, the bundle IDs,
and the iCloud container (in `project.yml`, the entitlements, and
`apple/AgendaSyncCore/Sources/AgendaSyncCore/Models.swift`) to ones your Apple
Developer account owns. Unit tests run with `AGENDA_DISABLE_CLOUDKIT=1` and
don't need iCloud. TestFlight distribution notes for the official build are
in [`ios/TESTFLIGHT.md`](ios/TESTFLIGHT.md).

## Backups and restore

Every installation backs up its database on startup and every six hours,
using SQLite's Online Backup API. It keeps 30 self-contained, integrity-checked
snapshots in `~/Library/Application Support/Agenda/backups/`. Use **Agenda →
Settings → Reveal Backups in Finder** to see them.

Before an update changes the database's structure, Agenda also saves a
`pre-migration-v<old>-to-v<new>-<time>.db` snapshot in the same folder. These
are kept separately from the rolling backups (the last five are kept). If
that snapshot can't be written, Agenda doesn't start and leaves the database
untouched.

To restore a snapshot:

1. Quit Agenda completely.
2. In `~/Library/Application Support/Agenda/`, rename `agenda.db` to
   `agenda.db.before-restore`, and move `agenda.db-wal` and `agenda.db-shm`
   aside if they exist.
3. Copy the chosen `backups/agenda-*.db` to `agenda.db`.
4. Launch Agenda and check that your projects and todos are there.

Never replace the live database while Agenda is running.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
development workflow, coding conventions, and the privacy gate every PR must
pass.

## License

Agenda is free software: you can redistribute it and/or modify it under the
terms of the [GNU General Public License](LICENSE), version 3 or (at your
option) any later version. Copyright © 2026 Will Schulz and contributors.

Bundled third-party components keep their own licenses. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
