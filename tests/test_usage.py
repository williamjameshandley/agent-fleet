import importlib.machinery
import io
import json
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

    def test_codex_uses_cached_proxy_quota(self):
        body = {"accounts": [{"status": "active", "quota": {
            "rate_limit": {"used_percent": 4, "reset_at": 1784493419,
                           "limit_window_seconds": 604800},
            "secondary_rate_limit": None,
        }}]}
        with patch.object(usage.urllib.request, "urlopen",
                          return_value=io.StringIO(json.dumps(body))):
            text = usage.codex()
        self.assertIn("5h [--------]   0%/0h", text)
        self.assertIn("7d [--------]   4%", text)

    def test_unknown_codex_window_is_drift(self):
        body = {"accounts": [{"status": "active", "quota": {
            "rate_limit": {"used_percent": 1, "reset_at": 1,
                           "limit_window_seconds": 42},
            "secondary_rate_limit": None,
        }}]}
        with patch.object(usage.urllib.request, "urlopen",
                          return_value=io.StringIO(json.dumps(body))):
            with self.assertRaises(ValueError):
                usage.codex()


if __name__ == "__main__":
    unittest.main()
