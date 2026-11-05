import base64
import json
import os
import tempfile
import unittest

import usage_cursor

# Shape of GET cursor.com/api/usage-summary as observed 2026-10-15 (Pro+).
SUMMARY = {
    "billingCycleStart": "2026-10-14T16:06:00.000Z",
    "billingCycleEnd": "2026-11-14T16:06:00.000Z",
    "membershipType": "pro_plus",
    "individualUsage": {"plan": {"enabled": True, "used": 3700, "limit": 7000,
                                 "autoPercentUsed": 3.0833333333333335, "apiPercentUsed": 0,
                                 "totalPercentUsed": 2.948207171314741},
                        "onDemand": {"enabled": False}},
}


class CursorSummaryTests(unittest.TestCase):
    def test_maps_auto_and_api_percent_to_billing_cycle_windows(self):
        w = usage_cursor.windows_from_summary(SUMMARY)
        start, end = 1791993960, 1794672360  # the same cycle in epoch seconds
        self.assertEqual(w["cursor_models"], {"used_percentage": 3.0833333333333335,
                                              "starts_at": start, "resets_at": end})
        self.assertEqual(w["other_models"]["used_percentage"], 0.0)

    def test_missing_fields_stay_missing(self):
        s = json.loads(json.dumps(SUMMARY))
        del s["individualUsage"]["plan"]["apiPercentUsed"]
        self.assertEqual(list(usage_cursor.windows_from_summary(s)), ["cursor_models"])
        self.assertEqual(usage_cursor.windows_from_summary({"individualUsage": SUMMARY["individualUsage"]}), {})

    def test_session_cookie_uses_user_id_from_token(self):
        claims = base64.urlsafe_b64encode(json.dumps({"sub": "auth0|user_abc"}).encode()).decode().rstrip("=")
        token = f"h.{claims}.s"
        self.assertEqual(usage_cursor.session_cookie(token), f"WorkosCursorSessionToken=user_abc%3A%3A{token}")

    def test_snapshot_keeps_changed_at_while_values_are_unchanged(self):
        w = usage_cursor.windows_from_summary(SUMMARY)
        with tempfile.TemporaryDirectory() as folder:
            path = usage_cursor.write_snapshot(w, 100.0, folder)
            usage_cursor.write_snapshot(w, 700.0, folder)
            with open(path) as fh:
                snap = json.load(fh)
            self.assertEqual((snap["changed_at"], snap["seen_at"]), (100.0, 700.0))
            self.assertEqual(os.path.basename(path), "cursor_dashboard.json")
            w["cursor_models"]["used_percentage"] = 4.0
            usage_cursor.write_snapshot(w, 1300.0, folder)
            with open(path) as fh:
                self.assertEqual(json.load(fh)["changed_at"], 1300.0)


if __name__ == "__main__":
    unittest.main()
