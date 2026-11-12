"""
Agenda -- usage-limit (quota) pacing.

Tracks how much of a provider's rolling allowance window has been used, so
the UI can show pace: quota used versus window time elapsed. This is separate
from the per-project token ledger (usage_ledger.py): quotas are account-wide
percentages reported by the provider, not local token counts.

Provider-independent model: one row per observation of (provider, window).
Observations arrive as snapshot files in the spool folder, written by
Claude Code's status line through usage_statusline.py.

Fixed windows (Claude 5h/7d) start at resets_at minus their length. A
snapshot may instead carry starts_at for windows of varying length.

Merge rules:
- A window is identified by its reset time. Within one allowance generation
  usage only grows, so the highest used % observed for that reset time wins;
  an older report from an idle session can never lower it. Claude Code
  re-runs the status line for idle sessions with their last-known values
  (no timestamp), so freshness counts only reports that confirm the current
  maximum, and trends follow the running maximum, never individual reports.
- A same-session drop of 10+ points starts a new generation inside the same
  reset window (the subscription allowance grew and percentages shrank). A
  later climb by that session back within 10 points of its pre-drop peak
  cancels the cut. Closed sessions stuck on the old percentage are dropped.
- The current window is the one with the latest reset time still in the
  future. If every known reset time has passed, the window is inactive: its
  old usage is never shown as current, and no new window is invented.
- Missing data is unknown, never zero.
"""

from __future__ import annotations

import glob
import json
import os
import time
from typing import Any

import db

# (provider, window) -> definition. "seconds" is the nominal length; a
# reported starts_at overrides it.
WINDOWS: dict[tuple[str, str], dict[str, Any]] = {
    ("claude", "five_hour"): {
        "label": "5-hour", "short": "Session", "seconds": 5 * 3600,
        "stale_after": 15 * 60, "trend_lookback": 90 * 60, "min_trend": 15 * 60,
    },
    ("claude", "seven_day"): {
        "label": "Weekly", "short": "Week", "seconds": 7 * 86400,
        "stale_after": 2 * 3600, "trend_lookback": 24 * 3600, "min_trend": 6 * 3600,
    },
}
PROVIDER_LABELS = {"claude": "Claude"}
SPOOL_PATTERNS = ("claude_statusline-*.json",)
RESET_TOLERANCE = 120  # seconds: reset times reported for one window may jitter
SPOOL_RETENTION = 8 * 86400
# Same-session used-% drop that marks a mid-window allowance resize (upgrade).
STEP_DOWN_POINTS = 10.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quota_observations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  provider     TEXT    NOT NULL,
  window       TEXT    NOT NULL,
  used_pct     REAL    NOT NULL,
  resets_at    INTEGER NOT NULL,   -- epoch seconds
  starts_at    INTEGER,            -- epoch seconds; NULL = resets_at - nominal length
  observed_at  REAL    NOT NULL,   -- epoch seconds these values were first seen
  seen_at      REAL    NOT NULL,   -- epoch seconds they were last reported
  source       TEXT    NOT NULL,
  session_id   TEXT    NOT NULL DEFAULT '',
  UNIQUE (provider, window, resets_at, used_pct, source, session_id)
);
CREATE INDEX IF NOT EXISTS idx_quota_obs_window
  ON quota_observations(provider, window, resets_at);

