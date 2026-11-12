# AI usage events (open format, version 1)

Agenda can keep a local ledger of the tokens your AI coding tools use, so you
can see usage and estimated cost by project over time. The ledger is off by
default, lives only in your local Agenda database, is never synced to iCloud,
and never stores prompt or response text.

Agenda reads Claude Code's local logs itself once you turn the ledger on. Any
other tool, script, or machine can submit usage with one HTTP call in the
format below.

## Turn it on

```bash
curl -sS -X PATCH -H 'Content-Type: application/json' \
  -d '{"enabled": true}' http://127.0.0.1:8552/api/usage/config
```

While enabled, Agenda scans this Mac's Claude Code logs every 15 minutes
(`scan_interval_minutes`). `POST /api/usage/scan` scans immediately.

Claude Code deletes old logs after `cleanupPeriodDays` (30 days by default).
Agenda keeps whatever it has already read, but raise that setting in
Claude Code's `settings.json` if you want older history available.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`/`PATCH` | `/api/usage/config` | Read or change `enabled`, `scan_interval_minutes` |
| `POST` | `/api/usage/scan` | Scan this Mac's Claude Code logs now |
| `POST` | `/api/usage/events` | Submit a batch (idempotent) |
| `GET` | `/api/usage/summary?group_by=day\|model\|source\|machine\|session&since=YYYY-MM-DD&until=YYYY-MM-DD&tz_offset_minutes=N` | Token totals and estimates |
| `GET` | `/api/usage/sessions?since=YYYY-MM-DD` | Per-session totals, tool-reported cost, todo markers |
| `GET` | `/api/usage/by-project?since=YYYY-MM-DD&until=YYYY-MM-DD&tz_offset_minutes=N` | Estimates per project and local day, with attribution shares |
| `GET` | `/api/usage/todos/<id>` | All-time usage attributed to a todo and its subtasks |
| `GET` | `/api/usage/unattributed?since=YYYY-MM-DD` | Sessions no rule could attribute |
| `PUT`/`DELETE` | `/api/usage/sessions/<source>/<session_id>/assignment` | Assign a session to `{"project_id"}` or `{"todo_id"}`, or clear |
| `GET`/`POST` | `/api/usage/billing` | List or add what you pay (`source`, `label`, `kind`: `monthly`/`one_off`, `amount_usd`, `start_date`, `end_date`) |
| `PUT`/`DELETE` | `/api/usage/billing/<id>` | Replace or remove a charge |
| `GET` | `/api/usage/pricing` | The price table in effect |

## Batch format

```json
{
  "format_version": 1,
  "source": "my_tool",
  "machine_id": "a stable random id for this machine",
  "machine_label": "optional display name",
  "events": [
    {
      "event_key": "unique per source, e.g. message id + request id",
      "ts": "2026-10-07T15:04:05Z",
      "model": "claude-opus-5-5",
      "provider": "anthropic",
      "session_id": "optional conversation/session id",
      "cwd": "optional working directory",
      "git_branch": "optional",
      "client_version": "optional",
      "speed": "optional, e.g. standard or fast",
      "input_tokens": 0,
      "output_tokens": 0,
      "cache_read_tokens": 0,
      "cache_write_5m_tokens": 0,
      "cache_write_1h_tokens": 0,
      "reasoning_tokens": 0,
      "web_search_requests": 0,
      "reported_cost_usd": null
    }
  ],
  "sessions": [
    {"session_id": "...", "reported_cost_usd": 1.23, "reported_model_usage": {}}
  ],
  "markers": [
    {"session_id": "...", "ts": "2026-10-07T15:00:00Z", "todo_id": 42, "kind": "running"}
  ]
}
```

- `source` is a short lowercase identifier (`claude_code`, `cursor`, …).
- `(source, event_key)` is unique. Re-sending a batch, rescanning a log, or
  submitting the same log from two machines never double counts.
- Up to 5,000 items per list per request; split larger uploads.
- `output_tokens` includes reasoning; `reasoning_tokens` is a breakdown only.
- `reported_cost_usd` is the tool's own figure, kept for cross-checking. It is
  never mixed into Agenda's estimate.
- `markers` link a session to an Agenda todo. `kind` is `running` (an agent
  set the todo's `running` flag), `copy_block` (a structured todo context
  block with ids was pasted in), or `manual`. Only these strong signals
  count; a todo id that merely appears in output is not a marker.

## How usage is attributed to projects

Attribution is computed when you look, never stored on events, so it can be
corrected at any time. For each response, the first rule that applies wins:

1. **Manual:** you assigned its session to a project or todo.
2. **Todo marker:** its session has a marker. The latest marker at or before
   the response wins. Responses up to 30 minutes before the first marker
   also belong to it (agents read context before setting `running`);
   earlier responses in a long session fall through to the next rule.
