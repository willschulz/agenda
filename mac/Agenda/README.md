# Agenda for macOS

Native macOS SwiftUI/SwiftPM app for Agenda: a `WKWebView` plus a supervised
local server. The app bundles relocatable CPython 3.13, Agenda's FastAPI
server and frontend, and all Python dependencies. It does not depend on
Homebrew, Anaconda, or a host Python installation.

Installs as `~/Applications/Agenda.app`, bundle ID `com.willschulz.Agenda`.

## Standalone by design

The embedded server owns `~/Library/Application Support/Agenda/agenda.db`,
which is the authoritative database. A fresh install seeds a generic starter
board via `seed.py`. No tailnet, account, or network service is required.

There is no remote or replica mode, so there is exactly one data path and no
ambiguity about which copy is authoritative. Optional iCloud sync (signed
builds only) replicates through CloudKit without changing that: the local
database stays authoritative.

Settings exposes two local controls:

- **On-device intelligence** — on macOS 26+, Agenda uses Apple’s on-device
  Foundation Models framework for concise todo titles and Pulse insights when
  Apple Intelligence is available. Deterministic fallbacks work without a
  model. Pulse keeps actionable Plan today/Stale work guidance separate from
  read-only accomplishment summaries, which appear when hovering past header
  dots. Agenda stores no model-provider credential and makes no third-party
  model calls.
- **Launch Agenda at login** — implemented with `SMAppService.mainApp`, so the
  local agent API is normally available without a manual launch.

## Agent integration pack

Release builds include the privacy-safe Cursor and Claude Code pack at
`Agenda.app/Contents/Resources/agent-integration/`. Use **Agenda → Settings →
Reveal Agent Integration Pack…** to open it, then follow `docs/CURSOR.md` or
`docs/CLAUDE_CODE.md`.

The same files are attached to each public release as
`Agenda-agent-integration-<version>.zip`. The canonical source is
`agent-integration/`; `scripts/assemble-app.sh` and
`scripts/publish-release.sh` both run its privacy/parity verifier before
bundling or publishing.

## Build + install

```bash
cd mac/Agenda
./scripts/vendor-python.sh # first run; pinned + checksum-verified
./install.sh             # swift build -c release, assemble, ad-hoc sign, launch
./install.sh --no-build  # re-bundle the existing binary without recompiling
```

## Quick iteration without bundling

```bash
cd mac/Agenda
swift run Agenda
```

Or open in Xcode: `open Package.swift`.

## Verify

```bash
pgrep -lx Agenda
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8552/api/projects
```

The app loads `http://127.0.0.1:8552/`. The local child PID is recorded at
`~/Library/Application Support/Agenda/server.pid`; a new launch reaps a child
left by a prior crash.

### SSE live-refresh

With the app open and idle, `PATCH` a harmless field on a scratch todo from
a shell, then confirm the app's UI updates without a manual reload (Agenda's
`EventSource('/api/events')` mechanism in `static/index.html`).

### Clipboard

Click a todo's hover Task number (`#id` →
`navigator.clipboard.writeText`) and confirm `pbpaste` reflects the copied
`Task #<id> "title"` text.

A `WKScriptMessageHandler` bridge (`WebView.swift`, `ClipboardBridge`) is
already built in: an injected `WKUserScript` patches
`navigator.clipboard.writeText` to try the native WebKit API first and,
only on rejection, fall back to `window.webkit.messageHandlers.agendaClipboard`
→ `NSPasteboard.general.setString`. `static/index.html` is untouched —
the patch happens entirely at the WebView layer.

This was added proactively rather than reactively: automated UI-click
testing (`osascript`/System Events) requires Accessibility permission for
the calling process, which this environment doesn't have granted
(`osascript is not allowed assistive access (-1719)`), so the real
click-driven clipboard path couldn't be automated end-to-end. Manual
verification is still recommended — click a Task number in the running app
and confirm `pbpaste` picks it up. If it works, the bridge is a no-op
(native path always wins); if native ever fails, the bridge silently covers it.

### External links

Clicking an `http(s)` link in a note (or anywhere else in the WKWebView) opens
Google Chrome instead of navigating the Agenda window. Agenda has no back
button, so an in-app load of GitHub/docs would trap the session. Same-origin
requests to the local server stay in-app. `mailto:` and other schemes use the
system default handler. If Chrome is missing, http(s) falls back to the
default browser.

## Structure

```
Agenda/
  Package.swift                      -- executableTarget, macOS(.v13), Swift 5 mode, Sparkle
  Sources/Agenda/
    AgendaApp.swift                   -- app lifecycle, single-instance guard, Settings scene
    LocalServerController.swift       -- starts/stops/restarts the embedded server
    PythonSupervisor.swift            -- embedded uvicorn lifecycle, first-run seed, health, PID reaping
    LegacyCredentialCleaner.swift     -- one-time removal of the retired provider key
    LoginItemController.swift         -- SMAppService.mainApp launch-at-login toggle
    ContentView.swift                 -- WebView plus failure/recovery UI
    WebView.swift                     -- NSViewRepresentable wrapping WKWebView; persistent WKWebsiteDataStore; clipboard bridge; external links open in Chrome
    Info.plist                        -- CFBundleIdentifier com.willschulz.Agenda
    AppIcon.icns                      -- generated from static/icons/icon-512.png via sips + iconutil
  Sources/AgendaLocalAI/              -- title contract + Foundation Models adapter
  Sources/AgendaAIHelper/             -- one-shot JSON stdin/stdout bridge for Python
  Tests/AgendaLocalAITests/           -- title contract and availability tests
  install.sh                          -- build -> assemble ~/Applications/Agenda.app -> ad-hoc codesign -> launch
  scripts/assemble-app.sh             -- copies server, frameworks, and agent pack into the app bundle
  scripts/vendor-python.sh            -- pinned relocatable runtime + dependencies
  vendor/python/                      -- generated, gitignored
```

## History: the AgendaMac name

Early builds were called **AgendaMac**, with bundle ID
`com.willschulz.AgendaMac`. `install.sh` removes a leftover
`~/Applications/AgendaMac.app` on install, since both bundles would otherwise
contend for port 8552. The bundle ID changed along with the name, so
`UserDefaults` started fresh under `com.willschulz.Agenda`. The database was
unaffected because its path was never keyed to the bundle ID.

## Uninstall

```bash
pkill -x Agenda 2>/dev/null || true
rm -rf ~/Applications/Agenda.app
rm -rf ~/Library/Application\ Support/Agenda/agenda.db
```

The last command deletes local Agenda data; omit it when reinstalling.

## Distribution status

The bundle is self-contained and clean-room tested. `install.sh` ad-hoc signs
it for this Mac; `scripts/package-release.sh` produces the signed, notarized,
stapled DMG for distribution. See [RELEASE.md](RELEASE.md). The current
runtime build is arm64-only.