CREATE TABLE IF NOT EXISTS quota_config (
  id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  target_pct    REAL NOT NULL DEFAULT 100,  -- share of the allowance to aim to use
  tolerance_pp  REAL NOT NULL DEFAULT 5     -- ± points counted as "on pace"
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(quota_observations)")}
    if "starts_at" not in cols:
        conn.execute("ALTER TABLE quota_observations ADD COLUMN starts_at INTEGER")
    conn.execute("INSERT OR IGNORE INTO quota_config (id) VALUES (1)")


def get_config(conn=None) -> dict[str, float]:
    conn = conn or db.get_conn()
    row = conn.execute("SELECT target_pct, tolerance_pp FROM quota_config WHERE id = 1").fetchone()
    return {"target_pct": row["target_pct"], "tolerance_pp": row["tolerance_pp"]}


def patch_config(data: dict[str, Any], conn=None) -> dict[str, float]:
    conn = conn or db.get_conn()
    for key in ("target_pct", "tolerance_pp"):
        if key in data:
            conn.execute(f"UPDATE quota_config SET {key} = ? WHERE id = 1", (float(data[key]),))
    return get_config(conn)


# ---------------------------------------------------------------------------
# Ingest status-line snapshots
# ---------------------------------------------------------------------------

def spool_dir() -> str:
    return str(db.DB_PATH.parent / "quota")


def ingest_spool(conn=None, folder: str | None = None, now: float | None = None) -> int:
    """Read snapshot files into observations; return rows inserted."""
    conn = conn or db.get_conn()
    now = now or time.time()
    inserted = 0
    paths = [p for pat in SPOOL_PATTERNS for p in glob.glob(os.path.join(folder or spool_dir(), pat))]
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                snap = json.load(fh)
        except (OSError, ValueError):
            continue
        seen = float(snap.get("seen_at") or 0)
        if now - seen > SPOOL_RETENTION:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        inserted += ingest_snapshot(conn, snap)
    return inserted


def ingest_snapshot(conn, snap: dict[str, Any]) -> int:
    provider = snap.get("provider") or "claude"
    source = snap.get("source") or "unknown"
    session = str(snap.get("session_id") or "")
    observed = float(snap.get("changed_at") or snap.get("seen_at") or 0)
    seen = float(snap.get("seen_at") or observed)
    inserted = 0
    for window, w in (snap.get("windows") or {}).items():
        if (provider, window) not in WINDOWS or not isinstance(w, dict):
            continue
        used, resets = w.get("used_percentage"), w.get("resets_at")
        if not isinstance(used, (int, float)) or not isinstance(resets, (int, float)):
            continue  # unknown stays unknown
        starts = w.get("starts_at")
        starts = int(starts) if isinstance(starts, (int, float)) and starts < resets else None
        cur = conn.execute(
            "INSERT OR IGNORE INTO quota_observations (provider, window, used_pct, resets_at, "
            "starts_at, observed_at, seen_at, source, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (provider, window, float(used), int(resets), starts, observed, seen, source, session),
        )
        if cur.rowcount:
            inserted += 1
        else:
            conn.execute(
                "UPDATE quota_observations SET seen_at = MAX(seen_at, ?) WHERE provider = ? "
                "AND window = ? AND resets_at = ? AND used_pct = ? AND source = ? AND session_id = ?",
                (seen, provider, window, int(resets), float(used), source, session),
            )
    return inserted


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------

def _current_generation(rows: list) -> list:
    """Keep observations from the current allowance generation in one reset window.

    A same-session used-% drop of STEP_DOWN_POINTS or more starts a new
    generation (subscription upgrade resized the allowance). A later climb
    by that session back within STEP_DOWN_POINTS of its pre-drop peak
    cancels the cut. Idle sessions replaying older, lower readings never
    create a cut; closed sessions stuck before the cut are dropped.
    """
    peaks: dict[str, float] = {}
    # (observed_at, session_id, pre_drop_peak)
    cuts: list[tuple[float, str, float]] = []
    cancelled: set[int] = set()
    for r in rows:
        sid = str(r["session_id"] or "")
        used = float(r["used_pct"])
        prev = peaks.get(sid)
        if prev is not None and used <= prev - STEP_DOWN_POINTS:
            cuts.append((float(r["observed_at"]), sid, prev))
            peaks[sid] = used
            continue
        peaks[sid] = max(prev or 0.0, used)
        for i, (_, s, pre) in enumerate(cuts):
            if s == sid and i not in cancelled and used >= pre - STEP_DOWN_POINTS:
                cancelled.add(i)
    confirmed = [t for i, (t, _, _) in enumerate(cuts) if i not in cancelled]
    if not confirmed:
        return list(rows)
    gen_start = max(confirmed)
    newest: dict[str, float] = {}
    for r in rows:
        sid = str(r["session_id"] or "")
        newest[sid] = max(newest.get(sid, 0.0), float(r["observed_at"]))
    live = {sid for sid, t in newest.items() if t >= gen_start}
    return [r for r in rows
            if float(r["observed_at"]) >= gen_start and str(r["session_id"] or "") in live]


def _window_state(conn, provider: str, window: str, cfg: dict, now: float) -> dict[str, Any]:
    d = WINDOWS[(provider, window)]
    base = {"provider": provider, "provider_label": PROVIDER_LABELS.get(provider, provider),
            "window": window, "label": d["label"], "short": d["short"],
            "window_seconds": d["seconds"]}
    latest_reset = conn.execute(
        "SELECT MAX(resets_at) FROM quota_observations WHERE provider = ? AND window = ?",
        (provider, window),
    ).fetchone()[0]
    if latest_reset is None:
        return {**base, "status": "unavailable"}
    if latest_reset <= now:
        last_seen = conn.execute(
            "SELECT MAX(seen_at) FROM quota_observations WHERE provider = ? AND window = ?",
            (provider, window),
        ).fetchone()[0]
        return {**base, "status": "inactive", "last_reset_at": latest_reset, "last_seen_at": last_seen}

    rows = _current_generation(conn.execute(
        "SELECT used_pct, starts_at, observed_at, seen_at, source, session_id "
        "FROM quota_observations "
        "WHERE provider = ? AND window = ? AND resets_at >= ? ORDER BY observed_at",
        (provider, window, latest_reset - RESET_TOLERANCE),
    ).fetchall())
    if not rows:
        return {**base, "status": "unavailable"}
    used = max(r["used_pct"] for r in rows)
    seen_at = max(r["seen_at"] for r in rows if r["used_pct"] == used)
    sources = sorted({r["source"] for r in rows})
    reported_starts = [r["starts_at"] for r in rows if r["starts_at"] is not None]
    start = max(reported_starts) if reported_starts else latest_reset - d["seconds"]
    length = latest_reset - start
    elapsed_pct = max(0.0, min(100.0, 100.0 * (now - start) / length))
    target_now = elapsed_pct * cfg["target_pct"] / 100.0
    delta = used - target_now
    tol = cfg["tolerance_pp"]
    pace = "over" if delta > tol else ("under" if delta < -tol else "on")
    age = max(0.0, now - seen_at)

    # Projection from recent history inside this window: first reading in the
    # lookback versus now. A short burst stretched over a long window
    # misleads, so each window needs a minimum span of history.
    # Running maximum over time: the usage the window had certainly reached.
    envelope: list[tuple[float, float]] = []
    peak = 0.0
    for r in rows:  # ordered by observed_at
        peak = max(peak, r["used_pct"])
        if envelope and envelope[-1][0] == r["observed_at"]:
            envelope[-1] = (r["observed_at"], peak)
        else:
            envelope.append((r["observed_at"], peak))
    projection = None
    cutoff = now - d["trend_lookback"]
    recent = [p for p in envelope if p[0] >= cutoff]
    first = recent[0] if recent else None
    if first is not None and now - first[0] >= d["min_trend"]:
        first_t, first_used = first
        rate = max(0.0, (used - first_used) / (now - first_t))  # pct per second
        remaining = latest_reset - now
        projected = used + rate * remaining
        exhaust_at = None
        if rate > 0 and projected > cfg["target_pct"] and used < cfg["target_pct"]:
            exhaust_at = now + (cfg["target_pct"] - used) / rate
        projection = {
            "projected_pct_at_reset": round(projected, 1),
            "exhausts_at": exhaust_at,
            "basis_seconds": int(now - first_t),
        }

    return {
        **base,
        "window_seconds": length,
        "status": "stale" if age > d["stale_after"] else "fresh",
        "used_pct": round(used, 1),
        "window_start": start,
        "resets_at": latest_reset,
        "elapsed_pct": round(elapsed_pct, 1),
        "target_pct_now": round(target_now, 1),
        "pace_delta": round(delta, 1),
        "pace": pace,
        "last_seen_at": seen_at,
        "age_seconds": int(age),
        "sources": sources,
        "projection": projection,
    }


def state(conn=None, now: float | None = None) -> dict[str, Any]:
    conn = conn or db.get_conn()
    now = now or time.time()
    cfg = get_config(conn)
    providers: dict[str, list] = {}
    for provider, window in WINDOWS:
        providers.setdefault(provider, []).append(_window_state(conn, provider, window, cfg, now))
    return {"now": now, "config": cfg, "providers": providers}


def history(provider: str, window: str, since: float, conn=None) -> list[dict[str, Any]]:
    """Raw observations for future charts and forecasts."""
    conn = conn or db.get_conn()
    return [dict(r) for r in conn.execute(
        "SELECT used_pct, resets_at, observed_at, seen_at, source FROM quota_observations "
        "WHERE provider = ? AND window = ? AND seen_at >= ? ORDER BY observed_at",
        (provider, window, since),
    ).fetchall()]
