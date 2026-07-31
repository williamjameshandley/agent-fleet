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
from dataclasses import replace
from unittest import mock
from pathlib import Path

import loop
import networkx as nx

from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.protocol import decode, decode_graph, encode
from agent_fleet.ui import AGENT_COLOUR, STATE_ORDER, recency
from agent_fleet.tmux import split_key
from agent_fleet.actions import next_waiting_key, session_name
from agent_fleet import actions
from agent_fleet import alan
from agent_fleet.config import machine, ssh_environment
from agent_fleet.alan import inventory as alan_inventory
from agent_fleet.alan import Watcher as AlanWatcher
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

        posted = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(posted), 2)
        for request in posted:
            self.assertIn("transform(fleet cursor)", request)
            self.assertNotIn("reload-sync", request)

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
        encoded = json.loads(encode(sessions))
        self.assertEqual(set(encoded), {"version", "sessions", "usage", "unavailable"})
        self.assertEqual(encoded["sessions"][0]["server"]["kind"], "tmux")
        self.assertEqual(decode(json.dumps(encoded)), sessions)
        with self.assertRaisesRegex(ValueError, "unsupported Fleet protocol version"):
            decode('{"version":2,"sessions":[],"usage":{},"unavailable":[]}')

    def test_protocol_round_trip_preserves_the_alan_graph(self):
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = [
            {"addr": "codex-a@newton", "kind": "codex", "host": "newton"}
        ]
        graph.add_node(
            "schedule:550e8400-e29b-41d4-a716-446655440000@newton#0",
            op="send",
            to="claude-b@lovelace",
        )
        graph.add_edge(
            "codex-a@newton#3",
            "claude-b@lovelace#5",
            key="send",
            relation="send",
        )

        restored = decode_graph(encode([], graph=graph))

        self.assertEqual(restored.graph, graph.graph)
        self.assertEqual(list(restored.nodes(data=True)), list(graph.nodes(data=True)))
        self.assertEqual(
            list(restored.edges(keys=True, data=True)),
            list(graph.edges(keys=True, data=True)),
        )

    def test_fleet_composes_host_observations_without_losing_semantics(self):
        newton = nx.MultiDiGraph()
        newton.graph["actors"] = [
            {"addr": "codex-a@newton", "kind": "codex", "host": "newton"}
        ]
        newton.add_node("codex-a@newton#3", actor="codex-a@newton", op="send")
        newton.add_node("claude-b@lovelace#5")
        newton.add_edge(
            "codex-a@newton#3",
            "claude-b@lovelace#5",
            key="send",
            relation="send",
        )

        lovelace = nx.MultiDiGraph()
        lovelace.graph["actors"] = [
            {"addr": "claude-b@lovelace", "kind": "claude", "host": "lovelace"}
        ]
        lovelace.add_node(
            "claude-b@lovelace#5",
            actor="claude-b@lovelace",
            op="input",
        )
        lovelace.add_node(
            "schedule:550e8400-e29b-41d4-a716-446655440000@lovelace#0",
            op="send",
            to="codex-a@newton",
        )

        fleet = Fleet()
        fleet.graphs = {"newton": newton, "lovelace": lovelace}
        composed = fleet.composed_graph()

        self.assertEqual(
            composed.graph["actors"],
            newton.graph["actors"] + lovelace.graph["actors"],
        )
        self.assertEqual(
            composed.nodes["claude-b@lovelace#5"]["op"],
            "input",
        )
        self.assertEqual(
            composed.edges[
                "codex-a@newton#3", "claude-b@lovelace#5", "send"
            ]["relation"],
            "send",
        )
        self.assertIn(
            "schedule:550e8400-e29b-41d4-a716-446655440000@lovelace#0",
            composed,
        )

    def test_alan_key_uses_its_host_bound_actor_identity_once(self):
        ref = SessionRef(ServerRef("newton", "", 0, 0, "alan"),
                         "codex-a@newton")
        self.assertEqual(ref.key, "alan:codex-a@newton")

    def test_python_actor_attaches_the_standard_jupyter_console(self):
        host = os.uname().nodename
        actor = f"python-a@{host}"
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"LOOP_STORE_DIR": directory}), \
             mock.patch.object(alan, "actors", return_value=[{
                 "addr": actor, "kind": "python", "state": "waiting",
             }]), \
             mock.patch.object(viewer.presentation, "python_console") as present:
            viewer.attach(f"alan:{actor}")
        present.assert_called_once_with(
            actor,
            Path(directory) / "actors" / actor / "native" / "kernel.json",
        )

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

    def test_a_failed_idle_check_does_not_latch_muster_refreshes_off(self):
        fleet = Fleet()
        fleet.refresh_pending = True
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "muster.sock").touch()
            with mock.patch("agent_fleet.daemon.RUNTIME", Path(directory)), \
                    mock.patch.object(Fleet, "wait_for_muster_idle",
                                      side_effect=RuntimeError("no muster client")):
                with self.assertRaises(RuntimeError):
                    asyncio.run(fleet.refresh_muster())

        self.assertFalse(fleet.refresh_pending)

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
            addr = f"codex-one@{host}"
            descriptor = {
                "addr": addr, "kind": "codex", "host": host,
                "state": "live", "cwd": str(root),
            }

            def observed(active):
                nodes = [
                    {"id": f"{addr}#0", "stream": addr, "op": "create",
                     "time": "2026-07-30T12:00:00Z"},
                ]
                if active:
                    nodes.extend([
                        {"id": f"{addr}#1", "stream": addr, "op": "input",
                         "sender": "will", "payload": "work",
                         "time": "2026-07-30T12:00:01Z"},
                        {"id": f"{addr}#2", "stream": addr, "op": "evaluation",
                         "time": "2026-07-30T12:00:02Z"},
                    ])
                return {
                    "directed": True, "multigraph": True,
                    "graph": {"actors": [descriptor]},
                    "nodes": nodes, "edges": [],
                }

            config = root / "config"
            state = root / "state"
            label_path = state / "agent-fleet" / "labels" / addr
            label_path.parent.mkdir(parents=True)
            label_path.write_text("needle row\n")
            (config / "agent-fleet").mkdir(parents=True)
            (config / "agent-fleet" / "hosts").write_text(host + "\n")

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
                    server.settimeout(.1)
                    while not stopped.is_set():
                        try:
                            connection, _ = server.accept()
                        except TimeoutError:
                            continue
                        with connection:
                            connection.makefile("rb").readline()
                            connection.sendall((json.dumps({
                                "ok": True,
                                "graph": observed(emit_update.is_set()),
                            }) + "\n").encode())

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
                "XDG_CONFIG_HOME": str(config),
                "XDG_STATE_HOME": str(state),
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
                            and fleet.sessions[host][0].state == "working"
                            and fleet.refresh_pending
                        )
                        await asyncio.sleep(.2)
                        before = fzf_state()
                        self.assertEqual(before["current"]["text"], initial_text)

                        after = await wait_for(
                            lambda: (
                                state if
                                "*" in
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

    def test_alan_create_and_lifecycle_use_the_four_operation_surface(self):
        with tempfile.TemporaryDirectory() as config, \
             mock.patch.dict(os.environ, {"XDG_STATE_HOME": config}), \
             mock.patch("agent_fleet.alan.loop.spawn",
                        return_value="claude-1@newton") as spawn:
            self.assertEqual(alan.create("claude", "analysis", "/work"),
                             "claude-1@newton")
            self.assertEqual(alan.label("claude-1@newton"), "analysis")
        spawn.assert_called_once_with({"kind": "claude", "cwd": "/work"})

        with mock.patch("agent_fleet.alan.loop.control") as control:
            alan.retire("claude-1@newton")
            self.assertEqual(alan_resume("claude-1@newton"), "claude-1@newton")
        self.assertEqual(control.call_args_list, [
            mock.call("claude-1@newton", "retire"),
            mock.call("claude-1@newton", "resume"),
        ])

    def test_inventory_projects_visible_actor_kinds_without_loop_presentation(self):
        descriptors = [
            {"addr": "codex-1@newton", "kind": "codex", "state": "working",
             "cwd": "/work", "created": 1, "human_activity": 2,
             "active_evaluation": "codex-1@newton#2", "evaluation_started": 3,
             "native": {"id": "thread-1"}},
            {"addr": "python-1@newton", "kind": "python", "state": "waiting",
             "cwd": "/work", "created": 1, "human_activity": 0,
             "active_evaluation": None, "evaluation_started": 0},
            {"addr": "claude-old@newton", "kind": "claude", "state": "retired",
             "cwd": "/work", "created": 1, "human_activity": 0,
             "active_evaluation": None, "evaluation_started": 0},
        ]
        projected = alan_inventory("newton", descriptors)
        self.assertEqual([item.ref.session_id for item in projected],
                         ["codex-1@newton", "python-1@newton"])
        self.assertEqual(projected[0].state, "working")
        self.assertEqual(projected[0].transcript_id, "thread-1")
        self.assertEqual(projected[0].evaluation, "codex-1@newton#2")
        self.assertEqual(projected[0].evaluation_started, 3)

    def test_fleet_uses_loop_client_instead_of_reimplementing_its_wire(self):
        source = (Path(__file__).parents[1] / "agent_fleet/alan.py").read_text()
        self.assertNotIn("AF_UNIX", source)
        self.assertNotIn("loop.watch", source)
        self.assertNotIn("loop.list", source)

    def test_show_still_resolves_the_global_source_before_requesting_a_viewer(self):
        session = self.session("newton")
        with mock.patch("agent_fleet.viewer.find", return_value=session) as find, \
             mock.patch("agent_fleet.viewer.request") as request:
            viewer.show(session.ref.key, "main")
        find.assert_called_once_with(session.ref.key)
        request.assert_called_once_with("main", session.ref.key)

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
            run.return_value.stdout = f"codex-deadbeef@{host}\n"
            actions.create()
        self.assertEqual(
            prompt.call_args_list[1],
            mock.call("agent", ("codex", "claude"), context=host))
        run.assert_called_once_with(
            host, "fleet", "actor-create", "codex", "analysis", "/work",
            stdout=subprocess.PIPE)
        wait.assert_called_once_with(f"alan:codex-deadbeef@{host}")
        show.assert_called_once_with(f"alan:codex-deadbeef@{host}")

    def test_remote_creator_is_batch_mode_and_leaves_failure_output_visible(self):
        host = "newton" if os.uname().nodename != "newton" else "lovelace"
        with mock.patch("agent_fleet.actions.subprocess.run") as run:
            actions.host_command(host, "fleet", "actor-create", "codex",
                                 "work tree;safe", "/work dir/$literal",
                                 stdout=subprocess.PIPE)
        run.assert_called_once_with(
            ["ssh", "-T", "-o", "BatchMode=yes", host,
             "fleet actor-create codex 'work tree;safe' '/work dir/$literal'"],
            text=True, check=True, capture_output=False, stdout=subprocess.PIPE)

    def test_create_uses_claudes_existing_provider_presentation(self):
        host = "newton" if os.uname().nodename != "newton" else "lovelace"
        with mock.patch("agent_fleet.actions.muster_input",
                        side_effect=[host, "claude", "analysis", "/work"]) as prompt, \
             mock.patch("agent_fleet.actions.host_command") as run, \
             mock.patch("agent_fleet.actions.wait_for_projection") as wait, \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            run.return_value.stdout = f"claude-deadbeef@{host}\n"
            actions.create()
        self.assertEqual(
            prompt.call_args_list[1],
            mock.call("agent", ("codex", "claude"), context=host))
        run.assert_called_once_with(
            host, "fleet", "actor-create", "claude", "analysis", "/work",
            stdout=subprocess.PIPE)
        wait.assert_called_once_with(f"alan:claude-deadbeef@{host}")
        show.assert_called_once_with(f"alan:claude-deadbeef@{host}")
        self.assertNotIn('"tmux", "new-session"',
                         (Path(__file__).parents[1] / "agent_fleet/actions.py").read_text())

    def test_fleet_package_requires_the_canonical_alan_client(self):
        package = (Path(__file__).parents[1] / "PKGBUILD").read_text()
        self.assertIn(
            "depends=('alan>=1:3.0.0.a1' ", package)

    def test_projection_readiness_waits_for_actor_native_evidence(self):
        actor = alan_inventory("lovelace", [{
            "addr": "codex-1@lovelace", "kind": "codex", "state": "waiting",
            "created": 1, "human_activity": 0, "cwd": "/work",
            "active_evaluation": None, "evaluation_started": 0,
            "native": {"id": "thread-1"},
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
            SessionRef(ServerRef(host, "", 0, 0, "alan"), f"codex-1@{host}"),
            "work", 1, 0, 0, 1, "tmux", "", "/work",
            "codex", "waiting", "", 0, "thread-1", 1)
        with mock.patch("agent_fleet.actions.find", return_value=session), \
             mock.patch("agent_fleet.actions.host_command") as command, \
             mock.patch("agent_fleet.actions.wait_for_absence") as absent, \
             mock.patch("agent_fleet.actions.viewer.slots", return_value=[("main", session.ref.key)]), \
             mock.patch("agent_fleet.actions.viewer.request") as request:
            actions.archive(session.ref.key)
        command.assert_called_once_with(host, "fleet", "alan-retire", f"codex-1@{host}",
                                        capture_output=True)
        absent.assert_called_once_with(session.ref.key)
        request.assert_called_once_with("main", "")

    def test_archive_refuses_actor_without_native_identity_before_mutation(self):
        host = os.uname().nodename
        session = Session(
            SessionRef(ServerRef(host, "", 0, 0, "alan"), f"codex-1@{host}"),
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

    def test_history_keeps_retained_alan_actor_and_suppresses_transcript_fallback(self):
        actor = {"addr": "codex-1@lovelace", "kind": "codex", "state": "retired",
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
                        return_value='{"version":1,"sessions":[],"usage":{},"unavailable":[]}'), \
             mock.patch("agent_fleet.actions.host_command", side_effect=command), \
             contextlib.redirect_stdout(output):
            actions.history()
        self.assertEqual(output.getvalue().splitlines(), [
            "alan:codex-1@lovelace\tlovelace\tcodex\twork\t/work"])

    def test_retained_unavailable_actor_remains_the_native_history_authority(self):
        actor = {"addr": "codex-1@lovelace", "kind": "codex", "state": "unavailable",
                 "label": "work", "cwd": "/work", "created": 10,
                 "human_activity": 20, "native": {"id": "thread-1"}}

        def command(_host, _fleet, operation, *args, **_kwargs):
            payload = ([actor] if operation == "alan-actors" else [{
                "agent": "codex", "session_id": "thread-1", "mtime": 20,
                "name": "work", "cwd": "/work"}])
            return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload))

        output = io.StringIO()
        with mock.patch("agent_fleet.actions.hosts", return_value=["lovelace"]), \
             mock.patch("agent_fleet.actions.snapshot",
                        return_value='{"version":1,"sessions":[],"usage":{},"unavailable":[]}'), \
             mock.patch("agent_fleet.actions.host_command", side_effect=command), \
             contextlib.redirect_stdout(output):
            actions.history()
        self.assertEqual(output.getvalue().splitlines(), [
            "alan:codex-1@lovelace\tlovelace\tcodex\twork\t/work"])

    def test_history_open_retries_the_same_alan_address(self):
        key = "alan:codex-1@lovelace"
        with mock.patch("agent_fleet.actions.host_command") as command, \
             mock.patch("agent_fleet.actions.wait_for_projection") as wait, \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            actions.open_history(key)
        command.assert_called_once_with("lovelace", "fleet", "alan-resume",
                                        "codex-1@lovelace",
                                        capture_output=True)
        wait.assert_called_once_with(key)
        show.assert_called_once_with(key)

    def test_transcript_history_open_captures_remote_resume_failure(self):
        key = "lovelace:codex:full-thread-id"
        with mock.patch("agent_fleet.actions.snapshot",
                        return_value='{"version":1,"sessions":[],"usage":{},"unavailable":[]}'), \
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
                actions.archive_report("alan:codex-1@lovelace")
        run.assert_called_once_with([
            "tmux", "display-message", "-t", "fleet@muster",
            "Archive failed: retire refused"])

    def test_history_open_failure_is_visible_in_muster(self):
        with mock.patch("agent_fleet.actions.open_history",
                        side_effect=RuntimeError("native identity changed")), \
             mock.patch("agent_fleet.actions.subprocess.run") as run:
            with self.assertRaisesRegex(SystemExit, "native identity changed"):
                actions.open_history_report("alan:codex-1@lovelace")
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
        self.assertIn("l:execute-silent(fleet toggle language)", source)
        self.assertIn("p:execute-silent(fleet toggle python)", source)
        self.assertIn('"--footer-border=bottom"', source)

    def test_muster_controls_independently_project_language_and_python(self):
        root = "codex-root@newton"
        language = "claude-child@newton"
        python = "python-child@newton"
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = [
            {"addr": root, "kind": "codex"},
            {"addr": language, "kind": "claude"},
            {"addr": python, "kind": "python"},
        ]
        graph.add_node(f"{root}#0", stream=root, op="create")
        for position, child in enumerate((language, python), 1):
            source = f"{root}#{position}"
            graph.add_node(source, stream=root, op="spawn")
            graph.add_node(f"{child}#0", stream=child, op="create", spawn=source)
            graph.add_edge(source, f"{child}#0", key="spawn")
        sessions = [
            Session(SessionRef(ServerRef("newton", "", 0, 0, "alan"), actor),
                    actor, 1, 0, 0, 1, "alan", "", "/work", kind, "waiting")
            for actor, kind in ((root, "codex"), (language, "claude"), (python, "python"))
        ]
        raw = encode(sessions, graph=graph)

        def projected(language_visible, python_visible):
            values = {
                "@fleet_show_language": language_visible,
                "@fleet_show_python": python_visible,
            }
            with mock.patch.object(ui, "snapshot", return_value=raw), \
                 mock.patch.object(ui, "option", side_effect=values.__getitem__):
                return [item.ref.session_id for item in ui.ordered()[0]]

        self.assertEqual(projected(False, False), [root])
        self.assertEqual(projected(True, False), [root, language])
        self.assertEqual(projected(False, True), [root, python])

    def test_toggle_changes_the_named_muster_tmux_option(self):
        with mock.patch.object(ui, "option", return_value=False), \
             mock.patch.object(ui.subprocess, "run") as run:
            ui.toggle("language")
        run.assert_called_once_with(
            ["tmux", "set-option", "-t", "=fleet@muster", "@fleet_show_language", "1"],
            check=True,
        )

    def test_footer_contains_only_action_hints(self):
        from agent_fleet.ui import footer
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((100, 24))):
            self.assertEqual(
                footer(),
                "Enter open  c create  r rename  x archive  l agents  p python")

    def test_column_header_counts_sessions_by_state(self):
        from agent_fleet.ui import column_header
        states = ["working", "working", "waiting", "needs-action"]
        sessions = [replace(self.session("lovelace", f"${i}"), reported_state=state)
                    for i, state in enumerate(states)]
        self.assertIn("2 working  1 waiting  4 total", column_header(sessions))

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

    def test_alan_attachment_execs_the_fleet_owned_repl(self):
        actor = f"codex-1@{os.uname().nodename}"
        with mock.patch.object(alan, "actors", return_value=[{
                 "addr": actor, "kind": "codex", "state": "waiting",
             }]), \
             mock.patch("agent_fleet.viewer.os.execvp") as execute:
            viewer.attach(f"alan:{actor}")
        execute.assert_called_once_with(
            "fleet", ["fleet", "actor-view", actor])

    def test_alan_preview_is_derived_from_native_evidence(self):
        actor = {
            "addr": "codex-1@newton", "kind": "codex",
            "cwd": "/work",
            "native": {"id": "thread-1", "thread_id": "thread-1",
                       "base_dir": "/alan/native",
                       "path": "/provider/corpus/rollout-thread-1.jsonl"},
        }
        evidence = object()
        with mock.patch("agent_fleet.tmux.alan.actors", return_value=[actor]), \
             mock.patch("agent_fleet.tmux.native_evidence",
                        return_value=evidence) as resolve, \
             mock.patch("agent_fleet.tmux.render_preview",
                        return_value="conversation\n") as preview:
            self.assertEqual(
                tmux.capture("alan:codex-1@newton", 80, 20),
                "conversation\n")
        resolve.assert_called_once_with(actor["native"])
        preview.assert_called_once_with(evidence, 80, 20)

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
