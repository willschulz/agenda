import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pydantic

import main
import usage_collect
import usage_ledger

PRICING = {
    "models": {
        "anthropic/claude-opus-5-5": {
            "input": "4.00", "output": "20.00", "cache_read": "0.20",
            "cache_write_5m": "5.00", "cache_write_1h": "8.00", "speed": {"fast": "2"},
        },
        "anthropic/claude-haiku-4-5": {
            "input": "1.00", "output": "5.00", "cache_read": None,
            "cache_write_5m": "1.25", "cache_write_1h": "2.00",
        },
    }
}


def _assistant(msg_id, req, ts, content, usage=None, model="claude-opus-5-5", sid="s1"):
    return {
        "type": "assistant", "sessionId": sid, "timestamp": ts, "requestId": req,
        "cwd": "/work/proj", "gitBranch": "main", "version": "2.1.0",
        "message": {
            "id": msg_id, "model": model, "content": content,
            "usage": usage or {
                "input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 500,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 500},
                "output_tokens_details": {"thinking_tokens": 40},
                "server_tool_use": {"web_search_requests": 1}, "speed": "standard",
            },
        },
    }


def _user(ts, content, sid="s1"):
    return {"type": "user", "sessionId": sid, "timestamp": ts, "message": {"role": "user", "content": content}}


