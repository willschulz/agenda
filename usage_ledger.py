"""
Agenda -- AI usage ledger.

Stores token-usage events from local AI coding tools in Mac-local tables and
derives API-equivalent cost estimates from a bundled, editable price table.

Design rules:
- Events are facts: idempotent on (source, event_key); never rewritten.
- Attribution to projects/todos is derived at query time (later phase), so
  markers are stored raw.
- Money is integer micro-USD. A group whose usage needs an unknown rate is
  reported as unpriced, never as zero.
- These tables are local only: CloudKit sync code addresses its own tables
  by name and never sees them.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import db

BUNDLED_PRICING = Path(__file__).resolve().parent / "pricing.json"
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
)
RATE_FOR_FIELD = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cache_read",
    "cache_write_5m_tokens": "cache_write_5m",
    "cache_write_1h_tokens": "cache_write_1h",
}
COUNT_FIELDS = TOKEN_FIELDS + ("reasoning_tokens", "web_search_requests")
MAX_EVENTS_PER_BATCH = 5000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS usage_config (
  id                     INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  enabled                INTEGER NOT NULL DEFAULT 0,
  scan_interval_minutes  INTEGER NOT NULL DEFAULT 15,
  local_machine_id       TEXT    NOT NULL,
  last_scan_at           TEXT,
  last_scan_status       TEXT,
  last_scan_counts_json  TEXT
);

CREATE TABLE IF NOT EXISTS usage_machines (
  machine_id  TEXT PRIMARY KEY,
  label       TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  source                 TEXT    NOT NULL,
  event_key              TEXT    NOT NULL,
  machine_id             TEXT    NOT NULL,
  ts                     TEXT    NOT NULL,
  session_id             TEXT,
  provider               TEXT,
  model                  TEXT    NOT NULL,
  cwd                    TEXT,
  git_branch             TEXT,
  client_version         TEXT,
  speed                  TEXT,
  input_tokens           INTEGER NOT NULL DEFAULT 0,
  output_tokens          INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
  cache_write_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens       INTEGER NOT NULL DEFAULT 0,
  web_search_requests    INTEGER NOT NULL DEFAULT 0,
  reported_cost_micros   INTEGER,
  ingested_at            TEXT    NOT NULL,
  UNIQUE (source, event_key)
);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts      ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(source, session_id);

CREATE TABLE IF NOT EXISTS usage_sessions (
  source                     TEXT NOT NULL,
  session_id                 TEXT NOT NULL,
  machine_id                 TEXT NOT NULL,
  reported_cost_micros       INTEGER,
  reported_model_usage_json  TEXT,
  updated_at                 TEXT NOT NULL,
  PRIMARY KEY (source, session_id)
);

CREATE TABLE IF NOT EXISTS usage_markers (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT    NOT NULL,
  session_id  TEXT    NOT NULL,
  ts          TEXT    NOT NULL,
  todo_id     INTEGER NOT NULL,
  kind        TEXT    NOT NULL,
  UNIQUE (source, session_id, ts, todo_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_usage_markers_session ON usage_markers(source, session_id, ts);

CREATE TABLE IF NOT EXISTS usage_billing (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source        TEXT    NOT NULL,              -- tool whose usage this pays for
  label         TEXT    NOT NULL,
  kind          TEXT    NOT NULL CHECK (kind IN ('monthly', 'one_off')),
  amount_micros INTEGER NOT NULL CHECK (amount_micros >= 0),
  start_date    TEXT    NOT NULL,              -- monthly: first covered day; one_off: period start
  end_date      TEXT,                          -- monthly: last covered day (NULL = ongoing); one_off: period end
  created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_assignments (
  source      TEXT    NOT NULL,
  session_id  TEXT    NOT NULL,
  project_id  INTEGER,
  todo_id     INTEGER,
  updated_at  TEXT    NOT NULL,
  PRIMARY KEY (source, session_id)
);
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)
    if conn.execute("SELECT 1 FROM usage_config WHERE id = 1").fetchone() is None:
        conn.execute(
            "INSERT INTO usage_config (id, local_machine_id) VALUES (1, ?)",
            (str(uuid.uuid4()),),
        )


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _usd_to_micros(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 1_000_000).to_integral_value())
    except Exception:
        return None


def micros_to_usd(micros: int) -> str:
    return f"{Decimal(micros) / 1_000_000:.2f}"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config(conn=None) -> dict[str, Any]:
    conn = conn or db.get_conn()
    row = conn.execute("SELECT * FROM usage_config WHERE id = 1").fetchone()
    counts = json.loads(row["last_scan_counts_json"]) if row["last_scan_counts_json"] else None
    return {
        "enabled": bool(row["enabled"]),
        "scan_interval_minutes": row["scan_interval_minutes"],
        "local_machine_id": row["local_machine_id"],
        "last_scan_at": row["last_scan_at"],
        "last_scan_status": row["last_scan_status"],
        "last_scan_counts": counts,
    }


def patch_config(data: dict[str, Any], conn=None) -> dict[str, Any]:
    conn = conn or db.get_conn()
    if "enabled" in data:
        conn.execute("UPDATE usage_config SET enabled = ? WHERE id = 1", (int(bool(data["enabled"])),))
    if "scan_interval_minutes" in data:
        conn.execute(
            "UPDATE usage_config SET scan_interval_minutes = ? WHERE id = 1",
            (int(data["scan_interval_minutes"]),),
        )
    return get_config(conn)


def record_scan(status: str, counts: dict[str, int] | None, conn=None) -> None:
    conn = conn or db.get_conn()
    conn.execute(
        "UPDATE usage_config SET last_scan_at = ?, last_scan_status = ?, "
        "last_scan_counts_json = ? WHERE id = 1",
        (_now(), status, json.dumps(counts) if counts is not None else None),
    )


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest(batch: dict[str, Any], conn=None) -> dict[str, int]:
    """Insert one open-format batch atomically. Re-sending is a no-op."""
    conn = conn or db.get_conn()
    source = batch["source"]
    machine_id = batch["machine_id"]
    now = _now()
    inserted = duplicates = 0
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO usage_machines (machine_id, label, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(machine_id) DO UPDATE SET "
            "label = COALESCE(excluded.label, usage_machines.label), last_seen = excluded.last_seen",
            (machine_id, batch.get("machine_label"), now, now),
        )
        for ev in batch.get("events", []):
            cur = conn.execute(
                "INSERT OR IGNORE INTO usage_events (source, event_key, machine_id, ts, "
                "session_id, provider, model, cwd, git_branch, client_version, speed, "
                + ", ".join(COUNT_FIELDS)
                + ", reported_cost_micros, ingested_at) VALUES ("
                + ", ".join(["?"] * (11 + len(COUNT_FIELDS) + 2))
                + ")",
                (
                    source, ev["event_key"], machine_id, ev["ts"], ev.get("session_id"),
                    ev.get("provider"), ev["model"], ev.get("cwd"), ev.get("git_branch"),
                    ev.get("client_version"), ev.get("speed"),
                    *(int(ev.get(f) or 0) for f in COUNT_FIELDS),
                    _usd_to_micros(ev.get("reported_cost_usd")), now,
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                duplicates += 1
        for s in batch.get("sessions", []):
            conn.execute(
                "INSERT INTO usage_sessions (source, session_id, machine_id, "
                "reported_cost_micros, reported_model_usage_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, session_id) DO UPDATE SET "
                "reported_cost_micros = excluded.reported_cost_micros, "
                "reported_model_usage_json = excluded.reported_model_usage_json, "
                "updated_at = excluded.updated_at",
                (
                    source, s["session_id"], machine_id,
                    _usd_to_micros(s.get("reported_cost_usd")),
                    json.dumps(s.get("reported_model_usage") or {}), now,
                ),
            )
        markers = 0
        for m in batch.get("markers", []):
            cur = conn.execute(
                "INSERT OR IGNORE INTO usage_markers (source, session_id, ts, todo_id, kind) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, m["session_id"], m["ts"], int(m["todo_id"]), m["kind"]),
            )
            markers += cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "inserted": inserted,
        "duplicates": duplicates,
        "sessions": len(batch.get("sessions", [])),
        "markers_inserted": markers,
    }


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def pricing_path() -> Path:
    override = os.environ.get("AGENDA_PRICING_PATH")
    if override:
        return Path(override)
    user_copy = db.DB_PATH.parent / "pricing.json"
    return user_copy if user_copy.exists() else BUNDLED_PRICING


def load_pricing(path: Path | None = None) -> dict[str, Any]:
    with open(path or pricing_path(), encoding="utf-8") as fh:
        return json.load(fh)


def model_key(provider: str | None, model: str) -> str:
    return f"{provider or 'unknown'}/{_DATE_SUFFIX.sub('', model)}"


def price_group(
    pricing: dict[str, Any], provider: str | None, model: str, speed: str | None,
    tokens: dict[str, int],
) -> int | None:
    """Return estimated micro-USD for summed tokens, or None if unpriced."""
    entry = pricing.get("models", {}).get(model_key(provider, model))
    if entry is None:
        return None
    multiplier = Decimal(1)
    if speed and speed != "standard":
        factor = (entry.get("speed") or {}).get(speed)
        if factor is None:
            return None
        multiplier = Decimal(factor)
    numerator = Decimal(0)  # micro-USD × 1e6 (rates are per million tokens)
    for field, rate_name in RATE_FOR_FIELD.items():
        count = tokens.get(field) or 0
        if not count:
            continue
        rate = entry.get(rate_name)
        if rate is None:
            return None
        numerator += Decimal(count) * Decimal(rate) * 1_000_000
    return int((numerator * multiplier / 1_000_000).to_integral_value())


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_GROUPS = {
    "day": "date(ts, :offset)",
    "model": "model",
    "source": "source",
    "machine": "machine_id",
    "session": "session_id",
}


def _grouped(conn, group_by: str, since: str | None, until: str | None, offset: str):
    key_sql = _GROUPS[group_by]
    where, params = _date_filter(since, until, offset)
    sums = ", ".join(f"SUM({f}) AS {f}" for f in COUNT_FIELDS)
    return conn.execute(
        f"SELECT {key_sql} AS k, provider, model, COALESCE(speed, 'standard') AS speed, "
        f"COUNT(*) AS events, {sums} FROM usage_events WHERE {where} "
        f"GROUP BY k, provider, model, speed ORDER BY k",
        params,
    ).fetchall()


def summary(
    group_by: str = "day",
    since: str | None = None,
    until: str | None = None,
    tz_offset_minutes: int = 0,
    conn=None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Token and API-equivalent totals, grouped by day/model/source/machine/session."""
    if group_by not in _GROUPS:
        raise ValueError(f"group_by must be one of {sorted(_GROUPS)}")
    conn = conn or db.get_conn()
    pricing = pricing or load_pricing()
    offset = f"{-tz_offset_minutes} minutes"
    rows: dict[Any, dict[str, Any]] = {}
    totals = _empty_bucket()
    unpriced_models: set[str] = set()
    for r in _grouped(conn, group_by, since, until, offset):
        tokens = {f: r[f] or 0 for f in COUNT_FIELDS}
        estimate = price_group(pricing, r["provider"], r["model"], r["speed"], tokens)
        if estimate is None:
            unpriced_models.add(model_key(r["provider"], r["model"]))
        for bucket in (rows.setdefault(r["k"], _empty_bucket()), totals):
            bucket["events"] += r["events"]
            for f in COUNT_FIELDS:
                bucket[f] += tokens[f]
            if estimate is None:
                bucket["unpriced_events"] += r["events"]
            else:
                bucket["estimate_micros"] += estimate
    out_rows = [{"key": k, **_finish(b)} for k, b in rows.items()]
    return {
        "group_by": group_by,
        "since": since,
        "until": until,
        "pricing_basis": "api_equivalent_list_price",
        "totals": _finish(totals),
        "rows": out_rows,
        "unpriced_models": sorted(unpriced_models),
        "excluded_from_estimate": ["web_search_requests"],
    }


