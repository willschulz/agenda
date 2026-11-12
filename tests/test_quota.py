import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import usage_quota
import usage_statusline

H = 3600
NOW = 1_800_000_000.0  # fixed clock


def _conn():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    usage_quota.ensure_schema(conn)
    return conn


def _snap(session, windows, changed_at, seen_at=None):
    return {"provider": "claude", "source": "claude_statusline", "session_id": session,
            "windows": windows, "changed_at": changed_at, "seen_at": seen_at or changed_at}


def _five(used, resets_at):
    return {"five_hour": {"used_percentage": used, "resets_at": resets_at}}


class StatusLineBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"AGENDA_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, payload):
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out):
            usage_statusline.main()
        return out.getvalue().strip()

    def _files(self):
        folder = os.path.join(self.tmp.name, "quota")
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def test_missing_rate_limits_writes_nothing_and_still_renders(self):
        line = self._run({"session_id": "s1", "model": {"display_name": "Opus"}})
        self.assertEqual(line, "Opus")
        self.assertEqual(self._files(), [])

    def test_partial_windows_are_kept_partial(self):
        import time
        resets = int(time.time()) + 2 * H
        self._run({"session_id": "s1", "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": resets},
            "seven_day": {"used_percentage": None, "resets_at": resets}}})
        [name] = self._files()
        with open(os.path.join(self.tmp.name, "quota", name)) as fh:
            snap = json.load(fh)
        self.assertEqual(list(snap["windows"]), ["five_hour"])
        self.assertEqual(snap["windows"]["five_hour"]["used_percentage"], 23.5)

    def test_sessions_get_separate_files_and_unsafe_ids_are_sanitized(self):
        import time
        w = {"five_hour": {"used_percentage": 1, "resets_at": int(time.time()) + H}}
        self._run({"session_id": "a", "rate_limits": w})
        self._run({"session_id": "../b", "rate_limits": w})
        self.assertEqual(self._files(), ["claude_statusline-a.json", "claude_statusline-b.json"])


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def _state(self, now=NOW):
        st = usage_quota.state(self.conn, now=now)
        return {w["window"]: w for w in st["providers"]["claude"]}

    def test_unavailable_without_any_observation(self):
        self.assertEqual(self._state()["five_hour"]["status"], "unavailable")
        self.assertEqual(self._state()["seven_day"]["status"], "unavailable")

    def test_pace_classification_with_tolerance(self):
        reset = int(NOW + 2.5 * H)  # 5h window is exactly half elapsed
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(42, reset), NOW - 60))
        w = self._state()["five_hour"]
        self.assertEqual((w["elapsed_pct"], w["used_pct"], w["pace_delta"], w["pace"]),
                         (50.0, 42.0, -8.0, "under"))
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(53, reset), NOW - 30))
        self.assertEqual(self._state()["five_hour"]["pace"], "on")
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(70, reset), NOW - 10))
        self.assertEqual(self._state()["five_hour"]["pace"], "over")

    def test_target_fraction_scales_the_pace_line(self):
        reset = int(NOW + 2.5 * H)
        usage_quota.patch_config({"target_pct": 80}, self.conn)
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(46, reset), NOW - 60))
        w = self._state()["five_hour"]
        self.assertEqual((w["target_pct_now"], w["pace"]), (40.0, "over"))

    def test_stale_idle_session_cannot_lower_usage(self):
        reset = int(NOW + 2 * H)
        usage_quota.ingest_snapshot(self.conn, _snap("busy", _five(60, reset), NOW - 300))
        # An idle session re-reports its older, lower reading later.
        usage_quota.ingest_snapshot(self.conn, _snap("idle", _five(35, reset), NOW - 600, NOW - 5))
        w = self._state()["five_hour"]
        self.assertEqual(w["used_pct"], 60.0)
        # The idle re-report doesn't confirm 60%, so it doesn't refresh the age.
        self.assertEqual(w["age_seconds"], 300)

    def test_expired_window_is_inactive_not_current(self):
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(90, int(NOW - 60)), NOW - 4 * H))
        w = self._state()["five_hour"]
        self.assertEqual(w["status"], "inactive")
        self.assertNotIn("used_pct", w)

    def test_rollover_new_window_supersedes_old(self):
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(95, int(NOW - 60)), NOW - 2 * H))
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(3, int(NOW + 5 * H - 120)), NOW - 30))
        w = self._state()["five_hour"]
        self.assertEqual((w["status"], w["used_pct"]), ("fresh", 3.0))

    def test_staleness_follows_last_report(self):
        reset = int(NOW + 4 * H)
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(10, reset), NOW - 20 * 60))
        self.assertEqual(self._state()["five_hour"]["status"], "stale")

    def test_projection_uses_recent_history_and_predicts_exhaustion(self):
        reset = int(NOW + 3 * H)
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(20, reset), NOW - H))
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(50, reset), NOW - 60))
        p = self._state()["five_hour"]["projection"]
        # 30 points/hour from 50% with 3h left -> 140% by reset; 100% in 1h40m.
        self.assertEqual(p["projected_pct_at_reset"], 140.0)
        self.assertAlmostEqual(p["exhausts_at"], NOW + 100 * 60, delta=1)

    def test_weekly_projection_needs_hours_of_history(self):
        reset = int(NOW + 3 * 86400)
        week = lambda used: {"seven_day": {"used_percentage": used, "resets_at": reset}}
        usage_quota.ingest_snapshot(self.conn, _snap("s", week(68), NOW - 50 * 60))
        usage_quota.ingest_snapshot(self.conn, _snap("s", week(71), NOW - 60))
        self.assertIsNone(self._state()["seven_day"]["projection"])

    def test_idle_sessions_replaying_old_values_neither_skew_trend_nor_freshness(self):
        reset = int(NOW + 3 * H)
        # One burst (as when the status-line command changes): many sessions
        # report at once, most with stale lower values for the same window.
        usage_quota.ingest_snapshot(self.conn, _snap("live", _five(40, reset), NOW - H))
        for i, old in enumerate((4, 17, 25)):
            usage_quota.ingest_snapshot(self.conn, _snap(f"idle{i}", _five(old, reset), NOW - H))
        usage_quota.ingest_snapshot(self.conn, _snap("live", _five(46, reset), NOW - 20 * 60))
        # Much later an idle session re-sends its stale value.
        usage_quota.ingest_snapshot(self.conn, _snap("idle0", _five(4, reset), NOW - H, NOW - 10))
        w = self._state()["five_hour"]
        self.assertEqual(w["used_pct"], 46.0)
        self.assertEqual(w["age_seconds"], 20 * 60)   # only the 46% report counts
        self.assertEqual(w["status"], "stale")
        p = w["projection"]
        # Trend from the envelope: 40 -> 46 over the hour since NOW-H, not 4 -> 46.
        self.assertEqual(p["projected_pct_at_reset"], round(46 + 6 / H * 3 * H, 1))

    def test_same_session_step_down_starts_new_generation(self):
        # Subscription upgrade: weekly % drops inside the same reset window.
        reset = int(NOW + 3 * 86400)
        week = lambda used: {"seven_day": {"used_percentage": used, "resets_at": reset}}
        usage_quota.ingest_snapshot(self.conn, _snap("old", week(88), NOW - 2 * H))
        usage_quota.ingest_snapshot(self.conn, _snap("live", week(90), NOW - H))
        usage_quota.ingest_snapshot(self.conn, _snap("live", week(4), NOW - 10 * 60))
        usage_quota.ingest_snapshot(self.conn, _snap("other", week(5), NOW - 5 * 60))
        # Closed "old" session still at 88% must not pin the dial; live peak is 5%.
        w = self._state()["seven_day"]
        self.assertEqual(w["used_pct"], 5.0)
        self.assertEqual(w["age_seconds"], 5 * 60)
        self.assertEqual(w["status"], "fresh")

    def test_step_down_cancelled_when_session_recovers(self):
        reset = int(NOW + 3 * H)
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(90, reset), NOW - H))
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(4, reset), NOW - 30 * 60))
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(88, reset), NOW - 60))
        w = self._state()["five_hour"]
        self.assertEqual(w["used_pct"], 90.0)

    def test_other_session_lower_reading_still_cannot_lower(self):
        reset = int(NOW + 2 * H)
        usage_quota.ingest_snapshot(self.conn, _snap("busy", _five(60, reset), NOW - 300))
        usage_quota.ingest_snapshot(self.conn, _snap("other", _five(5, reset), NOW - 60))
        self.assertEqual(self._state()["five_hour"]["used_pct"], 60.0)

    def test_no_projection_from_a_single_snapshot(self):
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(20, int(NOW + H)), NOW - 60))
        self.assertIsNone(self._state()["five_hour"]["projection"])

    def test_spool_ingest_reads_files_and_drops_old_ones(self):
        with tempfile.TemporaryDirectory() as folder:
            fresh = os.path.join(folder, "claude_statusline-a.json")
            old = os.path.join(folder, "claude_statusline-b.json")
            with open(fresh, "w") as fh:
                json.dump(_snap("a", _five(5, int(NOW + H)), NOW - 60), fh)
            with open(old, "w") as fh:
                json.dump(_snap("b", _five(5, int(NOW - 9 * 86400)), NOW - 9 * 86400), fh)
            self.assertEqual(usage_quota.ingest_spool(self.conn, folder, now=NOW), 1)
            self.assertFalse(os.path.exists(old))
            self.assertEqual(usage_quota.ingest_spool(self.conn, folder, now=NOW), 0)


