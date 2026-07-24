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
import contextlib
import io
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
from fleet_next.alan import refresh as alan_refresh
from fleet_next.tmux import refresh as tmux_refresh
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

    def test_alan_inventory_preserves_needs_action_and_human_activity(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1", "type": "codex", "state": "needs-action",
            "human_activity": 123, "native": {"id": "thread-1"},
            "attachment": {"kind": "codex", "socket": "/run/codex.sock",
                           "thread_id": "thread-1"},
        }])[0]
        self.assertEqual(actor.state, "needs-action")
        self.assertEqual(actor.human_activity, 123)

    def test_alan_inventory_keeps_migration_activity_until_later_human_input(self):
        actor = {"addr": "codex-1", "type": "codex", "state": "waiting",
                 "human_activity": 123, "native": {"id": "thread-1"},
                 "attachment": {"kind": "codex", "socket": "/run/codex.sock"}}
        baseline = {"codex-1": {"last_touch": 456, "provider": "codex",
                                "native_id": "thread-1"}}
        migrated = alan_inventory("lovelace", [actor], activity_baseline=baseline)[0]
        advanced = alan_inventory("lovelace", [{**actor, "human_activity": 789}],
                                   activity_baseline=baseline)[0]
        self.assertEqual(migrated.human_activity, 456)
        self.assertEqual(advanced.human_activity, 789)

    def test_alan_inventory_preserves_creation_time_as_recency_fallback(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1", "type": "codex", "state": "waiting",
            "created": 456, "human_activity": 0, "native": {"id": "thread-1"},
            "attachment": {"kind": "codex", "socket": "/run/codex.sock",
                           "thread_id": "thread-1"},
        }])[0]
        self.assertEqual(actor.created, 456)
        self.assertEqual(recency(actor), 456)

    def test_alan_refresh_waits_for_same_identity_and_usable_attachment(self):
        before = {"addr": "codex-1", "type": "codex", "state": "waiting",
                  "native": {"id": "thread-1"},
                  "attachment": {"kind": "codex", "socket": "/run/old"}}
        starting = {**before, "state": "starting", "attachment": {"kind": "none"}}
        ready = {**before, "attachment": {"kind": "codex", "socket": "/run/new"}}
        responses = [
            {"actors": [before]}, {"addr": "codex-1"},
            {"actors": [starting]}, {"actors": [ready]},
        ]
        with mock.patch("fleet_next.alan.request", side_effect=responses) as request, \
             mock.patch("fleet_next.alan.time.sleep"):
            alan_refresh("codex-1")
        self.assertEqual(request.call_args_list, [
            mock.call({"op": "list"}),
            mock.call({"op": "refresh", "addr": "codex-1"}),
            mock.call({"op": "list"}),
            mock.call({"op": "list"}),
        ])

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
        watcher.activity_baseline = {}
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

    def test_attention_replay_retains_migration_activity_across_later_attention(self):
        watcher = object.__new__(AlanWatcher)
        watcher.attention = {}
        watcher.activity_baseline = {}
        watcher._changed = queue.Queue()
        watcher._consumer = threading.Event()
        messages = [{"idx": 0, "payload": {
            "kind": "fleet_attention", "actor": "codex-1", "attention": "tracked",
            "last_touch": 456, "migration_id": "migration-1", "provider": "codex",
            "native_id": "thread-1", "source": {
                "host": "lovelace", "key": "tmux:key"}}}, {"idx": 1, "payload": {
                "kind": "fleet_attention", "actor": "codex-1", "attention": "done"}}]

        def response(_payload):
            watcher._consumer.set()
            return {"messages": messages}

        with mock.patch("fleet_next.alan.configured", return_value=True), \
             mock.patch("fleet_next.alan.request", side_effect=response):
            watcher._run_attention()
        self.assertEqual(watcher.attention["codex-1"], "done")
        self.assertEqual(watcher.activity_baseline["codex-1"], {
            "last_touch": 456, "provider": "codex", "native_id": "thread-1"})

    def test_migration_activity_requires_matching_actor_native_identity(self):
        actor = {"addr": "codex-1", "type": "codex", "state": "waiting",
                 "human_activity": 123, "native": {"id": "thread-new"},
                 "attachment": {"kind": "codex", "socket": "/run/codex.sock"}}
        baseline = {"codex-1": {"last_touch": 456, "provider": "codex",
                                "native_id": "thread-old"}}
        projected = alan_inventory(
            "lovelace", [actor], activity_baseline=baseline)[0]
        self.assertEqual(projected.human_activity, 123)

    def test_attention_event_cannot_set_activity_without_migration_provenance(self):
        watcher = object.__new__(AlanWatcher)
        watcher.attention = {}
        watcher.activity_baseline = {}
        watcher._changed = queue.Queue()
        watcher._consumer = threading.Event()

        def response(_payload):
            watcher._consumer.set()
            return {"messages": [{"idx": 0, "payload": {
                "kind": "fleet_attention", "actor": "codex-1",
                "attention": "tracked", "last_touch": 999}}]}

        with mock.patch("fleet_next.alan.configured", return_value=True), \
             mock.patch("fleet_next.alan.request", side_effect=response):
            watcher._run_attention()
        self.assertEqual(watcher.activity_baseline, {})

    def test_non_attachable_alan_actors_are_not_fleet_rows(self):
        self.assertEqual(alan_inventory("lovelace", [{
            "addr": "llm-1", "type": "llm", "state": "live", "label": "hidden",
            "attachment": {"kind": "none"},
        }]), [])

    def test_noninteractive_cli_uses_the_user_owned_alan_socket(self):
        root = Path(__file__).parents[1]
        self.assertNotIn("/etc/agent-fleet/alan-socket",
                         (root / "PKGBUILD").read_text())
        self.assertNotIn("LOOP_SOCKET=", (root / "fleet-next.service").read_text())
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("fleet_next.alan.Path.home", return_value=Path("/home/will")), \
             mock.patch("fleet_next.alan.Path.exists", return_value=True), \
             mock.patch("fleet_next.alan.Path.read_text",
                        return_value="/home/will/.local/state/alan/loop.sock\n"):
            self.assertEqual(alan_socket_path(),
                             Path("/home/will/.local/state/alan/loop.sock"))

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

    def test_main_viewer_focus_uses_its_attached_client(self):
        session = self.session(os.uname().nodename)
        with mock.patch("fleet_next.viewer.exchange") as exchange, \
             mock.patch("fleet_next.viewer.subprocess.run") as run, \
             mock.patch("fleet_next.viewer.os.open", return_value=7) as opened, \
             mock.patch("fleet_next.viewer.os.write") as write, \
             mock.patch("fleet_next.viewer.os.close"):
            run.return_value.stdout = "/dev/pts/16\n"
            viewer.request("main", session.ref.key)
        exchange.assert_called_once_with("main", f"OPEN {session.ref.key}")
        opened.assert_called_once_with(
            "/dev/pts/16", os.O_WRONLY | os.O_NOCTTY | os.O_NOFOLLOW)
        write.assert_called_once_with(7, b"\033[5t")

    def test_create_materializes_codex_as_an_alan_actor(self):
        host = os.uname().nodename
        with mock.patch("fleet_next.actions.muster_input",
                        side_effect=[host, "codex", "analysis.", "/work"]), \
             mock.patch("fleet_next.actions.host_command") as run, \
             mock.patch("fleet_next.actions.wait_for_projection") as wait, \
             mock.patch("fleet_next.actions.viewer.open_main") as show:
            run.return_value.stdout = "codex-deadbeef\n"
            actions.create()
        run.assert_called_once_with(
            host, "fleet-next", "alan-spawn", "codex", "analysis", "/work",
            capture_output=True)
        wait.assert_called_once_with(f"alan:{host}:codex-deadbeef")
        show.assert_called_once_with(f"alan:{host}:codex-deadbeef")

    def test_create_keeps_plain_shells_as_tmux_sessions(self):
        host = os.uname().nodename
        with mock.patch("fleet_next.actions.muster_input",
                        side_effect=[host, "shell", "terminal", "/work"]), \
             mock.patch("fleet_next.actions.host_command") as run, \
             mock.patch("fleet_next.actions.created_key", return_value="source-key"), \
             mock.patch("fleet_next.actions.viewer.open_main") as show:
            actions.create()
        run.assert_called_once_with(
            host, "tmux", "new-session", "-d", "-s", "terminal", "-c", "/work",
            os.environ.get("SHELL", "/bin/sh"))
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
        self.assertEqual(recency(waiting), 10)

    def test_working_without_observed_human_activity_uses_creation_not_output(self):
        working = Session(**{**self.session("newton").__dict__,
                             "reported_state": "working", "recency": 20,
                             "human_activity": 0})
        self.assertEqual(recency(working), working.created)

    def test_tmux_inventory_does_not_promote_client_activity_to_human_activity(self):
        source = (Path(__file__).parents[1] / "fleet_next/tmux.py").read_text()
        self.assertNotIn("#{client_activity}", source)

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
        paths = [root / "fleet-next", *(path for path in (root / "fleet_next").glob("*.py")
                                       if path.name != "migration.py")]
        source = "\n".join(path.read_text() for path in paths)
        for command in ("kill-session", "kill-window", "unlink-window"):
            self.assertNotIn(command, source)
        migration = (root / "fleet_next/migration.py").read_text()
        self.assertIn("kill-session", migration)
        self.assertNotIn("kill-window", migration)
        self.assertNotIn("unlink-window", migration)

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

    def test_refresh_routes_to_owner_and_reopens_every_matching_local_viewer(self):
        session = self.session("newton")
        with mock.patch("fleet_next.actions.find", return_value=session), \
             mock.patch("fleet_next.actions.viewer.slots",
                        return_value=[("main", session.ref.key),
                                      ("side", session.ref.key), ("other", "elsewhere")]), \
             mock.patch("fleet_next.actions.host_command") as command, \
             mock.patch("fleet_next.actions.viewer.request") as reopen:
            actions.refresh(session.ref.key)
        command.assert_called_once_with(
            "newton", "fleet-next", "refresh-local", session.ref.key,
            capture_output=True)
        self.assertEqual(reopen.call_args_list, [
            mock.call("main", session.ref.key), mock.call("side", session.ref.key)])

    def test_refresh_waits_for_global_projection_before_reopening(self):
        actor = alan_inventory("lovelace", [{
            "addr": "claude-1", "type": "claude", "state": "waiting",
            "native": {"id": "session-1"},
            "attachment": {"kind": "tmux", "session": "fleet@actor-claude-1"},
        }])[0]
        with mock.patch("fleet_next.actions.find",
                        side_effect=[actor, SystemExit("gone"), actor]), \
             mock.patch("fleet_next.actions.time.sleep"), \
             mock.patch("fleet_next.actions.viewer.slots",
                        return_value=[("main", actor.ref.key)]), \
             mock.patch("fleet_next.actions.host_command"), \
             mock.patch("fleet_next.actions.viewer.request") as reopen:
            actions.refresh(actor.ref.key)
        reopen.assert_called_once_with("main", actor.ref.key)

    def test_refresh_local_dispatches_alan_by_exact_tagged_key(self):
        host = os.uname().nodename
        with mock.patch("fleet_next.actions.alan_refresh") as refresh:
            actions.refresh_local(f"alan:{host}:claude-1")
        refresh.assert_called_once_with("claude-1")

    def test_legacy_codex_refresh_uses_exact_resume_argv_and_identity_guard(self):
        host = os.uname().nodename
        item = Session(SessionRef(ServerRef(host, "/tmp/tmux", 12, 10), "$1"),
                       "work", 1, 2, 0, 1, "codex", "waiting", "/work", "tracked",
                       "codex", "waiting", transcript_id="thread-1", human_activity=7)
        pane = mock.Mock(pane_id="%1")
        pane.pane_id = "%1"
        native = mock.Mock(socket_path="/tmp/tmux", pid=12, start_time=10,
                           windows=[mock.Mock(panes=[pane])])
        tmux = mock.Mock()
        tmux.cmd.return_value.stdout = []
        key = item.ref.key
        with mock.patch("fleet_next.tmux.inventory", return_value=[item]), \
             mock.patch("fleet_next.tmux.observe", return_value=[item]), \
             mock.patch("fleet_next.tmux.server", return_value=tmux), \
             mock.patch("fleet_next.tmux.TmuxSession.from_session_id",
                        return_value=native):
            tmux_refresh(key)
        command = tmux.cmd.call_args.args
        self.assertEqual(command[:4], ("if-shell", "-t", "%1", "-F"))
        self.assertIn("respawn-pane -k -t %1 -c /work codex --sandbox danger-full-access --ask-for-approval never resume thread-1",
                      command[5])
        self.assertEqual(command[6], "display-message -p FLEET_STALE")

    def test_legacy_refresh_refuses_working_and_multi_pane_sessions(self):
        host = os.uname().nodename
        base = Session(SessionRef(ServerRef(host, "/tmp/tmux", 12, 10), "$1"),
                       "work", 1, 2, 0, 1, "claude", "waiting", "/work", "tracked",
                       "claude", "working", transcript_id="session-1")
        with mock.patch("fleet_next.tmux.inventory", return_value=[base]), \
             mock.patch("fleet_next.tmux.observe", return_value=[base]):
            with self.assertRaisesRegex(SystemExit, "requires waiting"):
                tmux_refresh(base.ref.key)

        waiting = Session(**{**base.__dict__, "reported_state": "waiting"})
        panes = [mock.Mock(pane_id="%1"), mock.Mock(pane_id="%2")]
        native = mock.Mock(socket_path="/tmp/tmux", pid=12, start_time=10,
                           windows=[mock.Mock(panes=panes)])
        with mock.patch("fleet_next.tmux.inventory", return_value=[waiting]), \
             mock.patch("fleet_next.tmux.observe", return_value=[waiting]), \
             mock.patch("fleet_next.tmux.server"), \
             mock.patch("fleet_next.tmux.TmuxSession.from_session_id",
                        return_value=native):
            with self.assertRaisesRegex(SystemExit, "exactly one agent pane"):
                tmux_refresh(waiting.ref.key)

    def test_failed_refresh_reopens_a_still_usable_source_then_reports_failure(self):
        session = self.session("newton")
        failure = subprocess.CalledProcessError(1, ["ssh"])
        with mock.patch("fleet_next.actions.find", return_value=session), \
             mock.patch("fleet_next.actions.viewer.slots",
                        return_value=[("main", session.ref.key)]), \
             mock.patch("fleet_next.actions.host_command",
                        side_effect=[failure, mock.Mock()]), \
             mock.patch("fleet_next.actions.viewer.request") as reopen:
            with self.assertRaises(subprocess.CalledProcessError):
                actions.refresh(session.ref.key)
        reopen.assert_called_once_with("main", session.ref.key)

    def test_failed_refresh_does_not_reopen_from_stale_global_projection(self):
        session = self.session("newton")
        failure = subprocess.CalledProcessError(1, ["ssh"])
        unavailable = subprocess.CalledProcessError(1, ["ssh"])
        with mock.patch("fleet_next.actions.find", return_value=session), \
             mock.patch("fleet_next.actions.viewer.slots",
                        return_value=[("main", session.ref.key)]), \
             mock.patch("fleet_next.actions.host_command",
                        side_effect=[failure, unavailable]) as command, \
             mock.patch("fleet_next.actions.viewer.request") as reopen:
            with self.assertRaises(subprocess.CalledProcessError):
                actions.refresh(session.ref.key)
        self.assertEqual(command.call_args_list[1], mock.call(
            "newton", "fleet-next", "refresh-check", session.ref.key, "",
            capture_output=True))
        reopen.assert_not_called()

    def test_refresh_report_keeps_nonzero_failure_and_displays_reason_in_muster(self):
        failure = subprocess.CalledProcessError(1, ["ssh"], stderr="actor_not_idle\n")
        with mock.patch("fleet_next.actions.refresh", side_effect=failure), \
             mock.patch("fleet_next.actions.subprocess.run") as run:
            with self.assertRaisesRegex(SystemExit, "actor_not_idle"):
                actions.refresh_report("alan:newton:codex-1")
        run.assert_called_once_with([
            "tmux", "display-message", "-t", "fleet@muster",
            "Refresh failed: actor_not_idle"])

    def test_refresh_all_uses_one_snapshot_and_reports_every_outcome(self):
        waiting = self.session("lovelace", "$1")
        waiting = Session(**{**waiting.__dict__, "agent_name": "codex",
                             "reported_state": "waiting", "transcript_id": "thread-1"})
        working = Session(**{**self.session("newton", "$2").__dict__,
                             "agent_name": "claude", "reported_state": "working",
                             "transcript_id": "session-2"})
        unsupported = Session(**{**self.session("turing", "$3").__dict__,
                                 "agent_name": "python"})
        remote = Session(**{**self.session("boltzmann", "$4").__dict__,
                            "agent_name": "claude", "transcript_id": "session-4"})
        output = io.StringIO()
        with mock.patch("fleet_next.actions.snapshot", return_value="snapshot"), \
             mock.patch("fleet_next.actions.decode_message",
                        return_value=([waiting, working, unsupported, remote], {},
                                      ["boltzmann"])), \
             mock.patch("fleet_next.actions.refresh") as refresh, \
             contextlib.redirect_stdout(output):
            actions.refresh_all()
        refresh.assert_called_once_with(waiting.ref.key)
        self.assertEqual(output.getvalue().splitlines(), sorted([
            f"{waiting.ref.key}\trefreshed",
            f"{working.ref.key}\tskipped: working",
            f"{unsupported.ref.key}\tskipped: unsupported-python",
            f"{remote.ref.key}\tskipped: unavailable",
        ]))

    def test_refresh_all_continues_then_fails_after_eligible_failure(self):
        first = Session(**{**self.session("lovelace", "$1").__dict__,
                           "agent_name": "codex", "transcript_id": "thread-1"})
        second = Session(**{**self.session("newton", "$2").__dict__,
                            "agent_name": "claude", "transcript_id": "session-2"})
        failure = subprocess.CalledProcessError(
            1, ["ssh", "newton"], stderr="replacement\nfailed\tremotely\n")
        output = io.StringIO()
        with mock.patch("fleet_next.actions.snapshot", return_value="snapshot"), \
             mock.patch("fleet_next.actions.decode_message",
                        return_value=([second, first], {}, [])), \
             mock.patch("fleet_next.actions.refresh", side_effect=[failure, None]) as refresh, \
             contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "1"):
                actions.refresh_all()
        self.assertEqual(refresh.call_count, 2)
        self.assertIn(f"{first.ref.key}\tfailed: replacement failed remotely",
                      output.getvalue())
        self.assertIn(f"{second.ref.key}\trefreshed", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_refresh_reopens_viewer_after_its_real_attachment_child_exits(self):
        session = self.session(os.uname().nodename)
        with tempfile.TemporaryDirectory() as runtime, \
             mock.patch("fleet_next.viewer.RUNTIME", Path(runtime)), \
             mock.patch("fleet_next.viewer.command",
                        side_effect=[["sleep", ".05"], ["sleep", ".5"]]), \
             mock.patch("fleet_next.viewer.subprocess.run"), \
             mock.patch("fleet_next.actions.find", return_value=session), \
             mock.patch("fleet_next.actions.host_command",
                        side_effect=lambda *_args, **_kwargs: time.sleep(.1)):
            thread = threading.Thread(target=viewer.serve, args=("refresh",), daemon=True)
            thread.start()
            socket_path = Path(runtime) / "viewer-refresh.sock"
            for _ in range(100):
                if socket_path.exists():
                    break
                time.sleep(.01)
            viewer.request("refresh", session.ref.key)
            actions.refresh(session.ref.key)
            self.assertEqual(viewer.exchange("refresh", "STATUS"), session.ref.key)

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
