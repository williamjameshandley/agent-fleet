import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent_fleet import usage


class UsageTests(unittest.TestCase):
    def test_fields_are_aligned_and_weekly_has_a_day(self):
        five = usage.field("5h", 0)
        week = usage.field("7d", 4, 1784493419)
        line = f"{five}  {week}"
        self.assertEqual(line.index("7d"), 38)
        self.assertRegex(week, r"@[A-Z][a-z]{2} \d\d \d\d:\d\d$")

    def test_codex_reads_native_app_server_limits(self):
        server = r'''
import json, sys
initialize = json.loads(sys.stdin.readline())
assert initialize["method"] == "initialize"
print(json.dumps({"id": initialize["id"], "result": {}}), flush=True)
assert json.loads(sys.stdin.readline()) == {"method": "initialized"}
request = json.loads(sys.stdin.readline())
assert request["method"] == "account/rateLimits/read"
print(json.dumps({"id": request["id"], "result": {"rateLimits": {
    "primary": {"usedPercent": 14, "windowDurationMins": 10080,
                "resetsAt": 1784958277},
    "secondary": {"usedPercent": 5, "windowDurationMins": 300,
                  "resetsAt": 1784958277}}}}), flush=True)
'''
        text = usage.codex((sys.executable, "-c", server))
        self.assertIn("5h [#---------------]   5%", text)
        self.assertIn("7d [##--------------]  14%", text)

    def test_codex_rejects_malformed_protocol_response(self):
        server = "import sys; sys.stdin.readline(); print('not-json', flush=True)"
        with self.assertRaises(json.JSONDecodeError):
            usage.codex((sys.executable, "-c", server))

    def test_codex_timeout_reaps_app_server(self):
        process = None
        real_popen = subprocess.Popen

        def capture(*args, **kwargs):
            nonlocal process
            process = real_popen(*args, **kwargs)
            return process

        with patch.object(usage.subprocess, "Popen", side_effect=capture):
            with self.assertRaises(TimeoutError):
                server = ("import os, signal, time; "
                          "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                          "os.write(1, b'{'); time.sleep(60)")
                usage.codex((sys.executable, "-c", server),
                            timeout=0.05)
        self.assertIsNotNone(process.poll())

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
        self.assertIn("5h [################]  99%/0h", text)
        self.assertIn("7d [###############-]  95%", text)

if __name__ == "__main__":
    unittest.main()