def _write(path: Path, records, trailing_partial=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
        fh.write(trailing_partial)


def _ledger_conn():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    usage_ledger.ensure_schema(conn)
    return conn


class ClaudeCodeCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.file = self.root / "-work-proj" / "s1.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedupes_content_blocks_and_skips_synthetic(self):
        _write(self.file, [
            _assistant("m1", "r1", "2026-10-01T10:00:00Z", [{"type": "thinking", "thinking": ""}]),
            _assistant("m1", "r1", "2026-10-01T10:00:00Z", [{"type": "text", "text": "hi"}]),
            _assistant("m2", "r2", "2026-10-01T10:01:00Z", [{"type": "text", "text": "x"}],
                       model="<synthetic>"),
        ])
        found = usage_collect.collect_claude_code({}, self.root)
        self.assertEqual(len(found["events"]), 1)
        ev = found["events"][0]
        self.assertEqual(ev["event_key"], "m1:r1")
        self.assertEqual(ev["cache_write_1h_tokens"], 500)
        self.assertEqual(ev["reasoning_tokens"], 40)
        self.assertNotIn("content", ev)

    def test_only_strong_todo_markers(self):
        patch_cmd = ("curl -s -X PATCH -H 'Content-Type: application/json' "
                     "-d '{\"running\": true}' http://127.0.0.1:8552/api/todos/42")
        _write(self.file, [
            _user("2026-10-01T09:00:00Z", "context\ntodo_id: 7\nproject_id: 3"),
            _assistant("m1", "r1", "2026-10-01T10:00:00Z",
                       [{"type": "tool_use", "name": "Bash", "input": {"command": patch_cmd}}]),
            _assistant("m2", "r2", "2026-10-01T10:05:00Z",
                       [{"type": "tool_use", "name": "Bash", "input": {
                           "command": "curl -d '{\"running\": false}' http://x/api/todos/42"}}]),
            # File contents written by the agent are not executed commands.
            _assistant("m3", "r3", "2026-10-01T10:05:30Z",
                       [{"type": "tool_use", "name": "Write", "input": {
                           "file_path": "t.py", "content": patch_cmd.replace("42", "77")}}]),
            # A passing mention inside a tool result is not a marker.
            _user("2026-10-01T10:06:00Z", [{"type": "tool_result", "content": "todo_id: 99 /api/todos/99 running: true"}]),
        ])
        found = usage_collect.collect_claude_code({}, self.root)
        got = sorted((m["todo_id"], m["kind"]) for m in found["markers"])
        self.assertEqual(got, [(7, "copy_block"), (42, "running")])

    def test_incremental_scan_waits_for_complete_lines(self):
        line = json.dumps(_assistant("m2", "r2", "2026-10-01T11:00:00Z", []))
        _write(self.file, [_assistant("m1", "r1", "2026-10-01T10:00:00Z", [])], trailing_partial=line[:20])
        state = {}
        first = usage_collect.collect_claude_code(state, self.root)
        self.assertEqual([e["event_key"] for e in first["events"]], ["m1:r1"])
        with open(self.file, "a") as fh:
            fh.write(line[20:] + "\n")
        second = usage_collect.collect_claude_code(state, self.root)
        self.assertEqual([e["event_key"] for e in second["events"]], ["m2:r2"])
        self.assertEqual(usage_collect.collect_claude_code(state, self.root)["events"], [])

    def test_cost_state_becomes_session_report(self):
        _write(self.file, [{
            "type": "cost-state", "sessionId": "s1", "totalCostUSD": 1.5,
            "modelUsage": {"claude-opus-5-5": {"inputTokens": 1, "outputTokens": 2,
                                               "cacheReadInputTokens": 3,
                                               "cacheCreationInputTokens": 4, "costUSD": 1.5}},
        }])
        found = usage_collect.collect_claude_code({}, self.root)
        self.assertEqual(found["sessions"][0]["reported_cost_usd"], 1.5)
        self.assertEqual(found["sessions"][0]["reported_model_usage"]["claude-opus-5-5"]["cache_write_tokens"], 4)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = _ledger_conn()
        self.addCleanup(self.conn.close)

    def _batch(self, events, **kw):
        return {"format_version": 1, "source": "claude_code", "machine_id": "mac-1",
                "machine_label": "test", "events": events, "sessions": [], "markers": [], **kw}

    def _event(self, key, ts="2026-10-01T10:00:00Z", **kw):
        base = {"event_key": key, "ts": ts, "session_id": "s1", "model": "claude-opus-5-5",
                "provider": "anthropic", "input_tokens": 10, "output_tokens": 100,
                "cache_read_tokens": 1000, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 500}
        base.update(kw)
        return base

    def test_ingest_is_idempotent(self):
        batch = self._batch([self._event("a"), self._event("b")],
                            markers=[{"session_id": "s1", "ts": "2026-10-01T10:00:00Z",
                                      "todo_id": 5, "kind": "running"}])
        first = usage_ledger.ingest(batch, self.conn)
        again = usage_ledger.ingest(batch, self.conn)
        self.assertEqual((first["inserted"], first["duplicates"], first["markers_inserted"]), (2, 0, 1))
        self.assertEqual((again["inserted"], again["duplicates"], again["markers_inserted"]), (0, 2, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 2)

    def test_price_is_exact_integer_micros(self):
        # 10*4 + 100*20 + 1000*0.20 + 500*8 = 6240 micro-USD
        usage_ledger.ingest(self._batch([self._event("a")]), self.conn)
        result = usage_ledger.summary("model", conn=self.conn, pricing=PRICING)
        self.assertEqual(result["totals"]["estimate_micros"], 6240)
        self.assertEqual(result["totals"]["unpriced_events"], 0)

    def test_dated_model_ids_and_fast_speed(self):
        usage_ledger.ingest(self._batch([
            self._event("a", model="claude-opus-5-5-20260901", speed="fast"),
        ]), self.conn)
        result = usage_ledger.summary("model", conn=self.conn, pricing=PRICING)
        self.assertEqual(result["totals"]["estimate_micros"], 12480)

    def test_unknown_rate_is_unpriced_not_zero(self):
        usage_ledger.ingest(self._batch([
            self._event("a", model="claude-haiku-4-5-20251001"),  # cache_read rate unknown
            self._event("b", model="some-new-model"),
            self._event("c", model="claude-haiku-4-5", cache_read_tokens=0),
        ]), self.conn)
        result = usage_ledger.summary("model", conn=self.conn, pricing=PRICING)
        totals = result["totals"]
        self.assertEqual(totals["unpriced_events"], 2)
        # c: 10*1 + 100*5 + 500*2 = 1510
        self.assertEqual(totals["estimate_micros"], 1510)
        self.assertEqual(result["unpriced_models"],
                         ["anthropic/claude-haiku-4-5", "anthropic/some-new-model"])

    def test_day_grouping_uses_local_timezone(self):
        usage_ledger.ingest(self._batch([self._event("a", ts="2026-10-02T02:00:00Z")]), self.conn)
        result = usage_ledger.summary("day", tz_offset_minutes=240, conn=self.conn, pricing=PRICING)
        self.assertEqual([r["key"] for r in result["rows"]], ["2026-10-01"])

    def test_sessions_report_markers_and_reported_cost(self):
        usage_ledger.ingest(self._batch(
            [self._event("a")],
            sessions=[{"session_id": "s1", "reported_cost_usd": 0.01, "reported_model_usage": {}}],
            markers=[{"session_id": "s1", "ts": "2026-10-01T09:00:00Z", "todo_id": 5, "kind": "copy_block"}],
        ), self.conn)
        [s] = usage_ledger.sessions(conn=self.conn, pricing=PRICING)
        self.assertEqual(s["reported_cost_micros"], 10000)
        self.assertEqual(s["estimate_micros"], 6240)
        self.assertEqual(s["markers"][0]["todo_id"], 5)

    def test_config_defaults_to_disabled(self):
        self.assertFalse(usage_ledger.get_config(self.conn)["enabled"])
        self.assertTrue(usage_ledger.patch_config({"enabled": True}, self.conn)["enabled"])


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.conn = _ledger_conn()
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, git_repo TEXT,
                                   paths_json TEXT NOT NULL DEFAULT '[]',
                                   archived INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE todos (id INTEGER PRIMARY KEY, project_id INTEGER, parent_id INTEGER);
            INSERT INTO projects VALUES (1, 'Workspace', '/work', '[]', 0);
            INSERT INTO projects VALUES (2, 'App', '/work/app', '["~/notes/app"]', 0);
            INSERT INTO projects VALUES (3, 'Paper', NULL, '["relative/dir"]', 0);
            INSERT INTO todos VALUES (10, 2, NULL), (11, 2, 10), (20, 3, NULL);
        """)

    def _ingest(self, events, markers=()):
        usage_ledger.ingest({
            "format_version": 1, "source": "claude_code", "machine_id": "m",
            "events": events, "sessions": [], "markers": list(markers),
        }, self.conn)

    def _ev(self, key, ts, session="s1", cwd="/elsewhere"):
        return {"event_key": key, "ts": ts, "session_id": session, "model": "claude-opus-5-5",
                "provider": "anthropic", "cwd": cwd, "output_tokens": 1000}  # 20000 micros

    def _projects(self):
        res = usage_ledger.by_project(conn=self.conn, pricing=PRICING)
        return {p["project_id"]: p["estimate_micros"] for p in res["projects"]}, res

    def test_markers_window_by_time_and_backfill_session_start(self):
        self._ingest(
            [self._ev("a", "2026-10-01T09:45:00Z"),   # 15 min before first marker -> todo 10
             self._ev("b", "2026-10-01T10:30:00Z"),   # after marker 10
             self._ev("c", "2026-10-01T12:00:00Z")],  # after marker 20
            markers=[
                {"session_id": "s1", "ts": "2026-10-01T10:00:00Z", "todo_id": 10, "kind": "running"},
                {"session_id": "s1", "ts": "2026-10-01T11:00:00Z", "todo_id": 20, "kind": "running"},
            ],
        )
        by_pid, res = self._projects()
        self.assertEqual(by_pid, {2: 40000, 3: 20000})
        self.assertEqual(res["by_method"]["todo"]["estimate_micros"], 60000)

    def test_long_session_work_before_first_marker_is_not_backfilled(self):
        # Shape of a real session: a day of other work, then a todo marked running.
        self._ingest(
            [self._ev("a", "2026-10-10T17:00:00Z", cwd="/work/app/src"),  # day before -> cwd
             self._ev("b", "2026-10-10T18:00:00Z", cwd="/elsewhere"),     # day before -> unattributed
             self._ev("c", "2026-10-11T16:50:00Z"),                       # 20 min before -> todo 20
             self._ev("d", "2026-10-11T17:30:00Z")],                      # after -> todo 20
            markers=[{"session_id": "s1", "ts": "2026-10-11T17:10:00Z", "todo_id": 20, "kind": "running"}],
        )
        by_pid, res = self._projects()
        self.assertEqual(by_pid, {3: 40000, 2: 20000, None: 20000})
        self.assertEqual(res["by_method"]["todo"]["events"], 2)

    def test_shares_split_by_source_and_sum_with_unattributed(self):
        self._ingest([self._ev("a", "2026-10-01T09:00:00Z", "s1", "/work/app")])
        usage_ledger.ingest({
            "format_version": 1, "source": "cursor", "machine_id": "m",
            "events": [self._ev("b", "2026-10-01T09:00:00Z", "c1", "/work/app"),
                       self._ev("c", "2026-10-01T09:00:00Z", "c2", "/nowhere")],
            "sessions": [], "markers": [],
        }, self.conn)
        res = usage_ledger.by_project(conn=self.conn, pricing=PRICING)
        app = next(p for p in res["projects"] if p["project_id"] == 2)
        none = next(p for p in res["projects"] if p["project_id"] is None)
        self.assertEqual(app["share_bp"], 6667)
        self.assertEqual({k: v["share_bp"] for k, v in app["by_source"].items()},
                         {"claude_code": 3333, "cursor": 3333})
        self.assertEqual(none["share_bp"], 3333)
        self.assertEqual(res["by_source"]["cursor"]["share_bp"], 6667)

    def test_cwd_longest_root_wins_and_umbrella_roots_are_ignored(self):
        self._ingest([
            self._ev("a", "2026-10-01T09:00:00Z", "s1", "/work/app/src"),
            self._ev("b", "2026-10-01T09:00:00Z", "s2", "/work"),          # umbrella -> unattributed
            self._ev("c", "2026-10-01T09:00:00Z", "s3", "/work/appendix"),  # not inside /work/app
        ])
        by_pid, res = self._projects()
        self.assertEqual(by_pid, {2: 20000, None: 40000})
        self.assertEqual(res["by_method"]["cwd"]["events"], 1)

    def test_manual_assignment_overrides_markers_and_can_be_cleared(self):
        self._ingest([self._ev("a", "2026-10-01T10:30:00Z")],
                     markers=[{"session_id": "s1", "ts": "2026-10-01T10:00:00Z",
                               "todo_id": 10, "kind": "running"}])
        usage_ledger.assign_session("claude_code", "s1", 3, None, self.conn)
        self.assertEqual(self._projects()[0], {3: 20000})
        usage_ledger.assign_session("claude_code", "s1", None, None, self.conn)
        self.assertEqual(self._projects()[0], {2: 20000})

    def test_todo_cost_includes_subtasks(self):
        self._ingest([self._ev("a", "2026-10-01T10:30:00Z")],
                     markers=[{"session_id": "s1", "ts": "2026-10-01T10:00:00Z",
                               "todo_id": 11, "kind": "running"}])
        parent = usage_ledger.for_todo(10, self.conn, PRICING)
        self.assertEqual((parent["estimate_micros"], parent["sessions"], parent["includes_subtasks"]),
                         (20000, 1, True))
        self.assertEqual(usage_ledger.for_todo(20, self.conn, PRICING)["estimate_micros"], 0)

    def test_unattributed_sessions_list(self):
        self._ingest([self._ev("a", "2026-10-01T09:00:00Z", "lost", "/tmp/x")])
        [s] = usage_ledger.unattributed_sessions(conn=self.conn, pricing=PRICING)
        self.assertEqual((s["session_id"], s["cwds"], s["estimate_micros"]), ("lost", ["/tmp/x"], 20000))


class BillingAllocationTests(unittest.TestCase):
    def setUp(self):
        self.conn = _ledger_conn()
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, git_repo TEXT,
                                   paths_json TEXT NOT NULL DEFAULT '[]',
                                   archived INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE todos (id INTEGER PRIMARY KEY, project_id INTEGER, parent_id INTEGER);
            INSERT INTO projects VALUES (1, 'A', '/a', '[]', 0), (2, 'B', '/b', '[]', 0);
        """)

    def _ev(self, key, day, cwd, out=1000):
        return {"event_key": key, "ts": f"{day}T12:00:00Z", "session_id": key, "model": "claude-opus-5-5",
                "provider": "anthropic", "cwd": cwd, "output_tokens": out}

    def _ingest(self, source, events):
        usage_ledger.ingest({"format_version": 1, "source": source, "machine_id": "m",
                             "events": events, "sessions": [], "markers": []}, self.conn)

    def _charge(self, **kw):
        row = {"source": "claude_code", "label": "Claude", "kind": "monthly",
               "amount_micros": 20_000_000, "start_date": "2026-01-01", "end_date": None}
        row.update(kw)
        return usage_ledger.add_billing(row, self.conn)

    def _alloc(self, since, until):
        return usage_ledger.by_project(since, until, conn=self.conn, pricing=PRICING)

    def test_monthly_charge_prorates_by_day_and_splits_by_usage(self):
        self._ingest("claude_code", [self._ev("a", "2026-09-10", "/a", 3000),
                                     self._ev("b", "2026-09-12", "/b", 1000)])
        self._charge()
        res = self._alloc("2026-09-01", "2026-09-15")  # 15 of 30 days -> $10
        alloc = {p["project_id"]: p["allocated_micros"] for p in res["projects"]}
        self.assertEqual(alloc, {1: 7_500_000, 2: 2_500_000})
        self.assertEqual(res["billing"]["billed_micros"], 10_000_000)
        self.assertEqual(res["billing"]["unallocated_micros"], 0)

    def test_charge_without_usage_for_its_tool_stays_unallocated(self):
        self._ingest("claude_code", [self._ev("a", "2026-09-10", "/a")])
        self._charge(source="cursor", label="Cursor", amount_micros=60_000_000)
        res = self._alloc("2026-09-01", "2026-09-30")
        self.assertEqual(res["billing"]["by_source"]["cursor"]["unallocated_micros"], 60_000_000)
        self.assertEqual(sum(p["allocated_micros"] for p in res["projects"]), 0)

    def test_month_boundaries_and_exact_sums(self):
        # 3 projects-worth of odd weights across Sep 20 - Oct 10 (11 + 10 days).
        self._ingest("claude_code", [self._ev("a", "2026-09-25", "/a", 1),
                                     self._ev("b", "2026-09-26", "/b", 2),
                                     self._ev("c", "2026-10-05", "/a", 7)])
        self._charge()
        res = self._alloc("2026-09-20", "2026-10-10")
        sep = (20_000_000 * 11 + 15) // 30
        octo = (20_000_000 * 10 + 15) // 31
        self.assertEqual(res["billing"]["billed_micros"], sep + octo)
        self.assertEqual(sum(p["allocated_micros"] for p in res["projects"]), sep + octo)

    def test_one_off_charge_and_end_date(self):
        self._ingest("cursor", [self._ev("a", "2026-09-10", "/a")])
        usage_ledger.add_billing({"source": "cursor", "label": "Overage", "kind": "one_off",
                                  "amount_micros": 9_000_000, "start_date": "2026-09-01",
                                  "end_date": "2026-09-30"}, self.conn)
        self._charge(source="cursor", end_date="2026-08-31")  # ended before range
        res = self._alloc("2026-09-01", "2026-09-10")  # 10 of 30 days of the overage
        self.assertEqual(res["billing"]["billed_micros"], 3_000_000)
        self.assertEqual(res["projects"][0]["allocated_by_source"], {"cursor": 3_000_000})


class BundledPricingTests(unittest.TestCase):
    def test_bundled_table_matches_checked_claude_code_totals(self):
        # Token counts from a real Claude Code cost-state record (costUSD 22.8746).
        pricing = usage_ledger.load_pricing(usage_ledger.BUNDLED_PRICING)
        micros = usage_ledger.price_group(pricing, "anthropic", "claude-opus-5-5", "standard", {
            "input_tokens": 4930, "output_tokens": 141584,
            "cache_read_tokens": 62099739, "cache_write_1h_tokens": 950403,
        })
        self.assertEqual(micros, 22874572)


class BatchValidationTests(unittest.TestCase):
    def test_rejects_negative_tokens_and_unknown_marker_kind(self):
        good = {"format_version": 1, "source": "cursor", "machine_id": "m",
                "events": [{"event_key": "k", "ts": "2026-10-01T00:00:00Z", "model": "x"}]}
        main.UsageBatchIn.model_validate(good)
        bad_tokens = json.loads(json.dumps(good))
        bad_tokens["events"][0]["output_tokens"] = -1
        with self.assertRaises(pydantic.ValidationError):
            main.UsageBatchIn.model_validate(bad_tokens)
        bad_marker = dict(good, markers=[{"session_id": "s", "ts": "2026-10-01T00:00:00Z",
                                          "todo_id": 1, "kind": "mentioned"}])
        with self.assertRaises(pydantic.ValidationError):
            main.UsageBatchIn.model_validate(bad_marker)


if __name__ == "__main__":
    unittest.main()
