import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import insights
import main


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class InsightContractTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE projects (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              category TEXT NOT NULL,
              archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE todos (
              id INTEGER PRIMARY KEY,
              project_id INTEGER NOT NULL,
              text TEXT NOT NULL,
              is_blocker INTEGER NOT NULL DEFAULT 0,
              for_later INTEGER NOT NULL DEFAULT 0,
              in_progress INTEGER NOT NULL DEFAULT 0,
              running INTEGER NOT NULL DEFAULT 0,
              awaiting_review INTEGER NOT NULL DEFAULT 0,
              flagged INTEGER NOT NULL DEFAULT 0,
              completed INTEGER NOT NULL DEFAULT 0,
              completed_at TEXT,
              deadline TEXT,
              effort INTEGER,
              updated_at TEXT NOT NULL
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO projects (id, name, category) VALUES (?, ?, ?)",
            [
                (1, "Agenda", "top_of_mind"),
                (2, "Paper", "near_publication"),
            ],
        )
        old = (NOW - timedelta(days=16)).isoformat()
        recent = (NOW - timedelta(days=2)).isoformat()
        self.conn.executemany(
            """INSERT INTO todos
               (id, project_id, text, is_blocker, for_later, in_progress,
                running, awaiting_review, flagged, completed, completed_at,
                deadline, effort, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (10, 1, "Publish Agenda", 0, 0, 0, 0, 0, 0, 1, recent, None, 3, recent),
                (11, 1, "Review native insights", 0, 0, 1, 0, 0, 0, 0, None, None, 2, old),
                (12, 1, "Fix release blocker", 1, 0, 0, 0, 0, 0, 0, None, None, 1, recent),
                (20, 2, "Submit paper", 0, 0, 0, 0, 1, 0, 0, None, "2026-08-09", 5, old),
            ],
        )

    def tearDown(self):
        self.conn.close()

    def test_weekly_review_is_no_longer_an_insight_mode(self):
        with self.assertRaisesRegex(ValueError, "unsupported insight mode"):
            insights.build_context("weekly_review", self.conn, NOW)

    def test_daily_plan_prioritizes_deadline_blocker_and_review(self):
        context = insights.build_context("plan_today", self.conn, NOW)
        top = context["candidates"][:3]
        self.assertEqual({item["target_id"] for item in top}, {11, 12, 20})
        self.assertEqual(top[0]["kind"], "review")
        self.assertTrue(all(item["evidence_ids"] for item in top))

    def test_stale_work_calculates_age_without_model_help(self):
        context = insights.build_context("stale_work", self.conn, NOW)
        todo = next(item for item in context["candidates"] if item["target_id"] == 11)
        self.assertIn("16 days", todo["headline"])
        self.assertEqual(todo["kind"], "stale")

    def test_materialization_derives_targets_and_evidence_from_candidate(self):
        context = insights.build_context("plan_today", self.conn, NOW)
        candidate = context["candidates"][0]
        result = insights.materialize_insights(
            context,
            [
                {
                    "candidate_id": candidate["candidate_id"],
                    "headline": "Finish the waiting review",
                    "message": "This review is ready and blocks forward progress.",
                }
            ],
            provider="apple-foundation-models",
            now=NOW,
        )
        self.assertEqual(result[0]["target_id"], candidate["target_id"])
        self.assertEqual(result[0]["evidence_ids"], candidate["evidence_ids"])
        self.assertEqual(result[0]["suggested_action"], candidate["suggested_action"])

    def test_materialization_rejects_unknown_or_overlong_drafts(self):
        context = insights.build_context("plan_today", self.conn, NOW)
        with self.assertRaises(ValueError):
            insights.materialize_insights(
                context,
                [{"candidate_id": "invented", "headline": "No", "message": "No"}],
                provider="apple-foundation-models",
                now=NOW,
            )
        candidate = context["candidates"][0]
        with self.assertRaises(ValueError):
            insights.materialize_insights(
                context,
                [{
                    "candidate_id": candidate["candidate_id"],
                    "headline": "x" * 81,
                    "message": "Too long.",
                }],
                provider="apple-foundation-models",
                now=NOW,
            )


class InsightAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE insight_cache (
              mode TEXT PRIMARY KEY,
              source_hash TEXT NOT NULL,
              provider TEXT NOT NULL,
              insights_json TEXT NOT NULL,
              generated_at TEXT NOT NULL
            );
            CREATE TABLE insight_states (
              insight_id TEXT PRIMARY KEY,
              dismissed_at TEXT,
              snoozed_until TEXT,
              updated_at TEXT NOT NULL
            );
            """
        )
        self.context = {
            "mode": "plan_today",
            "generated_for": "2026-08-08",
            "source_hash": "source-1",
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "target_type": "todo",
                    "target_id": 7,
                    "kind": "review",
                    "urgency": "high",
                    "headline": "Review finished work",
                    "message": "This task is waiting for review.",
                    "evidence_ids": ["todo:7", "project:2"],
                    "suggested_action": "review",
                    "score": 100,
                }
            ],
        }

    async def asyncTearDown(self):
        self.conn.close()

    async def test_refresh_caches_valid_local_drafts(self):
        helper = AsyncMock(
            return_value={
                "status": "ok",
                "insights": [
                    {
                        "candidate_id": "candidate-1",
                        "headline": "Review the finished work",
                        "message": "It is ready for your attention.",
                    }
                ],
            }
        )
        with (
            patch.object(main.db, "get_conn", return_value=self.conn),
            patch.object(
                main.native_insights,
                "build_context",
                return_value=self.context,
            ),
            patch.object(
                main,
                "_local_ai_availability",
                {"status": "available"},
            ),
            patch.object(main, "_invoke_local_ai", helper),
        ):
            response = await main.refresh_native_insights(
                main.InsightRefreshRequest(mode="plan_today")
            )
        self.assertEqual(response["provider"], "apple-foundation-models")
        self.assertEqual(response["insights"][0]["target_id"], 7)
        cached = self.conn.execute(
            "SELECT provider FROM insight_cache WHERE mode='plan_today'"
        ).fetchone()
        self.assertEqual(cached["provider"], "apple-foundation-models")

    async def test_malformed_local_draft_falls_back_deterministically(self):
        with (
            patch.object(main.db, "get_conn", return_value=self.conn),
            patch.object(
                main.native_insights,
                "build_context",
                return_value=self.context,
            ),
            patch.object(
                main,
                "_local_ai_availability",
                {"status": "available"},
            ),
            patch.object(
                main,
                "_invoke_local_ai",
                AsyncMock(
                    return_value={
                        "status": "ok",
                        "insights": [
                            {
                                "candidate_id": "invented",
                                "headline": "Invented",
                                "message": "Not grounded.",
                            }
                        ],
                    }
                ),
            ),
        ):
            response = await main.refresh_native_insights(
                main.InsightRefreshRequest(mode="plan_today")
            )
        self.assertEqual(response["provider"], "deterministic")
        self.assertEqual(
            response["insights"][0]["headline"],
            "Review finished work",
        )


if __name__ == "__main__":
    unittest.main()