def sessions(
    since: str | None = None, tz_offset_minutes: int = 0, conn=None,
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-session totals with the tool's own reported cost and todo markers."""
    conn = conn or db.get_conn()
    pricing = pricing or load_pricing()
    by_session = summary("session", since=since, tz_offset_minutes=tz_offset_minutes,
                         conn=conn, pricing=pricing)["rows"]
    meta = {
        r["session_id"]: r
        for r in conn.execute(
            "SELECT session_id, source, machine_id, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
            "GROUP_CONCAT(DISTINCT model) AS models FROM usage_events GROUP BY session_id"
        ).fetchall()
    }
    reported = {
        r["session_id"]: r["reported_cost_micros"]
        for r in conn.execute("SELECT session_id, reported_cost_micros FROM usage_sessions")
    }
    markers: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT session_id, ts, todo_id, kind FROM usage_markers ORDER BY ts"
    ).fetchall():
        markers.setdefault(r["session_id"], []).append(
            {"ts": r["ts"], "todo_id": r["todo_id"], "kind": r["kind"]}
        )
    result = []
    for row in by_session:
        sid = row["key"]
        m = meta.get(sid)
        result.append({
            "session_id": sid,
            "source": m["source"] if m else None,
            "machine_id": m["machine_id"] if m else None,
            "first_ts": m["first_ts"] if m else None,
            "last_ts": m["last_ts"] if m else None,
            "models": sorted((m["models"] or "").split(",")) if m else [],
            "reported_cost_micros": reported.get(sid),
            "markers": markers.get(sid, []),
            **{k: v for k, v in row.items() if k != "key"},
        })
    result.sort(key=lambda s: s["last_ts"] or "", reverse=True)
    return result


