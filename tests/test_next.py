import unittest
import os
import subprocess
import sys
import tempfile
import time
import json
import queue
import socket
import threading
from unittest import mock
from pathlib import Path

from fleet_next.model import ServerRef, Session, SessionRef
from fleet_next.protocol import decode, encode
from fleet_next.ui import STATE_ORDER, recency
from fleet_next.tmux import split_key
from fleet_next.actions import agent_command, next_waiting_key, session_name
from fleet_next import actions
from fleet_next.config import ssh_environment
from fleet_next.alan import inventory as alan_inventory
from fleet_next.alan import socket_path as alan_socket_path
from fleet_next.alan import Watcher as AlanWatcher
from fleet_next.alan import set_attention as alan_set_attention
from fleet_next import viewer


class IdentityTests(unittest.TestCase):
    def session(self, host, sid="$1"):
        return Session(SessionRef(ServerRef(host, "/tmp/tmux/default", 12, 10), sid),
                       "work", 1, 2, 0, 1, "codex", "waiting", "/work", "tracked")

    def test_identical_tmux_ids_on_different_hosts_are_distinct(self):
        self.assertNotEqual(self.session("newton").ref, self.session("lovelace").ref)

    def test_protocol_round_trip_preserves_canonical_identity(self):
        sessions = [self.session("newton"), self.session("lovelace")]
        self.assertEqual(decode(encode(sessions)), sessions)

    def test_protocol_round_trip_preserves_tagged_alan_identity(self):
        actor = alan_inventory("newton", [{
            "addr": "python-deadbeef", "type": "python", "state": "live",
            "label": "notebook", "cwd": "/work", "native": {"id": "kernel-1"},
            "attachment": {"kind": "jupyter", "connection_file": "/run/kernel.json"},
        }])[0]

        self.assertEqual(actor.ref.key, "alan:newton:python-deadbeef")
        self.assertEqual(decode(encode([actor])), [actor])
        self.assertEqual(actor.agent, "python")
        self.assertEqual(actor.attachment["connection_file"], "/run/kernel.json")

    def test_alan_inventory_maps_busy_actor_without_creating_tmux_identity(self):
        actor = alan_inventory("lovelace", [{
            "addr": "python-1", "type": "python", "state": "busy",
            "label": "analysis", "cwd": None, "native": None,
            "attachment": {"kind": "jupyter", "connection_file": "/run/kernel.json"},
        }])[0]

        self.assertEqual(actor.ref.server.kind, "alan")
        self.assertEqual(actor.state, "working")
        self.assertEqual(actor.name, "analysis")

    def test_alan_inventory_reconstructs_attention_from_fleet_mailbox(self):
        actor = alan_inventory("lovelace", [{
            "addr": "python-1", "type": "python", "state": "live",
            "attachment": {"kind": "jupyter", "connection_file": "/run/k.json"},
        }], {"python-1": "done"})[0]
        self.assertEqual(actor.attention, "done")

    def test_alan_attention_appends_a_fleet_mailbox_event(self):
        with mock.patch("fleet_next.alan.request") as request:
            alan_set_attention("claude-1", "done")
        request.assert_called_once_with({
            "op": "send", "to": "fleet", "payload": {
                "kind": "fleet_attention", "actor": "claude-1",
                "attention": "done"}})

    def test_attention_replay_paginates_before_publishing(self):
        watcher = object.__new__(AlanWatcher)
        watcher.attention = {}
        watcher._changed = queue.Queue()
        watcher._consumer = threading.Event()
        first = [{"idx": index, "payload": {"kind": "fleet_attention",
                  "actor": f"python-{index}", "attention": "done"}}
                 for index in range(100)]
        second = [{"idx": 100, "payload": {"kind": "fleet_attention",
                   "actor": "python-0", "attention": "tracked"}}]

        def response(_payload):
            if response.calls == 0:
                response.calls += 1
                return {"messages": first}
            watcher._consumer.set()
            return {"messages": second}

        response.calls = 0
        with mock.patch("fleet_next.alan.configured", return_value=True), \
             mock.patch("fleet_next.alan.request", side_effect=response):
            watcher._run_attention()
        self.assertEqual(watcher.attention["python-0"], "tracked")
        self.assertEqual(len(watcher.attention), 100)
        self.assertEqual(watcher._changed.qsize(), 1)

    def test_non_attachable_alan_actors_are_not_fleet_rows(self):
        self.assertEqual(alan_inventory("lovelace", [{
            "addr": "llm-1", "type": "llm", "state": "live", "label": "hidden",
            "attachment": {"kind": "none"},
        }]), [])

    def test_packaged_service_and_noninteractive_cli_share_explicit_alan_socket(self):
        root = Path(__file__).parents[1]
        self.assertEqual((root / "alan-socket").read_text().strip(),
                         "/run/alan-loop/loop.sock")
        self.assertNotIn("LOOP_SOCKET=", (root / "fleet-next.service").read_text())
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("fleet_next.alan.Path.home", return_value=Path("/missing")), \
             mock.patch("fleet_next.alan.Path.exists", return_value=True), \
             mock.patch("fleet_next.alan.Path.read_text",
                        return_value="/run/alan-loop/loop.sock\n"):
            self.assertEqual(alan_socket_path(), Path("/run/alan-loop/loop.sock"))

    def test_watch_protocol_failure_clears_actor_rows_and_retries(self):
        actor = {"addr": "python-1", "type": "python", "state": "live",
                 "attachment": {"kind": "jupyter", "connection_file": "/run/k.json"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loop.sock"
            ready = threading.Event()

            def serve():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(path))
                    server.listen()
                    ready.set()
                    connection, _ = server.accept()
                    with connection:
                        connection.makefile().readline()
                        connection.sendall((json.dumps({"ok": True, "actors": [actor]}) +
                                            "\n").encode())
                        time.sleep(.05)
                        connection.sendall(b'{"ok":false,"error":"watch rejected"}\n')

            threading.Thread(target=serve, daemon=True).start()
            self.assertTrue(ready.wait(1))
            changed = queue.Queue()
            stopped = threading.Event()
            with mock.patch.dict(os.environ, {"LOOP_SOCKET": str(path)}):
                watcher = AlanWatcher(changed, stopped)
                deadline = time.monotonic() + 2
                while watcher.actors and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(watcher.actors, [])
                self.assertFalse(watcher.available)
                self.assertEqual(watcher.error, "watch rejected")
                self.assertGreaterEqual(changed.qsize(), 2)
                stopped.set()

    def test_alan_attach_execs_the_declared_jupyter_connection_file(self):
        actor = alan_inventory("lovelace", [{
            "addr": "python-1", "type": "python", "state": "live",
            "label": "analysis", "cwd": "/work",
            "attachment": {"kind": "jupyter", "connection_file": "/run/kernel.json"},
        }])[0]
        with mock.patch("fleet_next.viewer.find", return_value=actor), \
             mock.patch("os.execvp") as execute:
            viewer.attach(actor.ref.key)
        execute.assert_called_once_with(
            "jupyter", ["jupyter", "console", "--existing", "/run/kernel.json"])

    def test_alan_attach_execs_the_exact_native_codex_thread(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1", "type": "codex", "state": "live",
            "label": "review", "cwd": "/work", "native": {"id": "thread-1"},
            "attachment": {"kind": "codex", "socket": "/run/codex.sock",
                           "thread_id": "thread-1"},
        }])[0]
        with mock.patch("fleet_next.viewer.find", return_value=actor), \
             mock.patch("os.execvp") as execute:
            viewer.attach(actor.ref.key)
        execute.assert_called_once_with(
            "codex", ["codex", "resume", "--remote", "unix:///run/codex.sock", "thread-1"])

    def test_alan_attach_execs_the_declared_claude_tmux_session(self):
        actor = alan_inventory("lovelace", [{
            "addr": "claude-1", "type": "claude", "state": "waiting",
            "label": "review", "cwd": "/work", "native": {"id": "session-1"},
            "attachment": {"kind": "tmux", "session": "fleet@actor-claude-1"},
        }])[0]
        with mock.patch("fleet_next.viewer.find", return_value=actor), \
             mock.patch("os.execvp") as execute:
            viewer.attach(actor.ref.key)
        execute.assert_called_once_with(
            "tmux", ["tmux", "attach-session", "-t", "fleet@actor-claude-1"])

    def test_codex_tmux_attach_enables_native_nested_mouse_routing(self):
        session = self.session(os.uname().nodename)
        with mock.patch("fleet_next.viewer.find", return_value=session), \
             mock.patch("fleet_next.viewer.inventory", return_value=[session]), \
             mock.patch("subprocess.run") as run, \
             mock.patch("os.execvp") as execute:
            viewer.attach(session.ref.key)
        run.assert_called_once_with(
            ["tmux", "set-option", "-t", "$1", "mouse", "on"], check=True)
        execute.assert_called_once_with(
            "tmux", ["tmux", "attach-session", "-t", "$1"])

    def test_create_opens_a_real_tmux_session_in_main(self):
        host = os.uname().nodename
        with mock.patch("fleet_next.actions.muster_input",
                        side_effect=[host, "codex", "analysis.", "/work"]), \
             mock.patch("fleet_next.actions.host_command") as run, \
             mock.patch("fleet_next.actions.created_key",
                        return_value="source-key"), \
             mock.patch("fleet_next.actions.viewer.open_main") as show:
            actions.create()
        run.assert_called_once_with(
            host, "tmux", "new-session", "-d", "-s", "analysis", "-c", "/work",
            "codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never")
        show.assert_called_once_with("source-key")

    def test_tmux_name_normalization_preserves_spaces(self):
        self.assertEqual(session_name(" Test session. "), "Test session")
        self.assertEqual(session_name("docs:v2.1"), "docs-v2-1")

    def test_done_is_attention_not_agent_or_lifecycle_state(self):
        session = self.session("newton")
        done = Session(**{**session.__dict__, "attention": "done"})
        self.assertEqual((session.agent, done.agent), ("codex", "codex"))
        self.assertEqual(done.state, "waiting")

    def test_working_recency_is_human_activity(self):
        working = Session(**{**self.session("newton").__dict__,
                             "reported_state": "working", "recency": 20,
                             "human_activity": 10})
        waiting = Session(**{**working.__dict__, "reported_state": "waiting"})
        self.assertEqual(recency(working), 10)
        self.assertEqual(recency(waiting), 20)

    def test_working_without_observed_human_activity_does_not_follow_output(self):
        working = Session(**{**self.session("newton").__dict__,
                             "reported_state": "working", "recency": 20,
                             "human_activity": 0})
        self.assertEqual(recency(working), 0)

    def test_working_sorts_before_waiting_and_done(self):
        self.assertLess(STATE_ORDER["working"], STATE_ORDER["waiting"])

    def test_next_waiting_follows_active_and_wraps(self):
        working = Session(**{**self.session("newton", "$0").__dict__,
                             "reported_state": "working"})
        first = self.session("newton", "$1")
        second = self.session("lovelace", "$2")
        done = Session(**{**self.session("turing", "$3").__dict__,
                          "attention": "done"})
        sessions = [working, first, second, done]
        self.assertEqual(next_waiting_key(sessions, working.ref.key), first.ref.key)
        self.assertEqual(next_waiting_key(sessions, first.ref.key), second.ref.key)
        self.assertEqual(next_waiting_key(sessions, second.ref.key), first.ref.key)

    def test_source_key_contains_server_generation(self):
        session = self.session("newton")
        self.assertEqual(split_key(session.ref.key),
                         ("newton", "/tmp/tmux/default", 12, 10, "$1"))

    def test_new_cli_has_no_destructive_surface(self):
        root = Path(__file__).parents[1]
        paths = [root / "fleet-next", *(root / "fleet_next").glob("*.py")]
        source = "\n".join(path.read_text() for path in paths)
        for command in ("kill-session", "kill-window", "unlink-window"):
            self.assertNotIn(command, source)

    def test_commander_uses_native_codex(self):
        launcher = (Path(__file__).parents[1] / "fleet-commander").read_text()
        self.assertIn('.thread_name == "commander"', launcher)
        self.assertIn("codex --sandbox danger-full-access resume $id", launcher)
        self.assertIn('"$1" = restart', launcher)
        self.assertNotIn("fleet-next commander\"", launcher)

    def test_muster_and_main_route_to_the_lovelace_hub(self):
        root = Path(__file__).parents[1]
        muster = (root / "fleet-muster").read_text()
        main = (root / "fleet-viewer").read_text()
        service = (root / "fleet-next.service").read_text()
        self.assertIn('exec ssh -tt -o BatchMode=yes "$hub" fleet-muster', muster)
        self.assertIn('export SSH_AUTH_SOCK="/run/user/$(id -u)/gnupg/S.gpg-agent.ssh"',
                      muster)
        self.assertIn("new-session -d -s fleet@main", main)
        self.assertIn("set-option -t fleet@main prefix None", main)
        self.assertIn("set-option -t fleet@main mouse on", main)
        self.assertIn("set-option -t fleet@muster mouse off", muster)
        self.assertIn("fleet-next viewer-status main", main)
        self.assertIn("ConditionHost=lovelace", service)

    def test_muster_always_opens_the_global_main_viewer(self):
        source = (Path(__file__).parents[1] / "fleet_next/ui.py").read_text()
        self.assertIn("fleet-next show --slot main {1}", source)
        self.assertIn("load:pos({cursor()})+unbind(load)", source)
        self.assertIn('"--no-sort"', source)
        self.assertIn("enable-search+toggle-sort", source)
        self.assertNotIn('"--nth=2.."', source)
        self.assertIn("change-prompt(Search: )", source)
        self.assertIn("c:execute-silent(fleet-next create-tab)", source)
        self.assertIn("r:execute-silent(fleet-next rename-tab {1})", source)

    def test_create_opens_inside_the_muster(self):
        with mock.patch("subprocess.run") as run:
            actions.create_tab()
        run.assert_called_once_with(
            ["tmux", "new-window", "-t", "fleet@muster", "-n", "create",
             "exec fleet-next create"], check=True)

    def test_rename_opens_inside_the_muster(self):
        key = "lovelace:/tmp/tmux:1:2:$3"
        with mock.patch("subprocess.run") as run:
            actions.rename_tab(key)
        run.assert_called_once_with(
            ["tmux", "new-window", "-t", "fleet@muster", "-n", "rename",
             "exec fleet-next rename 'lovelace:/tmp/tmux:1:2:$3'"], check=True)

    def test_named_viewers_remain_local(self):
        launcher = (Path(__file__).parents[1] / "fleet-viewer").read_text()
        self.assertTrue(launcher.rstrip().endswith('exec fleet-next viewer --slot "$slot"'))

    def test_ssh_environment_uses_stable_agent_socket(self):
        environment = ssh_environment()
        self.assertEqual(environment["SSH_AUTH_SOCK"],
                         f"/run/user/{os.getuid()}/gnupg/S.gpg-agent.ssh")

    def test_viewer_uses_stable_agent_environment(self):
        source = (Path(__file__).parents[1] / "fleet_next/viewer.py").read_text()
        self.assertIn("ssh_environment().items()", source)
        self.assertIn("focus], check=True, env=ssh_environment()", source)

    def test_management_prompts_never_read_raw_terminal_input(self):
        source = (Path(__file__).parents[1] / "fleet_next/actions.py").read_text()
        self.assertNotRegex(source, r"(?<![A-Za-z_])input\(")
        self.assertIn('"rofi", "-dmenu"', source)

    def test_created_agents_skip_startup_permission_interstitials(self):
        self.assertEqual(agent_command("claude", "work"),
                         ["claude", "--dangerously-skip-permissions", "--name", "work"])
        self.assertEqual(agent_command("codex", "work"),
                         ["codex", "--sandbox", "danger-full-access",
                          "--ask-for-approval", "never"])

    def test_viewer_dismiss_is_an_explicit_clear(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as runtime:
            env = {**os.environ, "XDG_RUNTIME_DIR": runtime,
                   "PYTHONPATH": str(root)}
            process = subprocess.Popen([sys.executable, "-m", "fleet_next.cli",
                                        "viewer", "--slot", "test"], env=env)
            socket = Path(runtime) / "agent-fleet/viewer-test.sock"
            try:
                for _ in range(100):
                    if socket.exists():
                        break
                    time.sleep(.01)
                subprocess.run([sys.executable, "-m", "fleet_next.cli", "dismiss",
                                "--slot", "test"], env=env, check=True)
                code = "from fleet_next.viewer import slots; print(slots())"
                result = subprocess.run([sys.executable, "-c", code], env=env,
                                        text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.strip(), "[('test', '')]")
            finally:
                process.terminate()
                process.wait()

    def test_quota_only_events_force_an_inventory_emit(self):
        source = (Path(__file__).parents[1] / "fleet_next/tmux.py").read_text()
        self.assertIn('force = "quota" in events', source)
        self.assertIn("if serial != previous or force:", source)
