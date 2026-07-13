import importlib.machinery
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
fleet = importlib.machinery.SourceFileLoader("fleet_test", str(ROOT / "fleet")).load_module()


class FleetTests(unittest.TestCase):
    def test_merge_orders_urgency_then_recency(self):
        panes = {
            "ship": [
                dict(session="a", win="@1", window=0, pane_id="%1", agent="codex",
                     state="working", title="old", cwd="/a", ts=10),
                dict(session="b", win="@2", window=0, pane_id="%2", agent="claude",
                     state="waiting", title="new", cwd="/b", ts=20),
                dict(session="c", win="@3", window=0, pane_id="%3", agent="claude",
                     state="waiting", title="newest", cwd="/c", ts=30),
            ]
        }
        with patch.object(fleet, "hosts", return_value=["ship"]), \
             patch.object(fleet, "poll", side_effect=lambda host: panes[host]):
            rows, unreachable = fleet.merge()
        self.assertEqual([r["session"] for r in rows], ["c", "b", "a"])
        self.assertEqual(unreachable, [])

    def test_merge_uses_most_urgent_pane(self):
        panes = [
            dict(session="a", win="@1", window=0, pane_id="%1", agent="codex",
                 state="working", title="working", cwd="/a", ts=10),
            dict(session="a", win="@1", window=0, pane_id="%2", agent="claude",
                 state="needs-action", title="blocked", cwd="/b", ts=20),
        ]
        with patch.object(fleet, "hosts", return_value=["ship"]), \
             patch.object(fleet, "poll", return_value=panes):
            rows, _ = fleet.merge()
        self.assertEqual(rows[0]["state"], "needs-action")
        self.assertEqual(rows[0]["count"], 2)
        self.assertIsNone(rows[0]["pane_id"])

    def test_absent_does_not_turn_ssh_failure_into_absence(self):
        failed = subprocess.CompletedProcess([], 255, "", "permission denied")
        with patch.object(fleet, "flagship", return_value="flag"), \
             patch.object(fleet, "run", return_value=failed):
            with self.assertRaises(SystemExit):
                fleet.absent("ship", "work")

    def test_ordering_freeze_is_limited_to_fleet_clients(self):
        with patch.object(fleet, "flagship", return_value="flag"), \
             patch.object(fleet, "tmux", return_value="ordinary fleet\nfleet@main root\n"):
            self.assertFalse(fleet.ordering_frozen())
        with patch.object(fleet, "flagship", return_value="flag"), \
             patch.object(fleet, "tmux", return_value="ordinary root\nfleet@one fleet\n"):
            self.assertTrue(fleet.ordering_frozen())

    def test_state_change_time_is_preserved_until_state_changes(self):
        row = dict(host="flag", win="@1", session="work", window=0, pane_id="%1",
                   agent="codex", state="waiting", count=1, ts=10, num=1,
                   cwd="/tmp", title="done")
        old = {("flag", "@1"): {"state": "waiting", "changed": "40"}}
        with patch.object(fleet, "tmux_batched"):
            fleet.stamp_rows([row], old, {}, 50)
        self.assertEqual(row["state_changed"], 40)
        row["state"] = "working"
        with patch.object(fleet, "tmux_batched"):
            fleet.stamp_rows([row], old, {}, 50)
        self.assertEqual(row["state_changed"], 50)

    def test_spoken_number_requires_every_word_to_be_numeric(self):
        self.assertEqual(fleet.spoken_number(["twenty", "one"]), 21)
        self.assertIsNone(fleet.spoken_number(["the", "one"] ))

    def test_type_is_literal_and_defaults_to_focused_station(self):
        args = type("Args", (), {"screen": None, "text": ["hello", "world"]})()
        with patch.object(fleet, "selected_screen", return_value="noether-1a"), \
             patch.object(fleet, "station_session", return_value="fleet@noether-1a"), \
             patch.object(fleet, "flagship", return_value="lovelace"), \
             patch.object(fleet, "tmux") as tmux:
            fleet.cmd_type(args)
        tmux.assert_called_once_with("lovelace", "send-keys", "-l", "-t",
                                     "=fleet@noether-1a:", "hello world")


class TmuxIntegrationTests(unittest.TestCase):
    def test_grouped_stations_select_windows_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["tmux", "-L", "fleet-test", "-f", "/dev/null"]
            env = {"TMUX_TMPDIR": tmp}
            def run(*args):
                return subprocess.run([*base, *args], env=env, check=True,
                                      text=True, capture_output=True)
            run("new-session", "-d", "-s", "fleet@main", "-n", "zero")
            run("new-window", "-d", "-t", "fleet@main:1", "-n", "one")
            run("new-session", "-d", "-t", "fleet@main", "-s", "fleet@left")
            run("new-session", "-d", "-t", "fleet@main", "-s", "fleet@right")
            run("select-window", "-t", "fleet@left:0")
            run("select-window", "-t", "fleet@right:1")
            left = run("display", "-p", "-t", "fleet@left:", "#{window_index}").stdout.strip()
            right = run("display", "-p", "-t", "fleet@right:", "#{window_index}").stdout.strip()
            self.assertEqual((left, right), ("0", "1"))
            origin = run("display", "-p", "-t", "fleet@left:0", "#{window_id}").stdout.strip()
            run("set-option", "-t", "fleet@left", "@fleet_origin", origin)
            run("source-file", str(ROOT / "tmux.conf"))
            client = subprocess.Popen([*base, "-C", "attach", "-t", "fleet@left"],
                                      env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(20):
                    names = run("list-clients", "-F", "#{client_name}").stdout.splitlines()
                    if names:
                        break
                    time.sleep(0.01)
                run("select-window", "-t", "fleet@left:1")
                run("switch-client", "-c", names[0], "-T", "fleet")
                run("send-keys", "-K", "-c", names[0], "Escape")
                selected = run("display", "-p", "-t", "fleet@left:",
                               "#{window_index}").stdout.strip()
                self.assertEqual(selected, "0")
            finally:
                client.terminate()
                client.wait()
                client.stdin.close()
            run("kill-server")


if __name__ == "__main__":
    unittest.main()
