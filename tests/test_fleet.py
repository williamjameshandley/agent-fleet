import importlib.machinery
import io
import json
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
fleet = importlib.machinery.SourceFileLoader("fleet_test", str(ROOT / "fleet")).load_module()


class FleetTests(unittest.TestCase):
    def test_run_allows_interactive_subprocesses(self):
        with patch.object(fleet.subprocess, "run") as subprocess_run:
            fleet.run(["fzf"], capture_output=False, stdout=subprocess.PIPE)
        subprocess_run.assert_called_once_with(
            ["fzf"], text=True, capture_output=False, stdout=subprocess.PIPE)

    def test_merge_orders_working_first_then_recency(self):
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
        self.assertEqual([r["session"] for r in rows], ["a", "c", "b"])
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

    def test_poll_timeout_marks_only_that_host_unreachable(self):
        with patch.object(fleet, "flagship", return_value="flag"), \
             patch.object(fleet, "run", side_effect=subprocess.TimeoutExpired([], 10)):
            self.assertIsNone(fleet.poll("ship"))

    def test_host_session_target_survives_reordering(self):
        rows = [dict(num=1, host="newton", session="work"),
                dict(num=2, host="lovelace", session="work")]
        self.assertEqual(fleet.resolve({"rows": rows}, "newton:work"), rows[0])

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

    def test_remote_rows_attach_directly_without_shadow_sessions(self):
        row = dict(host="newton", win="@7", session="email-3", num=1)
        with patch.object(fleet, "flagship", return_value="lovelace"), \
             patch.object(fleet, "tmux") as tmux:
            fleet.create_rows([row], {})
        command = " ".join(str(x) for x in tmux.call_args.args)
        self.assertIn("set-option -t =email-3 status off", command)
        self.assertIn("attach-session -t =email-3", command)
        self.assertNotIn("ControlMaster=no", command)
        self.assertNotIn("ControlPath=none", command)
        self.assertIn("IdentityAgent=", command)
        self.assertNotIn("fleet@w", command)
        self.assertIn("newton exec sh -c", command)
        self.assertNotIn("newton 'exec sh -c", command)

    def test_reconcile_restores_each_grouped_sessions_window_identity(self):
        selected = {"fleet@left": "@1", "fleet@right": "@2"}
        with patch.object(fleet, "group_selections", return_value={
                "fleet@left": "@0", "fleet@right": "@2"}), \
             patch.object(fleet, "flagship", return_value="flag"), \
             patch.object(fleet, "tmux", return_value="@0\n@1\n@2\n"), \
             patch.object(fleet, "tmux_batched") as batched:
            fleet.restore_group_selections(selected)
        batched.assert_called_once_with(
            "flag", [["select-window", "-t", "=fleet@left:@1"]])

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

    def test_delete_kills_tmux_session_but_not_transcript(self):
        args = type("Args", (), {"target": "7"})()
        row = dict(num=7, host="newton", session="email-3")
        with patch.object(fleet, "manifest", return_value={"rows": [row]}), \
             patch.object(fleet, "tmux") as tmux, redirect_stdout(io.StringIO()) as out:
            fleet.cmd_delete(args)
        tmux.assert_called_once_with("newton", "kill-session", "-t", "=email-3")
        self.assertIn("transcript retained", out.getvalue())

    def test_resurrect_uses_native_agent_resume(self):
        args = type("Args", (), {"name": "restored", "session_id": "abc",
                                  "agent": "claude"})()
        session = type("Session", (), {
            "agent": "claude", "session_id": "abc-def",
            "cwd": lambda self: "/work"})()
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(fleet, "resolve_log", return_value=session), \
             patch.object(fleet, "run", return_value=ok) as run:
            fleet.cmd_resurrect_local(args)
        self.assertEqual(run.call_args_list[0].args[0], [
            "tmux", "new-session", "-d", "-s", "restored", "-c", "/work",
            "claude", "--resume", "abc-def"])

    def test_history_tab_reloads_with_the_history_cli(self):
        with patch.object(fleet.os, "execvp") as execvp:
            fleet.cmd_history_ui(None)
        argv = execvp.call_args.args[1]
        self.assertIn(f"start:reload({Path(fleet.__file__).resolve()} history -n 100)",
                      argv)

    def test_live_tab_contains_session_management(self):
        with patch.object(fleet, "ensure_muster_tabs"), \
             patch.object(fleet.os, "execvp") as execvp:
            fleet.cmd_muster_ui(None)
        argv = execvp.call_args.args[1]
        self.assertIn("Tab history · c create · r rename · d delete · Enter show", argv)
        self.assertTrue(any(arg.startswith("d:execute(") for arg in argv))
        self.assertTrue(any("wait-for -S agent-fleet-focus-main" in arg for arg in argv))
        self.assertIn("tmux capture-pane -ep -t '=fleet@main:{3}' "
                      "| tail -n $FZF_PREVIEW_LINES", argv)
        self.assertTrue(any("muster --cursor" in arg for arg in argv))

    def test_history_sort_handles_equal_timestamps(self):
        args = type("Args", (), {"n": 2})()
        items = [{"agent": "claude", "session_id": str(i), "mtime": 1,
                  "name": f"s{i}", "cwd": "/work"} for i in range(2)]
        result = subprocess.CompletedProcess([], 0, json.dumps(items), "")
        with patch.object(fleet, "manifest", return_value={"rows": []}), \
             patch.object(fleet, "hosts", return_value=["lovelace"]), \
             patch.object(fleet, "sh", return_value=result), \
             redirect_stdout(io.StringIO()) as out:
            fleet.cmd_history(args)
        self.assertEqual(len(out.getvalue().splitlines()), 2)


class TmuxIntegrationTests(unittest.TestCase):
    def test_reorder_restores_grouped_session_window_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["tmux", "-L", "fleet-reorder-test", "-f", "/dev/null"]
            env = {"TMUX_TMPDIR": tmp}
            def run(*args):
                return subprocess.run([*base, *args], env=env, check=True,
                                      text=True, capture_output=True)
            run("new-session", "-d", "-s", "fleet@main", "-n", "zero")
            run("new-window", "-d", "-t", "fleet@main:1", "-n", "one")
            run("new-window", "-d", "-t", "fleet@main:2", "-n", "two")
            run("new-session", "-d", "-t", "fleet@main", "-s", "fleet@left")
            run("select-window", "-t", "fleet@left:1")
            selected = run("display", "-p", "-t", "fleet@left:",
                           "#{window_id}").stdout.strip()
            with patch.object(fleet, "flagship", return_value="flag"), \
                 patch.object(fleet, "tmux", side_effect=lambda host, *args:
                              run(*args).stdout):
                before = fleet.group_selections()
                run("move-window", "-d", "-s", "fleet@main:1",
                    "-t", "fleet@main:1001")
                run("move-window", "-d", "-s", "fleet@main:1001",
                    "-t", "fleet@main:3")
                fleet.restore_group_selections(before)
            restored = run("display", "-p", "-t", "fleet@left:",
                           "#{window_id}").stdout.strip()
            self.assertEqual(restored, selected)
            run("kill-server")

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
