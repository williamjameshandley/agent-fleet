import unittest
import asyncio
import os
import pty
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

import loop

from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.protocol import decode, encode
from agent_fleet.ui import AGENT_COLOUR, STATE_ORDER, recency
from agent_fleet.tmux import split_key
from agent_fleet.actions import next_waiting_key, session_name
from agent_fleet import actions
from agent_fleet.config import machine, ssh_environment
from agent_fleet.alan import inventory as alan_inventory
from agent_fleet.alan import Watcher as AlanWatcher
from agent_fleet.alan import refresh as alan_refresh
from agent_fleet.alan import resume as alan_resume
from agent_fleet import viewer
from agent_fleet import workstation
from agent_fleet import tmux
from agent_fleet import ui
from agent_fleet import cli
from agent_fleet.daemon import Fleet


class IdentityTests(unittest.TestCase):
    def session(self, host, sid="$1"):
        return Session(SessionRef(ServerRef(host, "/tmp/tmux/default", 12, 10), sid),
                       "work", 1, 2, 0, 1, "codex", "waiting", "/work")

    def test_identical_tmux_ids_on_different_hosts_are_distinct(self):
        self.assertNotEqual(self.session("newton").ref, self.session("lovelace").ref)

    def test_event_collector_cannot_create_tmux_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                **os.environ,
                "HOME": str(root / "home"),
                "TMUX_TMPDIR": str(root / "tmux"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "PYTHONPATH": str(Path(__file__).parents[1]),
            }
            for path in ("home", "tmux", "runtime"):
                (root / path).mkdir()
            result = subprocess.run(
                [sys.executable, "-c",
                 "from agent_fleet.tmux import event_stream; next(event_stream('fixture'))"],
                text=True, capture_output=True, env=environment)
            socket_path = root / "tmux" / f"tmux-{os.getuid()}" / "default"
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tmux server is not running", result.stderr)
            self.assertFalse(socket_path.exists())

    def muster_state(self, matches, count=None):
        state = json.dumps({"matches": matches,
                            "matchCount": len(matches) if count is None else count})
        return subprocess.CompletedProcess([], 0, stdout=state)

    def test_cursor_position_comes_from_musters_loaded_identities(self):
        state = self.muster_state([{"text": "actor:first\tfirst"},
                                   {"text": "actor:focused\tfocused"}])
        with mock.patch.object(ui.viewer, "slots",
                               return_value=[("main", "actor:focused")]), \
                mock.patch("agent_fleet.ui.subprocess.run", return_value=state):
            self.assertEqual(ui.cursor(), "pos(2)")

    def test_cursor_placement_is_one_action_against_musters_own_list(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "muster.sock").touch()
            with mock.patch.object(ui, "RUNTIME", Path(directory)), \
                    mock.patch("agent_fleet.ui.subprocess.run",
                               return_value=subprocess.CompletedProcess([], 0)) as run:
                ui.select()

        run.assert_called_once()
        self.assertIn("transform(fleet cursor)", run.call_args.args[0])

    def test_cursor_refuses_a_truncated_match_list(self):
        state = self.muster_state([{"text": "actor:first\tfirst"}], count=2)
        with mock.patch.object(ui.viewer, "slots",
                               return_value=[("main", "actor:first")]), \
                mock.patch("agent_fleet.ui.subprocess.run", return_value=state):
            with self.assertRaises(SystemExit):
                ui.cursor()

    def test_machine_labels_are_single_cell_and_noether_uses_ligature(self):
        self.assertEqual([machine(host) for host in
                          ("newton", "lovelace", "boltzmann", "turing", "noether")],
                         ["N", "L", "B", "T", "Œ"])

    def test_viewer_force_reaps_a_signal_ignoring_process_group(self):
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import signal,time; signal.signal(signal.SIGHUP, signal.SIG_IGN); time.sleep(30)"],
            start_new_session=True)
        time.sleep(.05)
        started = time.monotonic()
        viewer.stop_child(child)
        self.assertLess(time.monotonic() - started, .5)
        self.assertIsNotNone(child.returncode)

    def test_protocol_round_trip_preserves_canonical_identity(self):
        sessions = [self.session("newton"), self.session("lovelace")]
        self.assertEqual(decode(encode(sessions)), sessions)

    def test_preview_daemon_rejects_stale_and_malformed_keys_before_dispatch(self):
        fleet = Fleet()
        fleet.sessions = {"lovelace": [self.session("lovelace")]}
        for key in ["lovelace:/tmp/tmux/default:12:10:$gone", "malformed"]:
            with self.assertRaises(RuntimeError) as raised:
                asyncio.run(fleet.preview(key))
            self.assertEqual(str(raised.exception), f"session disappeared: {key}")

        fleet.unavailable = {"lovelace"}
        with self.assertRaises(RuntimeError) as raised:
            asyncio.run(fleet.preview(self.session("lovelace").ref.key))
        self.assertEqual(
            str(raised.exception), "lovelace is disconnected; refusing action"
        )

    def test_preview_fast_path_preserves_argument_errors(self):
        executable = Path(__file__).parents[1] / "fleet"
        for arguments in [[], ["key", "bad"], ["key", "1", "2", "extra"]]:
            result = subprocess.run(
                [executable, "preview", *arguments], text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage: fleet preview", result.stderr)

    def test_muster_refresh_waits_for_three_seconds_of_client_inactivity(self):
        class Process:
            returncode = 0

            def __init__(self, output):
                self.output = output

            async def communicate(self):
                return self.output, b""

        processes = [Process(b"98\n99\n"), Process(b"98\n99\n")]

        async def create(*_args, **_kwargs):
            return processes.pop(0)

        with mock.patch("agent_fleet.daemon.asyncio.create_subprocess_exec",
                        side_effect=create) as execute, \
             mock.patch("agent_fleet.daemon.asyncio.sleep",
                        new_callable=mock.AsyncMock) as sleep, \
             mock.patch("agent_fleet.daemon.time.time",
                        side_effect=[100, 102]):
            asyncio.run(Fleet().wait_for_muster_idle())

        sleep.assert_awaited_once_with(2)
        self.assertEqual(execute.call_count, 2)

    def test_real_muster_input_survives_reload_after_an_alan_watch_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "agent-fleet"
            runtime.mkdir()
            tmux_runtime = root / "tmux"
            tmux_runtime.mkdir()
            muster_socket = runtime / "muster.sock"
            fleet_socket = runtime / "fleet.sock"
            alan_socket = root / "alan.sock"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fleet_executable = Path(__file__).parents[1] / "fleet"
            ssh = bin_dir / "ssh"
            ssh.write_text(f"#!/bin/sh\nexec {fleet_executable} projection\n")
            ssh.chmod(0o755)
            host = os.uname().nodename
            fleet = Fleet()
            stopped = threading.Event()
            emit_update = threading.Event()
            initial = {
                "addr": "codex-one", "type": "codex", "state": "waiting",
                "label": "needle row", "cwd": str(root), "created": 1,
                "native": {"id": "native-one"},
                "attachment": {"kind": "tmux", "session": "fleet@codex-one"},
            }
            updated = {**initial, "label": "new needle"}

            def serve_projection():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(fleet_socket))
                    server.listen()
                    server.settimeout(.1)
                    while not stopped.is_set():
                        try:
                            connection, _ = server.accept()
                        except TimeoutError:
                            continue
                        with connection:
                            request = connection.makefile("rb").readline()
                            if request == b"snapshot\n":
                                sessions = [
                                    session for group in fleet.sessions.values()
                                    for session in group
                                ]
                                connection.sendall(
                                    (encode(sessions, {}, []) + "\n").encode()
                                )

            def serve_watch():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(alan_socket))
                    server.listen()
                    connection, _ = server.accept()
                    with connection:
                        connection.makefile("rb").readline()
                        for actors in ([initial], [updated]):
                            connection.sendall(
                                (json.dumps({"ok": True, "actors": actors}) + "\n").encode()
                            )
                            if actors == [initial]:
                                emit_update.wait(5)
                        stopped.wait(5)

            projection_server = threading.Thread(
                target=serve_projection, daemon=True)
            watch_server = threading.Thread(target=serve_watch, daemon=True)
            projection_server.start()
            watch_server.start()
            for path in (fleet_socket, alan_socket):
                for _ in range(100):
                    if path.exists():
                        break
                    time.sleep(.01)
                self.assertTrue(path.exists())

            command = (
                f"printf 'alpha\\nneedle row\\nomega\\n' | "
                f"exec fzf --listen {muster_socket}"
            )
            environment = {
                **os.environ,
                "LOOP_SOCKET": str(alan_socket),
                "PYTHONPATH": (
                    f"{Path(loop.__file__).parents[1]}:"
                    f"{Path(__file__).parents[1]}"
                ),
                "PATH": f"{bin_dir}:{Path(__file__).parents[1]}:{os.environ['PATH']}",
                "TMUX_TMPDIR": str(tmux_runtime),
                "XDG_RUNTIME_DIR": str(root),
            }
            subprocess.run([
                "tmux", "new-session", "-d", "-s", "fleet@muster", command,
            ], check=True, env=environment)
            master = slave = None
            client = None
            try:
                for _ in range(500):
                    if muster_socket.exists():
                        break
                    time.sleep(.01)
                self.assertTrue(muster_socket.exists())

                async def wait_for(predicate):
                    for _ in range(200):
                        value = predicate()
                        if value:
                            return value
                        await asyncio.sleep(.02)
                    self.fail("fixture state did not arrive")

                def fzf_state():
                    result = subprocess.run(
                        ["curl", "-fsS", "--unix-socket", str(muster_socket),
                         "http://localhost"],
                        text=True, capture_output=True)
                    return json.loads(result.stdout) if result.returncode == 0 else {}

                async def exercise():
                    collector = asyncio.create_task(fleet.collect(host))
                    try:
                        await wait_for(
                            lambda: fleet.sessions.get(host)
                            and fleet.sessions[host][0].name == "needle row"
                        )
                        await wait_for(
                            lambda: "alan:" in
                            fzf_state().get("current", {}).get("text", "")
                        )

                        nonlocal master, slave, client
                        master, slave = pty.openpty()
                        client = subprocess.Popen(
                            ["tmux", "-u", "attach-session", "-t", "fleet@muster"],
                            stdin=slave, stdout=slave, stderr=slave,
                            env={**environment, "TERM": "xterm-256color"},
                            start_new_session=True)
                        os.close(slave)
                        slave = None
                        await asyncio.sleep(.2)
                        os.write(master, b"needle")
                        await wait_for(lambda: fzf_state().get("query") == "needle")
                        initial_text = fzf_state()["current"]["text"]
                        self.assertIn("needle row", initial_text)

                        emit_update.set()
                        await wait_for(
                            lambda: fleet.sessions.get(host)
                            and fleet.sessions[host][0].name == "new needle"
                            and fleet.refresh_pending
                        )
                        await asyncio.sleep(.2)
                        before = fzf_state()
                        self.assertEqual(before["current"]["text"], initial_text)

                        after = await wait_for(
                            lambda: (
                                state if
                                "new needle" in
                                (state := fzf_state()).get("current", {}).get("text", "")
                                else None
                            )
                        )
                        self.assertEqual(after["query"], "needle")
                    finally:
                        collector.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await collector

                with mock.patch.dict(os.environ, environment), \
                     mock.patch("agent_fleet.daemon.RUNTIME", runtime):
                    asyncio.run(exercise())
            finally:
                stopped.set()
                emit_update.set()
                projection_server.join(1)
                watch_server.join(1)
                if client is not None:
                    client.terminate()
                    client.wait(timeout=2)
                if slave is not None:
                    os.close(slave)
                if master is not None:
                    os.close(master)
                subprocess.run(
                    ["tmux", "kill-server"], env=environment,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_alan_inventory_preserves_the_presentation_backed_interface(self):
        visible = {
            "addr": "codex-current", "type": "codex", "state": "waiting",
            "label": "current", "native": {"id": "thread-1"},
            "attachment": {"kind": "tmux", "session": "fleet@codex-current"},
        }
        claude = {
            "addr": "claude-current", "type": "claude", "state": "working",
            "label": "claude", "native": {"id": "session-1"},
            "attachment": {"kind": "tmux", "session": "fleet@claude-current"},
        }
        hidden = [
            {"addr": "claude-home-1", "type": "claude", "state": "unavailable",
             "label": "alan-home_1", "attachment": {"kind": "none"}},
            {"addr": "claude-home-1b", "type": "claude", "state": "unavailable",
             "label": "alan-home_1b", "attachment": {"kind": "none"}},
            {"addr": "codex-headless", "type": "codex", "state": "waiting",
             "attachment": {"kind": "none"}},
            {"addr": "python-live", "type": "python", "state": "live",
             "attachment": {"kind": "jupyter", "connection_file": "/run/kernel.json"}},
            {"addr": "llm-live", "type": "llm", "state": "waiting",
             "attachment": {"kind": "none"}},
            {"addr": "codex-failed", "type": "codex", "state": "failed",
             "attachment": visible["attachment"]},
            {"addr": "claude-retired", "type": "claude", "state": "retired",
             "attachment": claude["attachment"]},
            {"addr": "codex-review", "type": "codex", "state": "waiting",
             "profile": {"view": [], "net": "none"},
             "attachment": visible["attachment"]},
        ]
        actors = alan_inventory("lovelace", [visible, *hidden, claude])
        self.assertEqual([actor.ref.session_id for actor in actors],
                         ["codex-current", "claude-current"])

    def test_alan_inventory_preserves_needs_action_and_human_activity(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1", "type": "codex", "state": "needs-action",
            "human_activity": 123, "native": {"id": "thread-1"},
            "attachment": {"kind": "codex", "socket": "/run/codex.sock",
                           "thread_id": "thread-1"},
        }])[0]
        self.assertEqual(actor.state, "needs-action")
        self.assertEqual(actor.human_activity, 123)

    def test_alan_inventory_preserves_creation_time_as_recency_fallback(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1", "type": "codex", "state": "waiting",
            "created": 456, "human_activity": 0, "native": {"id": "thread-1"},
            "attachment": {"kind": "codex", "socket": "/run/codex.sock",
                           "thread_id": "thread-1"},
        }])[0]
        self.assertEqual(actor.created, 456)
        self.assertEqual(recency(actor), 456)

    def test_alan_refresh_returns_at_alans_native_ready_boundary(self):
        before = {"addr": "codex-1", "type": "codex", "state": "waiting",
                  "native": {"id": "thread-1"},
                  "attachment": {"kind": "codex", "socket": "/run/old"}}
        with mock.patch("agent_fleet.alan.actors", return_value=[before]), \
             mock.patch("agent_fleet.alan.loop.refresh") as refresh:
            alan_refresh("codex-1")
        refresh.assert_called_once_with("codex-1")

    def test_alan_open_returns_at_alans_native_ready_boundary(self):
        archived = {"addr": "codex-1", "type": "codex", "state": "retired",
                    "native": {"id": "thread-1"}, "attachment": {"kind": "none"}}
        with mock.patch("agent_fleet.alan.actors", return_value=[archived]), \
             mock.patch("agent_fleet.alan.loop.spawn", return_value="codex-1") as spawn:
            self.assertEqual(alan_resume("codex-1"), "codex-1")
        spawn.assert_called_once_with("codex-1")

    def test_alan_inventory_uses_native_human_activity(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1", "type": "codex", "state": "waiting",
            "human_activity": 123,
            "attachment": {"kind": "tmux", "session": "fleet@codex-work-1",
                           "generation": "thread-1"},
        }])[0]
        self.assertEqual(actor.human_activity, 123)

    def test_non_attachable_alan_actors_are_not_fleet_rows(self):
        self.assertEqual(alan_inventory("lovelace", [{
            "addr": "llm-1", "type": "llm", "state": "live", "label": "hidden",
            "attachment": {"kind": "none"},
        }]), [])

    def test_profiled_python_reviewers_are_not_fleet_rows(self):
        self.assertEqual(alan_inventory("lovelace", [{
            "addr": "python-review", "type": "python", "state": "waiting",
            "label": "reviewer", "profile": {"view": [], "net": "none"},
            "attachment": {"kind": "jupyter", "connection_file": "/run/review.json"},
        }]), [])

    def test_fleet_has_no_alan_protocol_or_socket_implementation(self):
        root = Path(__file__).parents[1]
        source = (root / "agent_fleet/alan.py").read_text()
        self.assertNotIn("AF_UNIX", source)
        self.assertNotIn("json.", source)
        self.assertNotIn("alan-socket", source)

    def test_watch_failure_clears_actor_rows_and_retries(self):
        actor = {"addr": "python-1", "type": "python", "state": "live",
                 "attachment": {"kind": "jupyter", "connection_file": "/run/k.json"}}
        stopped = threading.Event()

        def watch():
            yield [actor]
            raise OSError("watch rejected")

        changed = queue.Queue()
        with mock.patch("agent_fleet.alan.loop.watch", side_effect=watch), \
             mock.patch("agent_fleet.alan.time.sleep", side_effect=lambda _: stopped.set()):
            watcher = AlanWatcher(changed, stopped)
            watcher._thread.join(1)
        self.assertEqual(watcher.actors, [])
        self.assertFalse(watcher.available)
        self.assertEqual(watcher.error, "Alan unavailable: watch rejected")
        self.assertEqual(changed.qsize(), 2)

    def test_alan_attach_execs_the_declared_codex_tmux_session(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1", "type": "codex", "state": "live",
            "label": "review", "cwd": "/work", "native": {"id": "thread-1"},
            "attachment": {"kind": "tmux", "session": "fleet@codex-review-1",
                           "generation": "thread-1"},
        }])[0]
        descriptor = {
            "addr": "codex-1", "type": "codex", "state": "live",
            "label": "review", "cwd": "/work", "native": {"id": "thread-1"},
            "attachment": actor.attachment}
        with mock.patch("agent_fleet.viewer.alan.actors",
                        return_value=[descriptor]), \
             mock.patch("agent_fleet.viewer.alan.attachment",
                        return_value=actor.attachment) as attachment, \
             mock.patch("agent_fleet.viewer.subprocess.run",
                        return_value=mock.Mock(stdout="thread-1\n")), \
             mock.patch("os.execvp") as execute:
            viewer.attach(actor.ref.key)
        attachment.assert_called_once_with("codex-1")
        execute.assert_called_once_with(
            "tmux", ["tmux", "attach-session", "-t", "fleet@codex-review-1"])

    def test_show_still_resolves_the_global_source_before_requesting_a_viewer(self):
        session = self.session("newton")
        with mock.patch("agent_fleet.viewer.find", return_value=session) as find, \
             mock.patch("agent_fleet.viewer.request") as request:
            viewer.show(session.ref.key, "main")
        find.assert_called_once_with(session.ref.key)
        request.assert_called_once_with("main", session.ref.key)

    def test_alan_attach_execs_the_declared_claude_tmux_session(self):
        actor = alan_inventory("lovelace", [{
            "addr": "claude-1", "type": "claude", "state": "waiting",
            "label": "review", "cwd": "/work", "native": {"id": "session-1"},
            "attachment": {"kind": "tmux", "session": "fleet@claude-review-1",
                           "generation": "session-1"},
        }])[0]
        descriptor = {
            "addr": "claude-1", "type": "claude", "state": "waiting",
            "label": "review", "cwd": "/work", "native": {"id": "session-1"},
            "attachment": actor.attachment}
        with mock.patch("agent_fleet.viewer.alan.actors",
                        return_value=[descriptor]), \
             mock.patch("agent_fleet.viewer.alan.attachment",
                        return_value=actor.attachment) as attachment, \
             mock.patch("agent_fleet.viewer.subprocess.run",
                        return_value=mock.Mock(stdout="session-1\n")), \
             mock.patch("os.execvp") as execute:
            viewer.attach(actor.ref.key)
        attachment.assert_called_once_with("claude-1")
        execute.assert_called_once_with(
            "tmux", ["tmux", "attach-session", "-t", "fleet@claude-review-1"])

    def test_codex_tmux_attach_enables_native_nested_mouse_routing(self):
        session = self.session(os.uname().nodename)
        with mock.patch("agent_fleet.viewer.inventory", return_value=[session]), \
             mock.patch("subprocess.run") as run, \
             mock.patch("os.execvp") as execute:
            viewer.attach(session.ref.key)
        run.assert_called_once_with(
            ["tmux", "set-option", "-t", "$1", "mouse", "on"], check=True)
        execute.assert_called_once_with(
            "tmux", ["tmux", "attach-session", "-t", "$1"])

    def test_main_viewer_focus_uses_workstation_reverse_socket(self):
        session = self.session(os.uname().nodename)
        with mock.patch("agent_fleet.viewer.exchange") as exchange, \
             mock.patch("agent_fleet.viewer.subprocess.run") as run, \
             mock.patch("agent_fleet.viewer.workstation.request") as request:
            run.return_value.stdout = "boltzmann\n"
            viewer.request("main", session.ref.key)
        exchange.assert_called_once_with("main", f"OPEN {session.ref.key}")
        request.assert_called_once_with(
            "boltzmann", {"operation": "focus", "slot": "main"})

    def test_workstation_server_exposes_only_focus_and_prompt(self):
        with mock.patch("agent_fleet.workstation.subprocess.run") as run:
            run.return_value.returncode = 0
            workstation.dispatch({"operation": "focus", "slot": "main"})
        run.assert_called_once_with(
            ["i3-msg", '[instance="fleet-main"] focus'],
            text=True, capture_output=True)
        with self.assertRaisesRegex(RuntimeError, "unknown workstation operation"):
            workstation.dispatch({"operation": "exec", "command": ["sh"]})

    def test_create_materializes_codex_as_an_alan_actor(self):
        host = os.uname().nodename
        with mock.patch("agent_fleet.actions.muster_input",
                        side_effect=[host, "codex", "analysis.", "/work"]) as prompt, \
             mock.patch("agent_fleet.actions.host_command") as run, \
            mock.patch("agent_fleet.actions.wait_for_projection") as wait, \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            run.return_value.stdout = "codex-deadbeef\n"
            actions.create()
        self.assertEqual(
            prompt.call_args_list[1],
            mock.call("agent", ("codex", "claude"), context=host))
        run.assert_called_once_with(
            host, "alan-create", "--present", "codex", "analysis", "/work",
            stdout=subprocess.PIPE)
        wait.assert_called_once_with(f"alan:{host}:codex-deadbeef")
        show.assert_called_once_with(f"alan:{host}:codex-deadbeef")

    def test_remote_creator_is_batch_mode_and_leaves_failure_output_visible(self):
        host = "newton" if os.uname().nodename != "newton" else "lovelace"
        with mock.patch("agent_fleet.actions.subprocess.run") as run:
            actions.host_command(host, "alan-create", "--present", "codex",
                                 "work tree;safe", "/work dir/$literal",
                                 stdout=subprocess.PIPE)
        run.assert_called_once_with(
            ["ssh", "-T", "-o", "BatchMode=yes", host,
             "alan-create --present codex 'work tree;safe' '/work dir/$literal'"],
            text=True, check=True, capture_output=False, stdout=subprocess.PIPE)

    def test_create_uses_claudes_existing_provider_presentation(self):
        host = "newton" if os.uname().nodename != "newton" else "lovelace"
        with mock.patch("agent_fleet.actions.muster_input",
                        side_effect=[host, "claude", "analysis", "/work"]) as prompt, \
             mock.patch("agent_fleet.actions.host_command") as run, \
             mock.patch("agent_fleet.actions.wait_for_projection") as wait, \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            run.return_value.stdout = "claude-deadbeef\n"
            actions.create()
        self.assertEqual(
            prompt.call_args_list[1],
            mock.call("agent", ("codex", "claude"), context=host))
        run.assert_called_once_with(
            host, "alan-create", "claude", "analysis", "/work",
            stdout=subprocess.PIPE)
        wait.assert_called_once_with(f"alan:{host}:claude-deadbeef")
        show.assert_called_once_with(f"alan:{host}:claude-deadbeef")
        self.assertNotIn('"tmux", "new-session"',
                         (Path(__file__).parents[1] / "agent_fleet/actions.py").read_text())

    def test_fleet_package_requires_the_canonical_alan_client(self):
        package = (Path(__file__).parents[1] / "PKGBUILD").read_text()
        self.assertIn(
            "depends=('alan>=1:2.0.0.a11.r1785330889.g64ac36c' ", package)

    def test_projection_readiness_waits_for_a_presented_actor(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1", "type": "codex", "state": "waiting",
            "native": {"id": "thread-1"},
            "attachment": {"kind": "tmux", "session": "fleet@codex-1"},
        }])[0]
        with mock.patch("agent_fleet.actions.find",
                        side_effect=[SystemExit(), actor]), \
             mock.patch("agent_fleet.actions.time.sleep") as sleep:
            actions.wait_for_projection(actor.ref.key, "thread-1")
        sleep.assert_called_once_with(.1)

    def test_tmux_name_normalization_preserves_spaces(self):
        self.assertEqual(session_name(" Test session. "), "Test session")
        self.assertEqual(session_name("docs:v2.1"), "docs-v2-1")

    def test_archive_retires_exact_alan_actor_after_durable_identity_check(self):
        host = os.uname().nodename
        session = Session(
            SessionRef(ServerRef(host, "", 0, 0, "alan"), "codex-1"),
            "work", 1, 0, 0, 1, "tmux", "", "/work",
            "codex", "waiting", "", 0, "thread-1", {"kind": "tmux"}, 1)
        with mock.patch("agent_fleet.actions.find", return_value=session), \
             mock.patch("agent_fleet.actions.host_command") as command, \
             mock.patch("agent_fleet.actions.wait_for_absence") as absent, \
             mock.patch("agent_fleet.actions.viewer.slots", return_value=[("main", session.ref.key)]), \
             mock.patch("agent_fleet.actions.viewer.request") as request:
            actions.archive(session.ref.key)
        command.assert_called_once_with(host, "fleet", "alan-retire", "codex-1",
                                        capture_output=True)
        absent.assert_called_once_with(session.ref.key)
        request.assert_called_once_with("main", "")

    def test_archive_refuses_actor_without_native_identity_before_mutation(self):
        host = os.uname().nodename
        session = Session(
            SessionRef(ServerRef(host, "", 0, 0, "alan"), "codex-1"),
            "work", 1, 0, 0, 1, "tmux", "", "/work",
            "codex", "waiting")
        with mock.patch("agent_fleet.actions.find", return_value=session), \
             mock.patch("agent_fleet.actions.host_command") as command:
            with self.assertRaisesRegex(SystemExit, "durable Claude or Codex identity"):
                actions.archive(session.ref.key)
        command.assert_not_called()

    def test_archive_verifies_transcript_then_closes_exact_tmux_identity(self):
        session = self.session("lovelace")
        session = Session(**{**session.__dict__, "transcript_id": "thread-1"})
        with mock.patch("agent_fleet.actions.find", return_value=session), \
             mock.patch("agent_fleet.actions.host_command") as command, \
             mock.patch("agent_fleet.actions.wait_for_absence") as absent, \
             mock.patch("agent_fleet.actions.viewer.slots", return_value=[]):
            actions.archive(session.ref.key)
        self.assertEqual(command.call_args_list, [
            mock.call("lovelace", "fleet", "transcript-check", "codex", "thread-1",
                      capture_output=True),
            mock.call("lovelace", "fleet", "mutate", session.ref.key, "archive",
                      capture_output=True),
        ])
        absent.assert_called_once_with(session.ref.key)

    def test_tmux_archive_revalidates_full_source_before_kill(self):
        key = "lovelace:/tmp/tmux:12:10:$1"
        result = mock.Mock(stdout=[])
        server = mock.Mock()
        server.cmd.return_value = result
        with mock.patch("agent_fleet.tmux.server", return_value=server):
            tmux.mutate(key, "archive", [])
        command = server.cmd.call_args.args
        self.assertEqual(command[:4], ("if-shell", "-t", "$1", "-F"))
        self.assertIn("kill-session -t '$1'", command[5])
        self.assertIn("/tmp/tmux", command[4])
        self.assertIn("12", command[4])
        self.assertIn("10", command[4])

    def test_history_keeps_failed_alan_actor_and_suppresses_transcript_fallback(self):
        actor = {"addr": "codex-1", "type": "codex", "state": "failed",
                 "label": "work", "cwd": "/work", "created": 10,
                 "human_activity": 20, "native": {"id": "thread-1"}}

        def command(_host, _fleet, operation, *args, **_kwargs):
            payload = ([actor] if operation == "alan-actors" else [{
                "agent": "codex", "session_id": "thread-1", "mtime": 20,
                "name": "duplicate", "cwd": "/work"}])
            return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))

        output = io.StringIO()
        with mock.patch("agent_fleet.actions.hosts", return_value=["lovelace"]), \
             mock.patch("agent_fleet.actions.snapshot",
                        return_value='{"sessions":[],"usage":{},"unavailable":[]}'), \
             mock.patch("agent_fleet.actions.host_command", side_effect=command), \
             contextlib.redirect_stdout(output):
            actions.history()
        self.assertEqual(output.getvalue().splitlines(), [
            "alan:lovelace:codex-1\tlovelace\tcodex\twork\t/work"])

    def test_unavailable_alan_descriptor_does_not_erase_transcript_history(self):
        actor = {"addr": "codex-1", "type": "codex", "state": "unavailable",
                 "native": {"id": "thread-1"}}

        def command(_host, _fleet, operation, *args, **_kwargs):
            payload = ([actor] if operation == "alan-actors" else [{
                "agent": "codex", "session_id": "thread-1", "mtime": 20,
                "name": "work", "cwd": "/work"}])
            return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))

        output = io.StringIO()
        with mock.patch("agent_fleet.actions.hosts", return_value=["lovelace"]), \
             mock.patch("agent_fleet.actions.snapshot",
                        return_value='{"sessions":[],"usage":{},"unavailable":[]}'), \
             mock.patch("agent_fleet.actions.host_command", side_effect=command), \
             contextlib.redirect_stdout(output):
            actions.history()
        self.assertEqual(output.getvalue().splitlines(), [
            "lovelace:codex:thread-1\tlovelace\tcodex\twork\t/work"])

    def test_history_open_retries_the_same_alan_address(self):
        key = "alan:lovelace:codex-1"
        with mock.patch("agent_fleet.actions.host_command") as command, \
             mock.patch("agent_fleet.actions.wait_for_projection") as wait, \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            actions.open_history(key)
        command.assert_called_once_with("lovelace", "fleet", "alan-resume", "codex-1",
                                        capture_output=True)
        wait.assert_called_once_with(key)
        show.assert_called_once_with(key)

    def test_transcript_history_open_captures_remote_resume_failure(self):
        key = "lovelace:codex:full-thread-id"
        with mock.patch("agent_fleet.actions.snapshot",
                        return_value='{"sessions":[],"usage":{},"unavailable":[]}'), \
             mock.patch("agent_fleet.actions.desktop_input", return_value="work"), \
             mock.patch("agent_fleet.actions.host_command") as command, \
             mock.patch("agent_fleet.actions.created_key", return_value="new-key"), \
             mock.patch("agent_fleet.actions.viewer.request"):
            actions.open_history(key)
        command.assert_called_once_with(
            "lovelace", "fleet", "resume", "codex", "full-thread-id", "work",
            capture_output=True)

    def test_archive_failure_is_visible_in_muster(self):
        failure = subprocess.CalledProcessError(1, ["fleet"], stderr="retire refused")
        with mock.patch("agent_fleet.actions.archive", side_effect=failure), \
             mock.patch("agent_fleet.actions.subprocess.run") as run:
            with self.assertRaisesRegex(SystemExit, "retire refused"):
                actions.archive_report("alan:lovelace:codex-1")
        run.assert_called_once_with([
            "tmux", "display-message", "-t", "fleet@muster",
            "Archive failed: retire refused"])

    def test_history_open_failure_is_visible_in_muster(self):
        with mock.patch("agent_fleet.actions.open_history",
                        side_effect=RuntimeError("native identity changed")), \
             mock.patch("agent_fleet.actions.subprocess.run") as run:
            with self.assertRaisesRegex(SystemExit, "native identity changed"):
                actions.open_history_report("alan:lovelace:codex-1")
        run.assert_called_once_with([
            "tmux", "display-message", "-t", "fleet@muster",
            "Open failed: native identity changed"])

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
        source = (Path(__file__).parents[1] / "agent_fleet/tmux.py").read_text()
        self.assertNotIn("#{client_activity}", source)

    def test_working_sorts_before_waiting(self):
        self.assertLess(STATE_ORDER["working"], STATE_ORDER["waiting"])

    def test_next_waiting_follows_active_and_wraps(self):
        working = Session(**{**self.session("newton", "$0").__dict__,
                             "reported_state": "working"})
        first = self.session("newton", "$1")
        second = self.session("lovelace", "$2")
        sessions = [working, first, second]
        self.assertEqual(next_waiting_key(sessions, working.ref.key), first.ref.key)
        self.assertEqual(next_waiting_key(sessions, first.ref.key), second.ref.key)
        self.assertEqual(next_waiting_key(sessions, second.ref.key), first.ref.key)

    def test_source_key_contains_server_generation(self):
        session = self.session("newton")
        self.assertEqual(split_key(session.ref.key),
                         ("newton", "/tmp/tmux/default", 12, 10, "$1"))

    def test_archive_is_the_only_destructive_surface(self):
        root = Path(__file__).parents[1]
        paths = [root / "fleet", *(root / "agent_fleet").glob("*.py")]
        source = "\n".join(path.read_text() for path in paths)
        self.assertEqual(source.count('"kill-session"'), 1)
        for command in ("kill-window", "unlink-window"):
            self.assertNotIn(command, source)

    def test_commander_routes_to_the_lovelace_alan_client(self):
        launcher = (Path(__file__).parents[1] / "fleet-commander").read_text()
        self.assertIn("ssh -tt -o BatchMode=yes lovelace fleet-commander", launcher)
        self.assertIn('exec fleet commander "$@"', launcher)
        self.assertNotIn("session_index.jsonl", launcher)
        self.assertNotIn("codex", launcher)

    def test_muster_and_main_route_to_the_lovelace_hub(self):
        root = Path(__file__).parents[1]
        muster = (root / "fleet-muster").read_text()
        main = (root / "fleet-viewer").read_text()
        service = (root / "fleet.service").read_text()
        self.assertIn('fleet workstation --socket "$local_socket"', muster)
        self.assertIn('-R "$remote_socket:$local_socket"', muster)
        self.assertIn('set -- --workstation "$workstation"', muster)
        self.assertIn('export SSH_AUTH_SOCK="/run/user/$(id -u)/gnupg/S.gpg-agent.ssh"',
                      muster)
        self.assertIn("new-session -d -s fleet@main", main)
        self.assertIn("set-option -t fleet@main prefix None", main)
        self.assertIn("set-option -t fleet@main mouse on", main)
        self.assertIn("set-option -t fleet@muster mouse off", muster)
        self.assertIn("fleet viewer-status main", main)
        self.assertIn("ConditionHost=lovelace", service)

    def test_muster_always_opens_the_global_main_viewer(self):
        source = (Path(__file__).parents[1] / "agent_fleet/ui.py").read_text()
        self.assertIn("fleet show --slot main {1}", source)
        self.assertIn("load:transform(fleet cursor)+unbind(load)", source)
        self.assertIn('"--no-sort"', source)
        self.assertIn("enable-search+toggle-sort", source)
        self.assertNotIn('"--nth=2.."', source)
        self.assertIn("change-prompt(Search: )", source)
        self.assertIn("c:execute-silent(fleet create-tab)", source)
        self.assertIn("r:execute-silent(fleet rename-tab {1})", source)
        self.assertIn('"--footer-border=bottom"', source)

    def test_footer_contains_only_action_hints(self):
        from agent_fleet.ui import footer
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((100, 24))):
            self.assertEqual(
                footer(),
                "Enter open  c create  r rename  R refresh  x archive")

    def test_claude_and_codex_use_distinct_provider_colours(self):
        self.assertNotEqual(AGENT_COLOUR["claude"], AGENT_COLOUR["codex"])

    def test_create_opens_inside_the_muster(self):
        with mock.patch("subprocess.run") as run:
            actions.create_tab()
        run.assert_called_once_with(
            ["tmux", "new-window", "-t", "fleet@muster", "-n", "create",
             "exec fleet create"], check=True)

    def test_rename_opens_inside_the_muster(self):
        key = "lovelace:/tmp/tmux:1:2:$3"
        with mock.patch("subprocess.run") as run:
            actions.rename_tab(key)
        run.assert_called_once_with(
            ["tmux", "new-window", "-t", "fleet@muster", "-n", "rename",
             "exec fleet rename 'lovelace:/tmp/tmux:1:2:$3'"], check=True)

    def test_named_viewers_remain_local(self):
        launcher = (Path(__file__).parents[1] / "fleet-viewer").read_text()
        self.assertTrue(launcher.rstrip().endswith('exec fleet viewer --slot "$slot"'))

    def test_ssh_environment_uses_stable_agent_socket(self):
        environment = ssh_environment()
        self.assertEqual(environment["SSH_AUTH_SOCK"],
                         f"/run/user/{os.getuid()}/gnupg/S.gpg-agent.ssh")

    def test_viewer_uses_stable_agent_environment(self):
        source = (Path(__file__).parents[1] / "agent_fleet/viewer.py").read_text()
        self.assertIn("ssh_environment().items()", source)

    def test_management_prompts_never_read_raw_terminal_input(self):
        source = (Path(__file__).parents[1] / "agent_fleet/actions.py").read_text()
        workstation_source = (
            Path(__file__).parents[1] / "agent_fleet/workstation.py").read_text()
        self.assertNotRegex(source, r"(?<![A-Za-z_])input\(")
        self.assertIn('"rofi", "-dmenu"', workstation_source)

    def test_refresh_routes_to_owner_and_reopens_every_matching_local_viewer(self):
        session = self.session("newton")
        with mock.patch("agent_fleet.actions.find", return_value=session), \
             mock.patch("agent_fleet.actions.viewer.slots",
                        return_value=[("main", session.ref.key),
                                      ("side", session.ref.key), ("other", "elsewhere")]), \
             mock.patch("agent_fleet.actions.host_command") as command, \
             mock.patch("agent_fleet.actions.viewer.request") as reopen:
            actions.refresh(session.ref.key)
        command.assert_called_once_with(
            "newton", "fleet", "refresh-local", session.ref.key,
            capture_output=True)
        self.assertEqual(reopen.call_args_list, [
            mock.call("main", session.ref.key), mock.call("side", session.ref.key)])

    def test_refresh_waits_for_global_projection_before_reopening(self):
        actor = alan_inventory("lovelace", [{
            "addr": "claude-1", "type": "claude", "state": "waiting",
            "native": {"id": "session-1"},
            "attachment": {"kind": "tmux", "session": "fleet@actor-claude-1"},
        }])[0]
        with mock.patch("agent_fleet.actions.find",
                        side_effect=[actor, SystemExit("gone"), actor]), \
             mock.patch("agent_fleet.actions.time.sleep"), \
             mock.patch("agent_fleet.actions.viewer.slots",
                        return_value=[("main", actor.ref.key)]), \
             mock.patch("agent_fleet.actions.host_command"), \
             mock.patch("agent_fleet.actions.viewer.request") as reopen:
            actions.refresh(actor.ref.key)
        reopen.assert_called_once_with("main", actor.ref.key)

    def test_refresh_local_dispatches_alan_by_exact_tagged_key(self):
        host = os.uname().nodename
        with mock.patch("agent_fleet.actions.alan_refresh") as refresh:
            actions.refresh_local(f"alan:{host}:claude-1")
        refresh.assert_called_once_with("claude-1")

    def test_refresh_local_refuses_legacy_tmux_sessions(self):
        with self.assertRaisesRegex(SystemExit, "Alan-owned"):
            actions.refresh_local("lovelace:/tmp/tmux:12:10:$1")

    def test_refresh_check_rejects_a_failed_actor_with_the_same_native_identity(self):
        host = os.uname().nodename
        actor = {"addr": "codex-1", "type": "codex", "state": "failed",
                 "native": {"id": "thread-1"}, "attachment": {"kind": "none"}}
        with mock.patch("agent_fleet.alan.actors", return_value=[actor]):
            with self.assertRaisesRegex(SystemExit, "no usable current native identity"):
                actions.refresh_check(f"alan:{host}:codex-1", "thread-1")

    def test_alan_preview_captures_the_persistent_tmux_pane(self):
        host = os.uname().nodename
        attachment = {"kind": "tmux", "session": "fleet@codex-review-1",
                      "generation": "12:34:$1"}
        session = mock.Mock(session_name=attachment["session"])
        server = mock.Mock(sessions=[session])
        server.cmd.return_value.stdout = [attachment["generation"]]
        with mock.patch.dict(tmux._alan_attachments, {"codex-1": attachment}, clear=True), \
             mock.patch("agent_fleet.tmux.server", return_value=server), \
             mock.patch("agent_fleet.tmux.capture_pane",
                        return_value="conversation\n") as render:
            result = tmux.capture(f"alan:{host}:codex-1", 80, 20)
        self.assertEqual(result, "conversation\n")
        render.assert_called_once_with(session, 80, 20)

    def test_alan_preview_refuses_a_disappeared_actor(self):
        host = os.uname().nodename
        with mock.patch.dict(tmux._alan_attachments, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "disappeared"):
                tmux.capture(f"alan:{host}:codex-1", 80, 20)

    def test_alan_preview_rejects_stale_tmux_generation(self):
        host = os.uname().nodename
        attachment = {"kind": "tmux", "session": "fleet@codex-review-1",
                      "generation": "new"}
        server = mock.Mock()
        server.cmd.return_value.stdout = ["old"]
        with mock.patch.dict(tmux._alan_attachments, {"codex-1": attachment}, clear=True), \
             mock.patch("agent_fleet.tmux.server", return_value=server):
            with self.assertRaisesRegex(RuntimeError, "stale Alan presentation"):
                tmux.capture(f"alan:{host}:codex-1", 80, 20)

    def test_failed_refresh_reopens_a_still_usable_source_then_reports_failure(self):
        session = self.session("newton")
        failure = subprocess.CalledProcessError(1, ["ssh"])
        with mock.patch("agent_fleet.actions.find", return_value=session), \
             mock.patch("agent_fleet.actions.viewer.slots",
                        return_value=[("main", session.ref.key)]), \
             mock.patch("agent_fleet.actions.host_command",
                        side_effect=[failure, mock.Mock()]), \
             mock.patch("agent_fleet.actions.viewer.request") as reopen:
            with self.assertRaises(subprocess.CalledProcessError):
                actions.refresh(session.ref.key)
        reopen.assert_called_once_with("main", session.ref.key)

    def test_failed_refresh_does_not_reopen_from_stale_global_projection(self):
        session = self.session("newton")
        failure = subprocess.CalledProcessError(1, ["ssh"])
        unavailable = subprocess.CalledProcessError(1, ["ssh"])
        with mock.patch("agent_fleet.actions.find", return_value=session), \
             mock.patch("agent_fleet.actions.viewer.slots",
                        return_value=[("main", session.ref.key)]), \
             mock.patch("agent_fleet.actions.host_command",
                        side_effect=[failure, unavailable]) as command, \
             mock.patch("agent_fleet.actions.viewer.request") as reopen:
            with self.assertRaises(subprocess.CalledProcessError):
                actions.refresh(session.ref.key)
        self.assertEqual(command.call_args_list[1], mock.call(
            "newton", "fleet", "refresh-check", session.ref.key, "",
            capture_output=True))
        reopen.assert_not_called()

    def test_refresh_report_keeps_nonzero_failure_and_displays_reason_in_muster(self):
        failure = subprocess.CalledProcessError(1, ["ssh"], stderr="actor_not_idle\n")
        with mock.patch("agent_fleet.actions.refresh", side_effect=failure), \
             mock.patch("agent_fleet.actions.subprocess.run") as run:
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
        with mock.patch("agent_fleet.actions.snapshot", return_value="snapshot"), \
             mock.patch("agent_fleet.actions.decode_message",
                        return_value=([waiting, working, unsupported, remote], {},
                                      ["boltzmann"])), \
             mock.patch("agent_fleet.actions.refresh") as refresh, \
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
        with mock.patch("agent_fleet.actions.snapshot", return_value="snapshot"), \
             mock.patch("agent_fleet.actions.decode_message",
                        return_value=([second, first], {}, [])), \
             mock.patch("agent_fleet.actions.refresh", side_effect=[failure, None]) as refresh, \
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
             mock.patch("agent_fleet.viewer.RUNTIME", Path(runtime)), \
             mock.patch("agent_fleet.viewer.command",
                        side_effect=[["sleep", ".05"], ["sleep", ".5"]]), \
             mock.patch("agent_fleet.viewer.subprocess.run"), \
             mock.patch("agent_fleet.actions.find", return_value=session), \
             mock.patch("agent_fleet.actions.host_command",
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

    def test_viewer_clear_remains_an_internal_primitive(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as runtime:
            env = {**os.environ, "XDG_RUNTIME_DIR": runtime,
                   "PYTHONPATH": str(root)}
            process = subprocess.Popen([sys.executable, "-m", "agent_fleet.cli",
                                        "viewer", "--slot", "test"], env=env)
            socket = Path(runtime) / "agent-fleet/viewer-test.sock"
            try:
                for _ in range(100):
                    if socket.exists():
                        break
                    time.sleep(.01)
                code = "from agent_fleet.viewer import request; request('test', '')"
                subprocess.run([sys.executable, "-c", code], env=env, check=True)
                code = "from agent_fleet.viewer import slots; print(slots())"
                result = subprocess.run([sys.executable, "-c", code], env=env,
                                        text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.strip(), "[('test', '')]")
            finally:
                process.terminate()
                process.wait()

    def test_quota_only_events_force_an_inventory_emit(self):
        source = (Path(__file__).parents[1] / "agent_fleet/tmux.py").read_text()
        self.assertIn('force = "quota" in events', source)
        self.assertIn("if serial != previous or force:", source)