def _share_bp(part: int, whole: int) -> int:
    return (part * 10_000 + whole // 2) // whole if whole else 0


def _empty_bucket() -> dict[str, int]:
    return {"events": 0, "unpriced_events": 0, "estimate_micros": 0, **{f: 0 for f in COUNT_FIELDS}}


def _finish(bucket: dict[str, int]) -> dict[str, Any]:
    return {**bucket, "estimate_usd": micros_to_usd(bucket["estimate_micros"])}


# ---------------------------------------------------------------------------
# Attribution (derived at query time; never stored on events)
# ---------------------------------------------------------------------------
#
# Precedence per event:
#   1. manual assignment of its session (project and/or todo)
#   2. todo marker: the latest marker at or before the event in its session;
#      events up to MARKER_LEAD before the first marker also belong to it (the
#      agent reads context before setting `running`). Earlier events in a long
#      session were other work and fall through to the cwd rule.
#   3. cwd inside a project's git_repo/paths (longest root wins; "umbrella"
#      roots that contain another project's root are ignored)
#   4. unattributed

METHODS = ("manual", "todo", "cwd", "unattributed")
MARKER_LEAD = timedelta(minutes=30)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _norm_root(path: str | None) -> str | None:
    if not path or not isinstance(path, str):
        return None
    path = path.strip()
    if not (path.startswith("/") or path.startswith("~")):
        return None  # relative paths have no stable meaning for a cwd match
    return os.path.normpath(os.path.expanduser(path))


class _Attributor:
    def __init__(self, conn):
        self.todo_project: dict[int, int] = {
            r["id"]: r["project_id"]
            for r in conn.execute("SELECT id, project_id FROM todos").fetchall()
        }
        self.project_names: dict[int, str] = {
            r["id"]: r["name"]
            for r in conn.execute("SELECT id, name FROM projects").fetchall()
        }
        roots: list[tuple[str, int]] = []
        for r in conn.execute(
            "SELECT id, git_repo, paths_json FROM projects WHERE archived = 0"
        ).fetchall():
            try:
                paths = json.loads(r["paths_json"] or "[]")
            except json.JSONDecodeError:
                paths = []
            for raw in [r["git_repo"], *(paths if isinstance(paths, list) else [])]:
                root = _norm_root(raw)
                if root and root != "/":
                    roots.append((root, r["id"]))
        umbrella = {
            root for root, pid in roots
            if any(o != root and o.startswith(root + os.sep) and opid != pid for o, opid in roots)
        }
        self.roots = sorted(
            ((r, p) for r, p in set(roots) if r not in umbrella), key=lambda x: -len(x[0])
        )
        self.markers: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for r in conn.execute(
            "SELECT source, session_id, ts, todo_id FROM usage_markers ORDER BY ts"
        ).fetchall():
            if r["todo_id"] in self.todo_project:
                self.markers.setdefault((r["source"], r["session_id"]), []).append(
                    (r["ts"], r["todo_id"])
                )
        self.assignments: dict[tuple[str, str], tuple[int | None, int | None]] = {}
        for r in conn.execute("SELECT * FROM usage_assignments").fetchall():
            todo_id = r["todo_id"] if r["todo_id"] in self.todo_project else None
            project_id = self.todo_project.get(todo_id) if todo_id else r["project_id"]
            if project_id in self.project_names:
                self.assignments[(r["source"], r["session_id"])] = (project_id, todo_id)
        self._cwd_cache: dict[str | None, int | None] = {}

    def _by_cwd(self, cwd: str | None) -> int | None:
        if cwd not in self._cwd_cache:
            found = None
            norm = _norm_root(cwd)
            if norm:
                for root, pid in self.roots:
                    if norm == root or norm.startswith(root + os.sep):
                        found = pid
                        break
            self._cwd_cache[cwd] = found
        return self._cwd_cache[cwd]

    def attribute(self, source: str, session_id: str | None, ts: str, cwd: str | None):
        """Return (project_id, todo_id, method) for one event."""
        key = (source, session_id or "")
        if key in self.assignments:
            project_id, todo_id = self.assignments[key]
            return project_id, todo_id, "manual"
        marks = self.markers.get(key)
        if marks:
            todo_id = None
            for mts, tid in marks:
                if mts <= ts:
                    todo_id = tid
                else:
                    break
            if todo_id is None:
                first_ts, first_tid = marks[0]
                start, event = _parse_ts(first_ts), _parse_ts(ts)
                if start and event and start - event <= MARKER_LEAD:
                    todo_id = first_tid
            if todo_id is not None:
                return self.todo_project[todo_id], todo_id, "todo"
        project_id = self._by_cwd(cwd)
        if project_id is not None:
            return project_id, None, "cwd"
        return None, None, "unattributed"


def _date_filter(since: str | None, until: str | None, offset: str):
    where, params = ["1=1"], {"offset": offset}
    if since:
        where.append("date(ts, :offset) >= date(:since)")
        params["since"] = since
    if until:
        where.append("date(ts, :offset) <= date(:until)")
        params["until"] = until
    return " AND ".join(where), params


def _attributed_groups(conn, since, until, tz_offset_minutes):
    """Return an attributor plus the raw event rows in the date range."""
    attributor = _Attributor(conn)
    where, params = _date_filter(since, until, f"{-tz_offset_minutes} minutes")
    cols = ", ".join(COUNT_FIELDS)
    rows = conn.execute(
        f"SELECT source, session_id, ts, date(ts, :offset) AS day, cwd, provider, model, "
        f"COALESCE(speed, 'standard') AS speed, {cols} FROM usage_events WHERE {where}",
        params,
    ).fetchall()
    return attributor, rows


def _accumulate(groups: dict, bucket_key, r) -> None:
    k = (bucket_key, r["provider"], r["model"], r["speed"])
    g = groups.setdefault(k, {f: 0 for f in COUNT_FIELDS} | {"events": 0})
    g["events"] += 1
    for f in COUNT_FIELDS:
        g[f] += r[f] or 0


def _price_groups(pricing, groups: dict) -> dict[Any, dict[str, int]]:
    """Collapse (key, provider, model, speed) token sums into priced buckets."""
    out: dict[Any, dict[str, int]] = {}
    for (key, provider, model, speed), tokens in groups.items():
        estimate = price_group(pricing, provider, model, speed, tokens)
        b = out.setdefault(key, _empty_bucket())
        b["events"] += tokens["events"]
        for f in COUNT_FIELDS:
            b[f] += tokens[f]
        if estimate is None:
            b["unpriced_events"] += tokens["events"]
        else:
            b["estimate_micros"] += estimate
    return out


def by_project(
    since: str | None = None, until: str | None = None, tz_offset_minutes: int = 0,
    conn=None, pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Usage and API-equivalent estimates per project, per local day."""
    conn = conn or db.get_conn()
    pricing = pricing or load_pricing()
    attributor, rows = _attributed_groups(conn, since, until, tz_offset_minutes)
    per_project: dict = {}
    per_day: dict = {}
    per_method: dict = {}
    per_source: dict = {}
    per_source_day: dict = {}
    for r in rows:
        pid, _tid, method = attributor.attribute(r["source"], r["session_id"], r["ts"], r["cwd"])
        _accumulate(per_project, pid, r)
        _accumulate(per_day, (pid, r["day"]), r)
        _accumulate(per_method, method, r)
        _accumulate(per_source, (pid, r["source"]), r)
        _accumulate(per_source_day, (pid, r["source"], r["day"]), r)
    projects = _price_groups(pricing, per_project)
    days = _price_groups(pricing, per_day)
    methods = _price_groups(pricing, per_method)
    sources = _price_groups(pricing, per_source)
    total = _empty_bucket()
    for b in projects.values():
        for k in total:
            total[k] += b[k]
    grand = total["estimate_micros"]
    out_projects = []
    for pid, bucket in projects.items():
        by_day = {
            day: b["estimate_micros"] for (p, day), b in days.items() if p == pid
        }
        by_source = {
            src: b["estimate_micros"] for (p, src), b in sources.items() if p == pid
        }
        out_projects.append({
            "project_id": pid,
            "name": attributor.project_names.get(pid) if pid is not None else None,
            **_finish(bucket),
            # Share of all priced usage in range, in basis points (1% = 100).
            "share_bp": _share_bp(bucket["estimate_micros"], grand),
            "by_source": {
                src: {"estimate_micros": m, "share_bp": _share_bp(m, grand)}
                for src, m in sorted(by_source.items())
            },
            "by_day": dict(sorted(by_day.items())),
        })
    out_projects.sort(key=lambda p: (p["project_id"] is None, -p["estimate_micros"]))
    source_totals: dict[str, int] = {}
    for (_p, src), b in sources.items():
        source_totals[src] = source_totals.get(src, 0) + b["estimate_micros"]

    # Actual billing allocated over the same local-date range.
    from datetime import datetime as _dt, timezone as _tz
    today = (_dt.now(_tz.utc) - timedelta(minutes=tz_offset_minutes)).date()
    usage_by_day = {k: b["estimate_micros"] for k, b in _price_groups(pricing, per_source_day).items()}
    first_day = min((k[2] for k in usage_by_day), default=today.isoformat())
    range_start = _d(since) if since else _d(first_day)
    range_end = min(_d(until), today) if until else today
    alloc = allocate_billing(usage_by_day, range_start, range_end, conn)
    for p in out_projects:
        parts = alloc["per_project"].get(p["project_id"], {})
        p["allocated_micros"] = sum(parts.values())
        p["allocated_usd"] = micros_to_usd(p["allocated_micros"])
        p["allocated_by_source"] = dict(sorted(parts.items()))
    billing = {
        "configured": alloc["configured"],
        "range": [range_start.isoformat(), range_end.isoformat()],
        "by_source": {
            src: {**v, "billed_usd": micros_to_usd(v["billed_micros"]),
                  "allocated_usd": micros_to_usd(v["allocated_micros"]),
                  "unallocated_usd": micros_to_usd(v["unallocated_micros"])}
            for src, v in sorted(alloc["per_source"].items())
        },
    }
    billing["billed_micros"] = sum(v["billed_micros"] for v in alloc["per_source"].values())
    billing["allocated_micros"] = sum(v["allocated_micros"] for v in alloc["per_source"].values())
    billing["unallocated_micros"] = billing["billed_micros"] - billing["allocated_micros"]
    return {
        "since": since,
        "until": until,
        "pricing_basis": "api_equivalent_list_price",
        "totals": _finish(total),
        "by_source": {
            src: {"estimate_micros": m, "share_bp": _share_bp(m, grand)}
            for src, m in sorted(source_totals.items())
        },
        "projects": out_projects,
        "billing": billing,
        "by_method": {m: _finish(methods.get(m, _empty_bucket())) for m in METHODS},
    }


def for_todo(
    todo_id: int, conn=None, pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Usage attributed to a todo and its descendants (all time)."""
    conn = conn or db.get_conn()
    pricing = pricing or load_pricing()
    family = {todo_id}
    frontier = [todo_id]
    while frontier:
        q = ",".join("?" * len(frontier))
        frontier = [
            r["id"] for r in conn.execute(
                f"SELECT id FROM todos WHERE parent_id IN ({q})", frontier
            ).fetchall() if r["id"] not in family
        ]
        family.update(frontier)
    attributor, rows = _attributed_groups(conn, None, None, 0)
    groups: dict = {}
    sessions_seen: set = set()
    last_ts = None
    for r in rows:
        _pid, tid, _m = attributor.attribute(r["source"], r["session_id"], r["ts"], r["cwd"])
        if tid not in family:
            continue
        _accumulate(groups, "todo", r)
        sessions_seen.add((r["source"], r["session_id"]))
        last_ts = max(last_ts or r["ts"], r["ts"])
    bucket = _price_groups(pricing, groups).get("todo", _empty_bucket())
    return {
        "todo_id": todo_id,
        "includes_subtasks": len(family) > 1,
        "sessions": len(sessions_seen),
        "last_used": last_ts,
        "pricing_basis": "api_equivalent_list_price",
        **_finish(bucket),
    }


def unattributed_sessions(
    since: str | None = None, tz_offset_minutes: int = 0, conn=None,
    pricing: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Sessions with usage that no rule could attribute, newest first."""
    conn = conn or db.get_conn()
    pricing = pricing or load_pricing()
    attributor, rows = _attributed_groups(conn, since, None, tz_offset_minutes)
    groups: dict = {}
    meta: dict = {}
    for r in rows:
        pid, _tid, _m = attributor.attribute(r["source"], r["session_id"], r["ts"], r["cwd"])
        if pid is not None:
            continue
        key = (r["source"], r["session_id"])
        _accumulate(groups, key, r)
        m = meta.setdefault(key, {"first_ts": r["ts"], "last_ts": r["ts"], "cwds": set()})
        m["first_ts"] = min(m["first_ts"], r["ts"])
        m["last_ts"] = max(m["last_ts"], r["ts"])
        if r["cwd"]:
            m["cwds"].add(r["cwd"])
    out = []
    for key, bucket in _price_groups(pricing, groups).items():
        m = meta[key]
        out.append({
            "source": key[0], "session_id": key[1],
            "first_ts": m["first_ts"], "last_ts": m["last_ts"],
            "cwds": sorted(m["cwds"]),
            **_finish(bucket),
        })
    out.sort(key=lambda s: s["last_ts"], reverse=True)
    return out


def assign_session(
    source: str, session_id: str, project_id: int | None, todo_id: int | None, conn=None,
) -> None:
    """Manually attribute a whole session; both None clears the assignment."""
    conn = conn or db.get_conn()
    if project_id is None and todo_id is None:
        conn.execute(
            "DELETE FROM usage_assignments WHERE source = ? AND session_id = ?",
            (source, session_id),
        )
        return
    conn.execute(
        "INSERT INTO usage_assignments (source, session_id, project_id, todo_id, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, session_id) DO UPDATE SET "
        "project_id = excluded.project_id, todo_id = excluded.todo_id, "
        "updated_at = excluded.updated_at",
        (source, session_id, project_id, todo_id, _now()),
    )


# ---------------------------------------------------------------------------
# Actual billing: what the user pays, allocated to projects by usage
# ---------------------------------------------------------------------------
#
# Each charge pays for one tool (`source`). For a date range, a monthly
# subscription contributes amount × covered_days / days_in_month for every
# calendar month it overlaps; a one-off charge contributes its amount
# prorated over its own period. Each such segment is split across projects
# by that tool's API-equivalent usage inside the segment. A segment with no
# recorded usage for its tool stays unallocated rather than being spread
# over other tools' projects.

BILLING_KINDS = ("monthly", "one_off")


def list_billing(conn=None) -> list[dict[str, Any]]:
    conn = conn or db.get_conn()
    return [
        {**dict(r), "amount_usd": micros_to_usd(r["amount_micros"])}
        for r in conn.execute("SELECT * FROM usage_billing ORDER BY source, start_date, id")
    ]


def add_billing(data: dict[str, Any], conn=None) -> dict[str, Any]:
    conn = conn or db.get_conn()
    cur = conn.execute(
        "INSERT INTO usage_billing (source, label, kind, amount_micros, start_date, end_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (data["source"], data["label"], data["kind"], int(data["amount_micros"]),
         data["start_date"], data.get("end_date"), _now()),
    )
    return next(b for b in list_billing(conn) if b["id"] == cur.lastrowid)


def update_billing(billing_id: int, data: dict[str, Any], conn=None) -> dict[str, Any] | None:
    conn = conn or db.get_conn()
    fields = [k for k in ("source", "label", "kind", "amount_micros", "start_date", "end_date") if k in data]
    if fields:
        conn.execute(
            f"UPDATE usage_billing SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
            (*[data[k] for k in fields], billing_id),
        )
    return next((b for b in list_billing(conn) if b["id"] == billing_id), None)


def delete_billing(billing_id: int, conn=None) -> bool:
    conn = conn or db.get_conn()
    return conn.execute("DELETE FROM usage_billing WHERE id = ?", (billing_id,)).rowcount > 0


def _d(iso: str):
    from datetime import date

    return date.fromisoformat(iso)


def _days_in_month(day) -> int:
    import calendar

    return calendar.monthrange(day.year, day.month)[1]


def _billing_segments(charge: dict[str, Any], start, end) -> list[tuple[Any, Any, int]]:
    """(first_day, last_day, amount_micros) pieces of a charge inside [start, end]."""
    lo = max(start, _d(charge["start_date"]))
    hi = min(end, _d(charge["end_date"])) if charge["end_date"] else end
    if lo > hi:
        return []
    amount = charge["amount_micros"]
    if charge["kind"] == "one_off":
        period = (_d(charge["end_date"] or charge["start_date"]) - _d(charge["start_date"])).days + 1
        covered = (hi - lo).days + 1
        return [(lo, hi, (amount * covered + period // 2) // period)]
    segments = []
    cursor = lo
    while cursor <= hi:
        dim = _days_in_month(cursor)
        month_end = cursor.replace(day=dim)
        seg_end = min(month_end, hi)
        covered = (seg_end - cursor).days + 1
        segments.append((cursor, seg_end, (amount * covered + dim // 2) // dim))
        cursor = seg_end + timedelta(days=1)
    return segments


def _split(amount: int, weights: dict[Any, int]) -> dict[Any, int]:
    """Largest-remainder split of integer micros; parts sum exactly to amount."""
    total = sum(weights.values())
    shares = {k: amount * w // total for k, w in weights.items()}
    leftover = amount - sum(shares.values())
    by_remainder = sorted(weights, key=lambda k: (amount * weights[k]) % total, reverse=True)
    for k in by_remainder[:leftover]:
        shares[k] += 1
    return shares


def allocate_billing(
    usage_by_day: dict[tuple[Any, str, str], int], start, end, conn=None,
) -> dict[str, Any]:
    """Allocate billed amounts in [start, end] to projects.

    `usage_by_day` maps (project_id, source, ISO day) → API-equivalent micros.
    Returns per-project allocations, per-source billed/allocated/unallocated
    totals, and whether any billing is configured at all.
    """
    conn = conn or db.get_conn()
    charges = list_billing(conn)
    per_project: dict[Any, dict[str, int]] = {}
    per_source: dict[str, dict[str, int]] = {}
    for charge in charges:
        src = charge["source"]
        bucket = per_source.setdefault(src, {"billed_micros": 0, "allocated_micros": 0, "unallocated_micros": 0})
        for lo, hi, amount in _billing_segments(charge, start, end):
            bucket["billed_micros"] += amount
            lo_s, hi_s = lo.isoformat(), hi.isoformat()
            weights: dict[Any, int] = {}
            for (pid, usrc, day), micros in usage_by_day.items():
                if usrc == src and lo_s <= day <= hi_s and micros > 0:
                    weights[pid] = weights.get(pid, 0) + micros
            if not weights:
                bucket["unallocated_micros"] += amount
                continue
            bucket["allocated_micros"] += amount
            for pid, part in _split(amount, weights).items():
                pp = per_project.setdefault(pid, {})
                pp[src] = pp.get(src, 0) + part
    return {"configured": bool(charges), "per_project": per_project, "per_source": per_source}


def iter_batches(found: dict[str, list], chunk: int = MAX_EVENTS_PER_BATCH) -> Iterable[dict[str, list]]:
    """Split a large local scan into ingestible pieces."""
    events = found.get("events", [])
    if not events:
        yield found
        return
    for i in range(0, len(events), chunk):
        part = {"events": events[i : i + chunk], "sessions": [], "markers": []}
        if i == 0:
            part["sessions"] = found.get("sessions", [])
            part["markers"] = found.get("markers", [])
        yield part
