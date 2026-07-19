import importlib.machinery
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
usage = importlib.machinery.SourceFileLoader(
    "fleet_usage_test", str(ROOT / "fleet-usage")).load_module()


class UsageTests(unittest.TestCase):
    def test_fields_are_aligned_and_weekly_has_a_day(self):
        five = usage.field("5h", 0)
        week = usage.field("7d", 4, 1784493419)
        line = f"{five}  {week}"
        self.assertEqual(line.index("7d"), 30)
        self.assertRegex(week, r"@[A-Z][a-z]{2} \d\d \d\d:\d\d$")

    def test_codex_falls_back_to_cached_proxy_quota(self):
        body = {"accounts": [{"status": "active", "quota": {
            "rate_limit": {"used_percent": 4, "reset_at": 1784493419,
                           "limit_window_seconds": 604800},
            "secondary_rate_limit": None,
        }}]}
        with patch.object(usage, "codex_local", return_value=None), \
             patch.object(usage.urllib.request, "urlopen",
                          return_value=io.StringIO(json.dumps(body))):
            text = usage.codex()
        self.assertIn("5h [--------]   0%/0h", text)
        self.assertIn("7d [--------]   4%", text)

    def test_codex_prefers_latest_local_rate_limit(self):
        event = {"payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 14, "resets_at": 1784958277,
                        "window_minutes": 10080},
            "secondary": None,
        }}}
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / ".codex/sessions/2026/07/19/session.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(event) + "\n")
            with patch.object(usage.Path, "home", return_value=Path(home)), \
                 patch.object(usage.urllib.request, "urlopen") as urlopen:
                text = usage.codex()
        self.assertIn("7d [#-------]  14%", text)
        urlopen.assert_not_called()

    def test_claude_keeps_usage_when_reset_is_unavailable(self):
        body = {"limits": [
            {"kind": "session", "percent": 99, "resets_at": None},
            {"kind": "weekly_all", "percent": 95,
             "resets_at": "2026-07-17T01:00:00Z"},
        ]}
        credentials = {"claudeAiOauth": {"accessToken": "test"}}
        with patch.object(usage.Path, "read_text", return_value=json.dumps(credentials)), \
             patch.object(usage.urllib.request, "urlopen",
                          return_value=io.StringIO(json.dumps(body))):
            text = usage.claude()
        self.assertIn("5h [########]  99%/0h", text)
        self.assertIn("7d [########]  95%", text)

    def test_unknown_codex_window_is_drift(self):
        body = {"accounts": [{"status": "active", "quota": {
            "rate_limit": {"used_percent": 1, "reset_at": 1,
                           "limit_window_seconds": 42},
            "secondary_rate_limit": None,
        }}]}
        with patch.object(usage.urllib.request, "urlopen",
                          return_value=io.StringIO(json.dumps(body))):
            with self.assertRaises(ValueError):
                usage.codex_proxy()


if __name__ == "__main__":
    unittest.main()
