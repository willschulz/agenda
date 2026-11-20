# Contributing to Agenda

Thanks for helping build Agenda. This guide covers the development workflow
and the few rules that keep the project healthy.

## New to this? Building a feature with an AI agent

You don't need to know Python, Swift, or git to contribute. An AI coding
agent can do the typing, and this repository tells it how to work here
safely. A first feature usually looks like this:

1. **Keep using the release app** for your real work. Your experiments
   never touch it or its data.
2. **Get the code:** `gh repo clone willschulz/agenda` (or `git clone`), then
   `cd agenda`. You need the [GitHub CLI](https://cli.github.com) signed in
   (`gh auth login`) to open pull requests.
3. **Start your agent in that folder** (for example, run `claude`) and
   describe the feature the way you'd describe it to a colleague: what you
   want to see, and why.
4. **Look at it.** The agent runs a private copy of Agenda at
   `http://127.0.0.1:8551` with a practice board. Click around, and keep
   asking for changes until it's right.
5. **Optional: connect your agent to your own board.** Agenda's
   [MCP server](agent-integration/docs/MCP.md) lets the agent track the work
   on your real board (the practice copy stays separate). In Agenda, open
   **Settings → Agent tools (MCP)** and copy the Claude Code command (or the
   JSON for other clients). If your Agenda doesn't have that section yet,
   it arrives with the next update; until then, from this folder:
   `claude mcp add --scope user agenda -- python3 "$PWD/agent-integration/mcp/agenda_mcp.py"`.
6. **Ask the agent to open a pull request.** It runs the checks and writes up
   the change. The maintainer reviews it, and once it's merged, it arrives
   in everyone's Agenda, yours included, through the next automatic update.

Why not just run your own build? A build you make yourself can't use iCloud
sync (that needs the official signing identity) and doesn't update itself,
so you'd be stuck on a private copy. Sending changes back is how you get
your feature *and* keep sync and updates.

Small, single-purpose pull requests are easiest to review and ship fastest.
If you have an idea but don't want to build it, open a
[feature request](https://github.com/willschulz/agenda/issues/new/choose)
instead.

## Ground rules

- **Discuss larger changes first.** For anything bigger than a bug fix or a
  small UI tweak, open an issue (or comment on an existing one) describing
  what you want to build, so we can agree on the approach before you write
  much code.
- **Local-first and private by design.** Agenda keeps user data on the user's
  Mac. New features must not send board content, notes, or paths over the
  network, and must not add analytics or third-party model calls. If a
  feature needs a network request, document exactly what is sent and where,
  and make it a no-op when the tool isn't in use.
- **The database is user data.** Schema changes go in `db.py` as additive,
  idempotent migrations. Never drop or rewrite columns that existing
  installs depend on.
- **Keep the stack simple.** The web UI is one file of vanilla HTML, CSS,
  and JS with no build step, so please don't add a bundler or framework. New
  Python dependencies need a good reason, because every one ships inside the
  app.

## Workflow

1. Fork the repository (or create a branch if you have write access).
2. Set up the dev environment as described in
   [README → Getting started](README.md#getting-started-development). In
   short: `scripts/dev.sh`.
3. Make your change on a feature branch, with tests where practical:
   - Python behavior → `tests/test_*.py` (`unittest`)
   - Swift app or sync behavior → `mac/Agenda/Tests/` or
     `apple/AgendaSyncCore/Tests/`
4. Run the checks locally with `scripts/check.sh`. It runs the same steps
   as CI: Python tests, Swift tests, the agent-pack verifier, and the privacy
   gate.

5. Open a pull request against `main` and fill in the template. CI runs the
   same checks, and `main` requires a passing CI run and one approving
   review.

For UI changes, include a before/after screenshot in the PR. Check both
light and dark mode.

## The privacy gate

`scripts/verify-public.sh` scans every tracked file for personal paths
(`/Users/<name>/`), private network addresses, personal email addresses, and
credential-shaped strings. CI fails if anything matches. Use placeholders
such as `~/Projects/example` or `<your-apple-id>` in code, tests, and docs.

If you have your own private terms you never want to commit (hostnames,
project names), list them one regex per line in `.privacy-patterns.local` at
the repo root. That file is gitignored, and both verifiers pick it up.

Never commit `.env` files, databases (`*.db`), signing identities,
provisioning profiles, or API keys.

## Testing against real data safely

`scripts/dev.sh` and the Agenda Sandbox app
(`mac/Agenda/scripts/try-first-run.sh`) each use their own database, so
neither can touch your real board. `./install.sh` is different: it replaces
`~/Applications/Agenda.app`, and the new build uses the same database as your
installed copy. It refuses to replace a signed release unless you pass
`--replace-signed`. Before testing a migration or anything destructive
against real data, back up first with **Settings → Reveal Backups in
Finder**. Agenda also keeps automatic snapshots.

## Signing, releases, and iCloud sync

Contributors build ad-hoc signed copies with `install.sh`. Those builds run
fully but skip iCloud sync, which needs CloudKit entitlements tied to the
official Apple Developer team. Signed releases, notarization, and the
Sparkle update feed are handled by the maintainer
([`mac/Agenda/RELEASE.md`](mac/Agenda/RELEASE.md)). A merged change ships in
the next release.

## Code style

- Python: standard library first, type hints on new functions, `from
  __future__ import annotations`, and small pure helpers that are easy to
  unit-test.
- Swift: Swift 5 language mode (set in `Package.swift`). Follow the existing
  file structure.
- JS/CSS: follow the patterns in `static/index.html`. Colors come from the
  existing CSS variables, and every change has to work in light and dark
  mode.
- Comments explain *why*, not *what*.

## License

By contributing, you agree that your contributions are licensed under the
GNU General Public License v3.0 or later, the same license as the project.