3. **Working folder:** its `cwd` is inside a project's git repo or one of its
   absolute paths. The deepest match wins; a folder that contains another
   project's folder is treated as a shared workspace and ignored.
4. Otherwise it is **unattributed** and listed for manual assignment.

A todo's usage includes its subtasks.

## What the numbers mean

- **Tokens** are measured by the tool that made the request.
- **Estimate** is tokens × the price table: what the usage would cost at
  pay-as-you-go API list prices. If you pay a subscription, this is not what
  you were billed.
- **Share** (`share_bp`, basis points: 100 = 1%) is a project's part of all
  priced usage in the range, including Unattributed, so shares sum to 100%.
  It is weighted by API list price so tools and models are comparable;
  `by_source` splits it per tool (e.g. `claude_code`, `cursor`). Project
  cards show this share rather than dollars.
- **Actual cost** (`allocated_micros`) is what you entered under billing,
  split across projects. Each calendar month of a subscription is split by
  that tool's usage in that month; a one-off charge by usage in its own
  period. A tool with a charge but no recorded usage in the period stays
  **unallocated** instead of being spread over other tools' projects.
- A model or token type without a known rate is reported as **unpriced**,
  never as zero. Web-search requests are counted but not priced.

The bundled price table can be overridden by placing an edited copy named
`pricing.json` next to Agenda's database
(`~/Library/Application Support/Agenda/`).

## Sending from another machine

`usage_collect.py` (standard-library Python 3, inside the app bundle at
`Agenda.app/Contents/Resources/server/`) runs anywhere Claude Code does:

```bash
python3 usage_collect.py --dry-run
python3 usage_collect.py --post http://<agenda-host>:8552
```

It remembers how far it has read in
`~/.config/agenda-usage/state.json` (override with `AGENDA_USAGE_STATE`).
Agenda's API listens only on the local machine, so a remote collector needs
a forwarding method you trust, such as an SSH tunnel.

## Usage-limit pacing (Claude)

Separately from token accounting, Agenda can show how fast you are using
your Claude subscription's 5-hour and weekly limits. The dials appear once
Claude Code has reported usage. A gauge in the bottom-right
corner compares the share of the limit used (thick inner arc) with the share
of the window's time elapsed (thin outer arc, with a tick at the on-pace
point). Within ±5 points of the time elapsed you are on pace; more is over
pace, less is under.

The data comes from Claude Code's status line, which reports
`rate_limits.five_hour` and `rate_limits.seven_day` (only when Claude Code is
signed in with a Claude Pro or Max subscription; API-key use has no such
limits). To connect, open **Settings → Claude usage limits** and click
**Connect Claude Code**. Agenda then sets `statusLine` in
`~/.claude/settings.json` to the bridge inside the installed app and changes
nothing else in that file:

```json
"statusLine": {
  "type": "command",
  "command": "/Applications/Agenda.app/Contents/Resources/python/bin/python3 -I -B /Applications/Agenda.app/Contents/Resources/server/usage_statusline.py"
}
```

- **Your own status line is kept.** If you already had one, Agenda saves that
  setting in its data folder (`claude_statusline_previous.json`, plus a copy
  of the whole file as `claude-settings-before-agenda.json`). The bridge runs
  your command with the same input and prints its output, so what you see in
  Claude Code doesn't change. **Disconnect** puts your setting back exactly.
- **Moving the app.** If the app moves or is reinstalled somewhere else,
  Agenda repoints the status line at the new copy the next time it starts.
  Settings also offers **Use this copy** when another copy of Agenda is
  connected.
- **Invalid settings are left alone.** If `settings.json` isn't valid JSON,
  Agenda changes nothing and says so in Settings.

The command keeps `-B`. Without it, Python writes bytecode caches into the
signed app bundle each time the status line runs, which breaks the app's code
signature (`codesign --verify` then reports "a sealed resource is missing or
invalid"). Configure it by hand only if you need to, and keep `-I -B`.

Without a status line of your own, the bridge prints a short one (for example
`Opus · 5h 42%▼ · 7d 71%▲`). Either way it writes a snapshot under Agenda's
data folder. It makes no network calls and reads no credentials. Claude Code
only reports limits after a response, so readings age while Claude Code is
idle; the gauge shows each reading's age and marks it stale. A window whose
reset time has passed is shown as inactive until Claude Code reports a new
one.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/quota` | Each window's used %, reset time, elapsed %, pace, freshness, projection |
| `PATCH` | `/api/quota/config` | `target_pct` (share of the limit to aim to use, default 100) and `tolerance_pp` (default 5) |
| `GET` | `/api/quota/history?provider=claude&window=five_hour&hours=24` | Raw observations |
| `GET` | `/api/quota/claude-setup` | Whether Claude Code's status line is connected (`state`: `not_installed`, `not_connected`, `other`, `connected`, `moved`, `broken`, `error`) |
| `POST` | `/api/quota/claude-setup` | `{"action": "connect"}` or `{"action": "disconnect"}` |
