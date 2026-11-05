import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main


class TitleContractTests(unittest.TestCase):
    def test_normalizes_valid_provider_title(self):
        self.assertEqual(
            main._normalize_title_candidate('"Fix login item registration"', "BUG: "),
            "BUG: Fix login item registration",
        )

    def test_rejects_multiline_overlong_and_too_many_words(self):
        self.assertIsNone(main._normalize_title_candidate("Title\nExplanation"))
        self.assertIsNone(main._normalize_title_candidate("x" * 61))
        self.assertIsNone(
            main._normalize_title_candidate(
                "This title has far too many words for a compact card"
            )
        )

    def test_fallback_is_always_bounded(self):
        result = main._fallback_title(
            "This is an exceptionally long task description that needs shortening",
            prefix="FR: ",
        )
        self.assertTrue(result.startswith("FR: "))
        self.assertLessEqual(len(result), main.TITLE_MAX_CHARACTERS)
        self.assertLessEqual(
            len(result.removeprefix("FR: ").split()),
            main.TITLE_MAX_WORDS,
        )


class LocalHelperTests(unittest.IsolatedAsyncioTestCase):
    def make_helper(self, body: str) -> str:
        directory = Path(self.temp.name)
        helper = directory / "helper.py"
        helper.write_text(
            "#!/usr/bin/env python3\n"
            + textwrap.dedent(body).lstrip()
        )
        helper.chmod(0o755)
        return str(helper)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_reads_valid_helper_json(self):
        helper = self.make_helper(
            """
            import json, sys
            json.load(sys.stdin)
            print(json.dumps({
                "status": "ok",
                "title": "Concise local title",
                "provider": "apple-foundation-models"
            }))
            """
        )
        with patch.object(main, "LOCAL_AI_HELPER", helper):
            response = await main._invoke_local_ai(
                {"operation": "title", "text": "Long input"}
            )
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["title"], "Concise local title")

    async def test_reports_helper_timeout(self):
        helper = self.make_helper(
            """
            import time
            time.sleep(2)
            """
        )
        with (
            patch.object(main, "LOCAL_AI_HELPER", helper),
            patch.object(main, "LOCAL_AI_TIMEOUT_SECONDS", 0.05),
        ):
            response = await main._invoke_local_ai(
                {"operation": "title", "text": "Long input"}
            )
        self.assertEqual(response, {"status": "error", "reason": "timeout"})

    async def test_reports_invalid_helper_output(self):
        helper = self.make_helper("print('not json')\n")
        with patch.object(main, "LOCAL_AI_HELPER", helper):
            response = await main._invoke_local_ai({"operation": "availability"})
        self.assertEqual(response, {"status": "error", "reason": "invalid-json"})


class ProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_local_provider(self):
        local = AsyncMock(return_value="Local concise title")
        with (
            patch.object(main, "_local_ai_availability", {"status": "available"}),
            patch.object(main, "_local_title", local),
        ):
            title, provider = await main._select_generated_title(
                "A long todo description that requires a concise title"
            )
        self.assertEqual((title, provider), ("Local concise title", "apple-foundation-models"))

    async def test_no_provider_keeps_deterministic_fallback(self):
        with patch.object(
            main, "_local_ai_availability", {"status": "unavailable"}
        ):
            title, provider = await main._select_generated_title("Long raw input")
        self.assertIsNone(title)
        self.assertIsNone(provider)


if __name__ == "__main__":
    unittest.main()