class RetiredProviderTests(unittest.TestCase):
    """Cursor dials were removed; leftover Cursor data must stay invisible."""

    def setUp(self):
        self.conn = _conn()

    def test_only_claude_is_reported(self):
        usage_quota.ingest_snapshot(self.conn, _snap("s", _five(10, int(NOW + H)), NOW - 60))
        self.assertEqual(list(usage_quota.state(self.conn, now=NOW)["providers"]), ["claude"])

    def test_cursor_snapshots_and_spool_files_are_ignored(self):
        snap = {"provider": "cursor", "source": "cursor_dashboard", "changed_at": NOW, "seen_at": NOW,
                "windows": {"cursor_models": {"used_percentage": 3, "resets_at": int(NOW + 86400)}}}
        self.assertEqual(usage_quota.ingest_snapshot(self.conn, snap), 0)
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "cursor_dashboard.json"), "w") as fh:
                json.dump(snap, fh)
            self.assertEqual(usage_quota.ingest_spool(self.conn, folder, now=NOW), 0)

    def test_schema_migration_adds_starts_at(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.executescript(usage_quota.SCHEMA_SQL.replace(
            "  starts_at    INTEGER,            -- epoch seconds; NULL = resets_at - nominal length\n", ""))
        usage_quota.ensure_schema(conn)
        self.assertIn("starts_at", {r[1] for r in conn.execute("PRAGMA table_info(quota_observations)")})


if __name__ == "__main__":
    unittest.main()
