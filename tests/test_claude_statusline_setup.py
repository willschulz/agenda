import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_statusline_setup as setup
import usage_quota
import usage_statusline


class ClaudeStatusLineSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.claude = root / "claude"
        self.data = root / "data"
        self.data.mkdir()
        self.settings = self.claude / "settings.json"
        patches = [
            patch.dict(os.environ, {"AGENDA_CLAUDE_DIR": str(self.claude)}),
            patch.object(setup.db, "DB_PATH", self.data / "agenda.db"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        usage_quota.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _write(self, data):
        self.claude.mkdir(exist_ok=True)
        self.settings.write_text(json.dumps(data))

    def _read(self):
        return json.loads(self.settings.read_text())

    def test_without_claude_code_nothing_is_written(self):
        self.assertEqual(setup.status(self.conn)["state"], "not_installed")
        with self.assertRaises(setup.SetupError):
            setup.connect(self.conn)
        self.assertFalse(self.claude.exists())

    def test_connect_and_disconnect_with_no_previous_status_line(self):
        self._write({"model": "opus", "permissions": {"allow": ["Read"]}})
        self.assertEqual(setup.status(self.conn)["state"], "not_connected")
        self.assertEqual(setup.connect(self.conn)["state"], "connected")
        data = self._read()
        self.assertEqual(data["statusLine"], {"type": "command", "command": setup.bridge_command()})
        self.assertEqual((data["model"], data["permissions"]), ("opus", {"allow": ["Read"]}))
        self.assertIn(sys.executable, data["statusLine"]["command"])
        self.assertIn(str(setup.BRIDGE), data["statusLine"]["command"])
        self.assertEqual(setup.disconnect(self.conn)["state"], "not_connected")
        self.assertEqual(self._read(), {"model": "opus", "permissions": {"allow": ["Read"]}})

    def test_existing_status_line_is_kept_and_restored(self):
        mine = {"type": "command", "command": "~/bin/my-status", "padding": 1}
        self._write({"statusLine": mine})
        self.assertEqual(setup.status(self.conn)["state"], "other")
        s = setup.connect(self.conn)
        self.assertTrue(s["keeps_previous"])
        self.assertEqual(self._read()["statusLine"]["padding"], 1)
        # Connecting twice must not overwrite the saved original with our own command.
        setup.connect(self.conn)
        setup.disconnect(self.conn)
        self.assertEqual(self._read()["statusLine"], mine)
        self.assertFalse(setup.previous_path().exists())

    def test_invalid_settings_are_left_alone(self):
        self.claude.mkdir()
        self.settings.write_text("{not json")
        self.assertEqual(setup.status(self.conn)["state"], "error")
        with self.assertRaises(setup.SetupError):
            setup.connect(self.conn)
        self.assertEqual(self.settings.read_text(), "{not json")

    def test_moved_app_is_repaired_only_when_the_old_bridge_is_gone(self):
        other = Path(self.tmp.name) / "Other.app" / "usage_statusline.py"
        other.parent.mkdir()
        other.write_text("")
        self._write({"statusLine": {"type": "command", "command": f"/usr/bin/python3 -I -B {other}"}})
        self.assertEqual(setup.status(self.conn)["state"], "moved")
        self.assertFalse(setup.repair_if_broken())
        other.unlink()
        self.assertEqual(setup.status(self.conn)["state"], "broken")
        self.assertTrue(setup.repair_if_broken())
        self.assertEqual(setup.status(self.conn)["state"], "connected")

    def test_hand_written_command_for_this_bridge_counts_as_connected(self):
        self._write({"statusLine": {"type": "command", "command": f"/some/python3 -I -B {setup.BRIDGE}"}})
        self.assertEqual(setup.status(self.conn)["state"], "connected")

    def test_disconnect_leaves_someone_elses_status_line(self):
        self._write({"statusLine": {"type": "command", "command": "other"}})
        setup.disconnect(self.conn)
        self.assertEqual(self._read()["statusLine"]["command"], "other")


class BridgePassthroughTests(unittest.TestCase):
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

    def _save_previous(self, command):
        with open(os.path.join(self.tmp.name, usage_statusline.PREVIOUS_FILE), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": command}}, fh)

    def test_previous_status_line_gets_the_same_input(self):
        self._save_previous("python3 -c 'import json,sys; print(\"mine:\" + json.load(sys.stdin)[\"session_id\"])'")
        self.assertEqual(self._run({"session_id": "s1", "model": {"display_name": "Opus"}}), "mine:s1")

    def test_failing_previous_falls_back_to_agenda_line(self):
        self._save_previous("exit 3")
        self.assertEqual(self._run({"session_id": "s1", "model": {"display_name": "Opus"}}), "Opus")

    def test_never_calls_itself(self):
        self._save_previous("python3 /x/usage_statusline.py")
        self.assertEqual(usage_statusline.previous_command(), "")


if __name__ == "__main__":
    unittest.main()
