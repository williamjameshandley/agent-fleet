import unittest
import asyncio
import os
import pty
import shlex
import subprocess
import sys
import tempfile
import time
import json
import queue
import signal
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
from agent_fleet.render import AGENT_COLOUR, STATE_ORDER, recency
from agent_fleet.tmux import split_key
from agent_fleet import actions, authority
from agent_fleet import alan
from agent_fleet.config import machine, ssh_environment
from agent_fleet.alan import inventory as alan_inventory
from agent_fleet.alan import Watcher as AlanWatcher
from agent_fleet.alan import resume as alan_resume
from agent_fleet import hot, render, viewer
from agent_fleet import workstation
from agent_fleet import tmux
from agent_fleet import ui
from agent_fleet import reboot, ui_process
from agent_fleet import ui_process as cli
from agent_fleet.daemon import Fleet


def without_tmux_client():
    """$TMUX overrides TMUX_TMPDIR, so a fixture server needs it removed."""
    environment = {name: value for name, value in os.environ.items()
                   if name not in {"TMUX", "TMUX_PANE"}}
    environment["FLEET_TMUX"] = str(Path(__file__).parents[1] / "fleet-tmux")
    return environment


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
                **without_tmux_client(),
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
        with mock.patch.object(hot, "active_main", return_value="actor:focused"), \
                mock.patch.object(hot, "fetch",
                                  return_value="actor:focused\n") as fetch, \
                mock.patch("agent_fleet.hot.subprocess.run", return_value=state):
            self.assertEqual(hot.cursor(), "pos(2)")
        fetch.assert_called_once_with("cursor actor:focused")

    def test_cursor_falls_back_to_the_daemons_first_waiting_row(self):
        state = self.muster_state([{"text": "actor:first\tfirst"},
                                   {"text": "actor:second\tsecond"}])
        with mock.patch.object(hot, "active_main", return_value=""), \
                mock.patch.object(hot, "fetch",
                                  return_value="actor:first\n") as fetch, \
                mock.patch("agent_fleet.hot.subprocess.run", return_value=state):
            self.assertEqual(hot.cursor(), "pos(1)")
        fetch.assert_called_once_with("cursor")

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
            self.assertIn("transform(/usr/lib/agent-fleet/ui cursor)", request)
            self.assertNotIn("reload-sync", request)

    def test_cursor_refuses_a_truncated_match_list(self):
        state = self.muster_state([{"text": "actor:first\tfirst"}], count=2)
        with mock.patch.object(hot, "active_main", return_value="actor:first"), \
                mock.patch.object(hot, "fetch", return_value="actor:first\n"), \
                mock.patch("agent_fleet.hot.subprocess.run", return_value=state):
            with self.assertRaises(SystemExit):
                hot.cursor()

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
        fleet.observations = {"newton": encode([], {}, [], newton),
                              "lovelace": encode([], {}, [], lovelace)}
        fleet.observed = 1
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

    def test_composed_graph_recomposes_only_per_observation_generation(self):
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = [{"addr": "a@h", "kind": "codex"}]
        fleet = Fleet()
        fleet.observations = {"lovelace": encode([], {}, [], graph)}
        fleet.observed = 1
        first = fleet.composed_graph()
        self.assertIs(fleet.composed_graph(), first)
        fleet.observations["lovelace"] = encode([], {}, [], graph)
        self.assertIs(fleet.composed_graph(), first)
        fleet.observed = 2
        self.assertIsNot(fleet.composed_graph(), first)

    def test_reply_drops_the_render_when_the_client_disconnected(self):
        fleet = Fleet()

        class Reader:
            async def readline(self):
                return b"header\n"

        class Writer:
            closed = False

            def write(self, payload):
                pass

            async def drain(self):
                raise ConnectionResetError

            def close(self):
                self.closed = True

        async def exercise():
            writer = Writer()
            with mock.patch.object(fleet, "projected",
                                   mock.AsyncMock(return_value=[])):
                await fleet.reply(Reader(), writer)
            self.assertTrue(writer.closed)

        asyncio.run(exercise())

    def test_daemon_resolves_one_exact_live_descriptor_without_snapshot(self):
        fleet = Fleet()
        session = self.session("lovelace")
        fleet.sessions = {"lovelace": [session]}
        fleet.unavailable = set()
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()

        async def exercise():
            reader = asyncio.StreamReader()
            reader.feed_data(f"resolve {session.ref.key}\n".encode())
            reader.feed_eof()
            await fleet.reply(reader, writer)

        asyncio.run(exercise())
        self.assertEqual(json.loads(writer.write.call_args.args[0]),
                         {"agent": session.agent, "state": session.state,
                          "cwd": session.cwd})

    def test_alan_key_uses_its_host_bound_actor_identity_once(self):
        ref = SessionRef(ServerRef("newton", "", 0, 0, "alan"),
                         "codex-a@newton")
        self.assertEqual(ref.key, "alan:codex-a@newton")

    def test_viewer_open_has_no_actor_creation_or_resume_path(self):
        source = (Path(__file__).parents[1] / "agent_fleet/viewer.py").read_text()
        self.assertNotIn("alan.resume", source)
        self.assertNotIn("alan.spawn", source)

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
        for arguments in [[], ["key", "bad"], ["key", "1", "2", "extra"]]:
            result = subprocess.run(
                [sys.executable, "-m", "agent_fleet.ui_process", "preview", *arguments],
                text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("usage: /usr/lib/agent-fleet/ui", result.stderr)

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
            ssh = bin_dir / "ssh"
            ssh.write_text(
                "#!/bin/sh\n"
                f"exec {sys.executable} -c "
                "'from agent_fleet.daemon import projection; print(projection(), end=\"\")'\n"
            )
            ssh.chmod(0o755)
            host = os.uname().nodename
            fleet = Fleet()
            stopped = threading.Event()
            emit_update = threading.Event()
            addr = f"codex-one@{host}"
            descriptor = {
                "addr": addr, "kind": "codex", "host": host,
                "state": "waiting", "cwd": str(root),
            }

            principal = f"will@{host}"

            def observed(active):
                nodes = [
                    {"id": f"{principal}#0", "stream": principal,
                     "op": "create", "time": "2026-07-30T11:59:58Z"},
                    {"id": f"{principal}#1", "stream": principal,
                     "op": "spawn", "time": "2026-07-30T11:59:59Z"},
                    {"id": f"{addr}#0", "stream": addr, "op": "create",
                     "time": "2026-07-30T12:00:00Z", "evidence": "x" * 65536},
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
                    "graph": {"actors": [
                        {**descriptor,
                         "state": "working" if active else "waiting"},
                        {"addr": principal, "kind": "principal", "host": host},
                    ]},
                    "nodes": nodes,
                    "edges": [{"source": f"{principal}#1",
                               "target": f"{addr}#0", "key": "spawn"}],
                }

            config = root / "config"
            state = root / "state"
            label_path = state / "agent-fleet" / "labels" / addr
            label_path.parent.mkdir(parents=True)
            label_path.write_text("needle row\n")
            (config / "agent-fleet").mkdir(parents=True)
            (config / "agent-fleet" / "hosts").write_text(host + "\n")

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

            watch_server = threading.Thread(target=serve_watch, daemon=True)
            watch_server.start()
            for _ in range(100):
                if alan_socket.exists():
                    break
                time.sleep(.01)
            self.assertTrue(alan_socket.exists())

            command = (
                f"printf 'alpha\\nneedle row\\nomega\\n' | "
                f"exec fzf --listen {muster_socket}"
            )
            environment = {
                **without_tmux_client(),
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
                    reply_server = await asyncio.start_unix_server(
                        fleet.reply, str(fleet_socket))
                    collector = asyncio.create_task(fleet.collect(host))
                    try:
                        await wait_for(
                            lambda: fleet.sessions.get(host)
                            and fleet.sessions[host][0].name == "needle row"
                        )
                        await wait_for(
                            lambda: "alan:" in
                            (fzf_state().get("current") or {}).get("text", "")
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
                        reply_server.close()
                        collector.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await collector

                with mock.patch.dict(os.environ, environment, clear=True), \
                     mock.patch("agent_fleet.daemon.RUNTIME", runtime):
                    asyncio.run(exercise())
            finally:
                stopped.set()
                emit_update.set()
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

    def test_migrated_codex_uses_its_persisted_native_identity(self):
        actor = "codex-actor-id@newton"
        environment = {**os.environ, "XDG_STATE_HOME": ""}
        environment.pop("LOOP_STORE_DIR", None)
        with tempfile.TemporaryDirectory() as state, \
             mock.patch.dict(os.environ, {**environment,
                                          "XDG_STATE_HOME": state}, clear=True):
            native = alan.native_dir(actor)
            native.mkdir(parents=True)
            (native / "thread_id").write_text("native-id\n")
            self.assertEqual(alan.provider_identity(actor, "codex"), "native-id")

    def test_inventory_projects_visible_actor_kinds_without_loop_presentation(self):
        identity = "00000000-0000-4000-8000-000000000001"
        codex = f"codex-{identity}@newton"
        descriptors = [
            {"addr": codex, "kind": "codex", "state": "working",
             "cwd": "/work", "created": 1, "human_activity": 2,
             "native_id": "persisted-native-id",
             "active_evaluation": f"{codex}#2", "evaluation_started": 3,
             "native": {"path": f"/native/rollout-{identity}.jsonl"}},
            {"addr": "python-1@newton", "kind": "python", "state": "waiting",
             "cwd": "/work", "created": 1, "human_activity": 0,
             "active_evaluation": None, "evaluation_started": 0,
             "native": {"kind": "ipython", "path": "/native/history.sqlite"}},
            {"addr": "claude-old@newton", "kind": "claude", "state": "retired",
             "cwd": "/work", "created": 1, "human_activity": 0,
             "active_evaluation": None, "evaluation_started": 0},
        ]
        projected = alan_inventory("newton", descriptors)
        self.assertEqual([item.ref.session_id for item in projected],
                         [codex, "python-1@newton"])
        self.assertEqual(projected[0].state, "working")
        self.assertEqual(projected[0].transcript_id, "persisted-native-id")
        self.assertEqual(projected[0].transcript_path,
                         f"/native/rollout-{identity}.jsonl")
        self.assertEqual(projected[0].evaluation, f"{codex}#2")
        self.assertEqual(projected[0].evaluation_started, 3)
        self.assertEqual(projected[1].transcript_id, "")
        self.assertEqual(projected[1].transcript_path, "")

    def test_fleet_uses_loop_client_instead_of_reimplementing_its_wire(self):
        source = (Path(__file__).parents[1] / "agent_fleet/alan.py").read_text()
        self.assertNotIn("AF_UNIX", source)
        self.assertNotIn("loop.watch", source)
        self.assertNotIn("loop.list", source)

    def test_open_main_sends_the_canonical_source_to_the_persistent_viewer(self):
        session = self.session("newton")
        with mock.patch("agent_fleet.viewer.request") as request, \
             mock.patch("agent_fleet.ui.select") as select:
            viewer.open_main(session.ref.key)
        request.assert_called_once_with("main", session.ref.key)
        select.assert_called_once_with()

    def test_public_show_api_places_an_explicit_named_slot(self):
        with mock.patch.object(viewer, "slots", return_value=[("main", "old")]), \
             mock.patch.object(viewer, "request") as request:
            viewer.show("source", slot="left")
        request.assert_called_once_with("left", "source")

    def test_repeated_local_open_uses_only_atomic_switch(self):
        session = self.session(os.uname().nodename)
        state = viewer.Attachment("main", "/dev/pts/9")
        state.source = "old"
        state.host = os.uname().nodename.split(".", 1)[0]
        state.child = mock.Mock(poll=mock.Mock(return_value=None))
        with mock.patch.object(state, "switch_local") as switch, \
             mock.patch.object(viewer.subprocess, "Popen") as popen:
            state.open(session.ref.key)
        switch.assert_called_once_with(session.ref.key)
        popen.assert_not_called()

    def test_initial_local_attachment_does_not_request_nested_tmux(self):
        state = viewer.Attachment("main", "/dev/pts/9")
        child = mock.Mock()
        with mock.patch.object(state, "resolve", return_value=("/tmp/tmux", 12, 10, "$1")), \
             mock.patch.object(state, "wait_local"), \
             mock.patch.object(viewer.subprocess, "Popen", return_value=child) as popen, \
             mock.patch.object(viewer, "ssh_environment",
                               return_value={"TMUX": "nested", "TMUX_PANE": "%1",
                                             "PATH": "/usr/bin"}):
            state.start_local("source")
        self.assertEqual(popen.call_args.kwargs["env"], {"PATH": "/usr/bin"})

    def test_atomic_switch_revalidates_server_generation_session_and_client(self):
        command = mock.Mock(stdout=[])
        server = mock.Mock()
        server.cmd.return_value = command
        with mock.patch.object(tmux, "server", return_value=server):
            tmux.switch_session("/tmp/tmux/default", 12, 10, "$1", "/dev/pts/9")
        arguments = server.cmd.call_args.args
        self.assertEqual(arguments[:4], ("if-shell", "-t", "$1", "-F"))
        for identity in ("socket_path", "pid", "start_time", "session_id"):
            self.assertIn(identity, arguments[4])
        self.assertIn("switch-client", arguments[5])

    def test_installed_switch_boundary_moves_exact_client_and_rejects_stale_generation(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            environment = {**without_tmux_client(), "TMUX_TMPDIR": directory}
            environment["TERM"] = "xterm-256color"
            subprocess.run(["tmux", "new-session", "-d", "-s", "one"],
                           check=True, env=environment)
            subprocess.run(["tmux", "new-session", "-d", "-s", "two"],
                           check=True, env=environment)
            client = subprocess.Popen(["script", "-qec", "tmux attach-session -t one",
                                       "/dev/null"],
                                      stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,
                                      env=environment, start_new_session=True)
            try:
                for _ in range(100):
                    result = subprocess.run(
                        ["tmux", "list-clients", "-F", "#{client_name}"],
                        text=True, capture_output=True, env=environment)
                    if result.stdout.strip():
                        break
                    time.sleep(.01)
                tty = result.stdout.strip()
                attached = subprocess.run(
                    ["tmux", "list-sessions", "-F", "#{session_name} #{session_attached}"],
                    check=True, text=True, capture_output=True, env=environment).stdout.splitlines()
                self.assertIn("one 1", attached)
                identity = subprocess.run(
                    ["tmux", "list-sessions", "-f", "#{==:#{session_name},two}", "-F",
                     "#{socket_path}\t#{pid}\t#{start_time}\t#{session_id}"],
                    check=True, text=True, capture_output=True, env=environment
                ).stdout.strip().split("\t")
                subprocess.run([root / "fleet-switch", *identity, tty],
                               check=True, env=environment)
                attached = subprocess.run(
                    ["tmux", "list-sessions", "-F", "#{session_name} #{session_attached}"],
                    check=True, text=True, capture_output=True, env=environment).stdout.splitlines()
                self.assertIn("two 1", attached)
                self.assertIn("one 0", attached)
                stale = subprocess.run([root / "fleet-switch", identity[0], "999999",
                                        *identity[2:], tty], env=environment,
                                       text=True, capture_output=True)
                self.assertNotEqual(stale.returncode, 0)
                self.assertIn("identity changed", stale.stderr)
            finally:
                os.killpg(client.pid, signal.SIGHUP)
                client.wait()
                subprocess.run(["tmux", "kill-server"], env=environment)

    def test_failed_host_replacement_resumes_unchanged_old_attachment(self):
        state = viewer.Attachment("main", "/dev/pts/9")
        state.source = "old-source"
        state.host = "old-host"
        state.child = mock.Mock(pid=81, poll=mock.Mock(return_value=None))
        with mock.patch.object(state, "start_remote", side_effect=RuntimeError("offline")), \
             mock.patch.object(viewer, "stop_child") as stop, \
             mock.patch.object(viewer.os, "killpg") as kill:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                state.open("new-host:/tmp/tmux/default:12:10:$1")
        stop.assert_called_once_with(state.child, signal.SIGSTOP)
        kill.assert_called_once_with(81, signal.SIGCONT)
        self.assertEqual((state.source, state.host), ("old-source", "old-host"))

    def test_focus_failure_does_not_turn_a_completed_open_into_failure(self):
        state = mock.Mock()
        with mock.patch.object(viewer, "focus", side_effect=OSError("no display")), \
             mock.patch.object(viewer, "viewer_error") as report:
            error = viewer.activate(state, "side", "source", 12.0)
        state.open.assert_called_once_with("source", 12.0)
        self.assertEqual(error, "Focus failed: no display")
        report.assert_not_called()

    def test_dead_attachment_clears_the_advertised_source_without_an_open(self):
        state = viewer.Attachment("main", "/dev/pts/9")
        state.source = "source"
        state.host = os.uname().nodename.split(".", 1)[0]
        state.child = mock.Mock(poll=mock.Mock(return_value=1))
        with mock.patch.object(state, "close") as close:
            error = state.check()
        close.assert_called_once_with()
        self.assertEqual(state.source, "")
        self.assertEqual(error, "Viewer attachment exited unexpectedly")

    def test_dead_master_cleanup_never_starts_ssh(self):
        state = viewer.Attachment("main", "/dev/pts/9")
        state.source = "source"
        state.host = "newton"
        state.master = (82, 10)
        state.remote_file = "/run/user/1000/agent-fleet/viewer-lovelace-main.tty"
        state.child = mock.Mock(poll=mock.Mock(return_value=1))
        child = state.child
        with mock.patch.object(viewer, "process_alive", return_value=False), \
             mock.patch.object(viewer, "stop_child") as stop, \
             mock.patch.object(viewer.subprocess, "run") as run:
            error = state.check()
        stop.assert_called_once_with(child)
        run.assert_not_called()
        self.assertEqual(error, "Viewer SSH master exited unexpectedly")
        self.assertEqual((state.source, state.remote_file), ("", None))

    def test_repeated_remote_open_retains_master_and_interactive_client(self):
        state = viewer.Attachment("main", "/dev/pts/9")
        state.source = "remote:/tmp/tmux/default:12:10:$1"
        state.host = "remote"
        state.master = (82, 10)
        state.remote_tty = "/dev/pts/8"
        state.child = mock.Mock(poll=mock.Mock(return_value=None))
        checked = mock.Mock(returncode=0)
        switched = mock.Mock(stdout="0.001000\n")
        with mock.patch.object(viewer.subprocess, "run", return_value=checked), \
             mock.patch.object(viewer, "process_alive", return_value=True), \
             mock.patch.object(state, "resolve", return_value=("/tmp/tmux/default", 12, 10, "$2")), \
             mock.patch.object(state, "ssh", return_value=switched) as ssh, \
             mock.patch.object(viewer.subprocess, "Popen") as popen:
            state.open("remote:/tmp/tmux/default:12:10:$2")
        popen.assert_not_called()
        ssh.assert_called_once()
        self.assertEqual(state.master, (82, 10))
        self.assertEqual(state.child.poll(), None)

    def test_remote_start_retries_until_the_allocated_client_is_registered(self):
        state = viewer.Attachment("main", "/dev/pts/9")
        child = mock.Mock(poll=mock.Mock(return_value=None))
        empty = mock.Mock(stdout="", returncode=0)
        tty = mock.Mock(stdout="/dev/pts/8\n", returncode=0)
        not_ready = mock.Mock(stdout="", stderr="can't find client: /dev/pts/8\n",
                              returncode=1)
        ready = mock.Mock(stdout="0.001000\n", stderr="", returncode=0)
        with mock.patch.object(viewer.subprocess, "Popen", return_value=child), \
             mock.patch.object(state, "ensure_master", return_value=(82, 10)), \
             mock.patch.object(state, "resolve",
                               return_value=("/tmp/tmux", 12, 10, "$1")), \
             mock.patch.object(state, "ssh",
                               side_effect=[empty, tty, not_ready, tty, ready]) as ssh:
            result = state.start_remote("newton", "source")
        self.assertEqual(result[0:2], (child, (82, 10)))
        self.assertEqual(state.switch_duration, .001)
        self.assertEqual(sum(call.kwargs.get("check") is False
                             for call in ssh.call_args_list), 2)

    def test_non_hub_actor_lookup_reuses_one_forward_to_the_fleet_daemon(self):
        state = viewer.Attachment("side", "/dev/pts/9")
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.recv.side_effect = [b"projection", b"", b"projection", b""]
        with mock.patch.object(viewer.os, "uname",
                               return_value=mock.Mock(nodename="newton")), \
             mock.patch.object(viewer.subprocess, "run") as run, \
             mock.patch.object(state, "ensure_master", return_value=(82, 10)), \
             mock.patch.object(viewer, "process_alive", return_value=True), \
             mock.patch.object(viewer.socket, "socket", return_value=client):
            self.assertEqual(state.daemon("resolve source"), "projection")
            self.assertEqual(state.daemon("resolve source"), "projection")
        self.assertEqual(run.call_count, 2)
        cancel, forward = run.call_args_list
        self.assertIn("cancel", cancel.args[0])
        command = forward.args[0]
        self.assertIn("StreamLocalBindUnlink=yes", command)
        self.assertNotIn("python", " ".join(map(str, command)))
        self.assertEqual(client.connect.call_count, 2)

    def test_replaced_daemon_master_rebuilds_the_exact_forward(self):
        state = viewer.Attachment("side", "/dev/pts/9")
        state.daemon_socket = Path("/run/user/1000/agent-fleet/viewer-side-fleet.sock")
        state.daemon_master = (81, 9)
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.recv.side_effect = [b"projection", b""]
        with mock.patch.object(viewer.os, "uname",
                               return_value=mock.Mock(nodename="newton")), \
             mock.patch.object(viewer, "process_alive", return_value=False), \
             mock.patch.object(Path, "unlink") as unlink, \
             mock.patch.object(state, "ensure_master", return_value=(82, 10)), \
             mock.patch.object(state, "cancel_daemon_forward") as cancel, \
             mock.patch.object(viewer.subprocess, "run") as run, \
             mock.patch.object(viewer.socket, "socket", return_value=client):
            self.assertEqual(state.daemon("resolve source"), "projection")
        unlink.assert_called_once_with(missing_ok=True)
        cancel.assert_called_once_with()
        self.assertIn("forward", run.call_args.args[0])
        self.assertEqual(state.daemon_master, (82, 10))

    def test_master_creation_requires_configured_persistent_multiplexing(self):
        state = viewer.Attachment("side", "/dev/pts/9")
        policy = mock.Mock(stdout="controlmaster no\ncontrolpath none\ncontrolpersist no\n")
        with mock.patch.object(viewer.subprocess, "run", return_value=policy):
            with self.assertRaisesRegex(RuntimeError, "ControlMaster"):
                state.master_policy("newton")

    def test_daemon_forward_recovery_cancels_only_the_exact_slot_rule(self):
        state = viewer.Attachment("left", "/dev/pts/9")
        local, specification = state.daemon_forward()
        with mock.patch.object(viewer.subprocess, "run") as run, \
             mock.patch.object(Path, "unlink") as unlink:
            state.cancel_daemon_forward()
        run.assert_called_once_with(
            ["ssh", "-O", "cancel", "-L", specification, viewer.HUB],
            env=viewer.ssh_environment(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        unlink.assert_called_once_with(missing_ok=True)
        self.assertEqual(local.name, "viewer-left-fleet.sock")

    def test_main_viewer_focus_uses_workstation_reverse_socket(self):
        with mock.patch("agent_fleet.viewer.subprocess.run") as run, \
             mock.patch("agent_fleet.viewer.workstation.request") as request:
            run.return_value.stdout = "boltzmann\n"
            viewer.focus("main")
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
             mock.patch("agent_fleet.actions.fleet_action",
                        return_value={"source": f"alan:codex-deadbeef@{host}"}) as action, \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            actions.create_prompt()
        self.assertEqual(
            prompt.call_args_list[1],
            mock.call("agent", ("codex", "claude"), context=host))
        action.assert_called_once_with({"operation": "create", "host": host,
                                       "agent": "codex", "name": "analysis.",
                                       "cwd": "/work"})
        show.assert_called_once_with(f"alan:codex-deadbeef@{host}")

    def test_create_composes_from_explicit_python_values(self):
        host = os.uname().nodename
        with mock.patch("agent_fleet.actions.fleet_action",
                        return_value={"source": f"alan:codex-deadbeef@{host}"}) as action, \
             mock.patch("agent_fleet.actions.viewer.open_main"), \
             mock.patch("agent_fleet.actions.muster_input") as prompt:
            key = actions.create(host, "codex", "analysis.", "/work")
        self.assertEqual(key, f"alan:codex-deadbeef@{host}")
        prompt.assert_not_called()
        action.assert_called_once_with({"operation": "create", "host": host,
                                       "agent": "codex", "name": "analysis.",
                                       "cwd": "/work"})

    def test_daemon_remote_authority_uses_one_fixed_batch_mode_entrypoint(self):
        host = "newton" if os.uname().nodename != "newton" else "lovelace"
        process = mock.Mock(returncode=0)
        process.communicate = mock.AsyncMock(
            return_value=(b'{"ok":true,"value":{"name":"safe"}}\n', b''))
        with mock.patch("agent_fleet.daemon.asyncio.create_subprocess_exec",
                        return_value=process) as execute:
            value = asyncio.run(Fleet().authority(
                host, {"operation": "rename-alan", "actor": "codex-1@newton",
                       "name": "work tree;safe"}))
        self.assertEqual(value, {"name": "safe"})
        command = execute.call_args.args
        self.assertEqual(command[:5],
                         ("ssh", "-T", "-o", "BatchMode=yes", host))
        self.assertIn("/usr/lib/agent-fleet/action", command[5])
        self.assertNotIn("python -c", command[5])

    def test_next_waiting_module_uses_the_composable_action(self):
        source = (Path(__file__).parents[1] / "agent_fleet/next_waiting.py").read_text()
        self.assertEqual(source, "from .actions import next_waiting\n\n\nnext_waiting()\n")

    def test_create_uses_claudes_existing_provider_presentation(self):
        host = "newton" if os.uname().nodename != "newton" else "lovelace"
        with mock.patch("agent_fleet.actions.muster_input",
                        side_effect=[host, "claude", "analysis", "/work"]) as prompt, \
             mock.patch("agent_fleet.actions.fleet_action",
                        return_value={"source": f"alan:claude-deadbeef@{host}"}) as action, \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            actions.create_prompt()
        self.assertEqual(
            prompt.call_args_list[1],
            mock.call("agent", ("codex", "claude"), context=host))
        action.assert_called_once_with({"operation": "create", "host": host,
                                       "agent": "claude", "name": "analysis",
                                       "cwd": "/work"})
        show.assert_called_once_with(f"alan:claude-deadbeef@{host}")
        self.assertNotIn('"tmux", "new-session"',
                         (Path(__file__).parents[1] / "agent_fleet/actions.py").read_text())

    def test_fleet_package_requires_the_canonical_alan_client(self):
        package = (Path(__file__).parents[1] / "PKGBUILD").read_text()
        self.assertIn(
            "depends=('alan>=1:3.0.0.a1' ", package)
        self.assertNotIn('"$pkgdir/usr/bin/fleet"', package)
        self.assertIn('"$pkgdir/usr/lib/agent-fleet/ui"', package)
        private = (Path(__file__).parents[1] / "agent_fleet/ui_process.py").read_text()
        for removed in ("serve", "quota", "projection", "alan-retire", "mutate"):
            self.assertNotIn(f'command("{removed}"', private)

    def test_private_ui_rejects_unknown_requests(self):
        with self.assertRaises(SystemExit):
            ui_process.main(["unknown"])

    def test_create_rejects_unknown_agent_before_process_creation(self):
        fleet = Fleet()
        fleet.unavailable.clear()
        with mock.patch("agent_fleet.daemon.hosts", return_value=["lovelace"]), \
             mock.patch.object(fleet, "authority") as execute:
            with self.assertRaisesRegex(ValueError, "Claude or Codex"):
                asyncio.run(fleet.action({"operation": "create", "host": "lovelace",
                                          "agent": "python", "name": "work",
                                          "cwd": "/work"}))
        execute.assert_not_awaited()

    def test_reboot_snapshot_returns_report_without_printing(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("agent_fleet.reboot.sh", return_value=""), \
             mock.patch("builtins.print") as output:
            report = reboot.snapshot(Path(directory) / "snapshot.json")
        self.assertEqual(report["panes"], [])
        self.assertEqual(report["captured"], 0)
        output.assert_not_called()

    def test_reboot_restore_returns_report_without_printing(self):
        pane = {"session": "work", "window": 0, "window_name": "main",
                "pane": 0, "cwd": "/work", "command": "bash"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps([pane]))
            with mock.patch("agent_fleet.reboot.sh", return_value=""), \
                 mock.patch("agent_fleet.reboot.subprocess.run") as run, \
                 mock.patch("builtins.print") as output:
                report = reboot.restore(path)
        self.assertEqual(report, {"panes": [pane], "restored": 1})
        self.assertEqual(run.call_args_list[0].args[0][:5],
                         ["/usr/bin/tmux", "-N", "new-session", "-d", "-s"])
        self.assertTrue(any(call.args[0][2] == "rename-window" for call in run.call_args_list))
        output.assert_not_called()

    def test_daemon_create_waits_on_its_own_observed_generation(self):
        host = "lovelace"
        fleet = Fleet()
        fleet.unavailable.clear()
        actor = alan_inventory(host, [{
            "addr": f"codex-1@{host}", "kind": "codex", "state": "waiting",
            "created": 1, "human_activity": 0, "cwd": "/work",
            "active_evaluation": None, "evaluation_started": 0,
        }])[0]

        async def exercise():
            with mock.patch("agent_fleet.daemon.hosts", return_value=[host]), \
                 mock.patch.object(fleet, "authority", return_value={
                     "source": actor.ref.key}) as execute:
                pending = asyncio.create_task(fleet.action({
                    "operation": "create", "host": host, "agent": "codex",
                    "name": "work", "cwd": "/work"}))
                await asyncio.sleep(0)
                self.assertFalse(pending.done())
                fleet.sessions[host] = [actor]
                fleet.observed += 1
                async with fleet.changed:
                    fleet.changed.notify_all()
                self.assertEqual(await pending, {"source": actor.ref.key})
            execute.assert_awaited_once()

        asyncio.run(exercise())

    def test_tmux_name_normalization_preserves_spaces(self):
        self.assertEqual(Fleet.action_name(" Test session. "), "Test session")
        self.assertEqual(Fleet.action_name("docs:v2.1"), "docs-v2-1")

    def test_archive_retires_exact_alan_actor_by_address(self):
        host = os.uname().nodename
        identity = "00000000-0000-4000-8000-000000000001"
        actor = f"codex-{identity}@{host}"
        key = f"alan:{actor}"
        with mock.patch("agent_fleet.actions.fleet_action") as action, \
             mock.patch("agent_fleet.actions.viewer.slots", return_value=[("main", key)]), \
             mock.patch("agent_fleet.actions.viewer.request") as request:
            actions.archive(key)
        action.assert_called_once_with({"operation": "archive", "source": key})
        request.assert_called_once_with("main", "")

    def test_archive_retires_alan_claude_without_projected_native_identity(self):
        host = os.uname().nodename
        session = Session(
            SessionRef(ServerRef(host, "", 0, 0, "alan"), f"claude-1@{host}"),
            "work", 1, 0, 0, 1, "alan", "", "/work",
            "claude", "waiting", "", 0, "", 1)
        fleet = Fleet()
        fleet.sessions[host] = [session]
        fleet.unavailable.clear()
        with mock.patch.object(fleet, "authority", return_value={}) as execute, \
             mock.patch.object(fleet, "wait_for_absence") as absent:
            self.assertEqual(asyncio.run(fleet.action(
                {"operation": "archive", "source": session.ref.key})), {})
        execute.assert_awaited_once_with(
            host, {"operation": "archive-alan", "actor": f"claude-1@{host}"})
        absent.assert_awaited_once_with(session.ref.key)

    def test_archive_retires_bare_alan_language_actor_by_address(self):
        host = os.uname().nodename
        session = Session(
            SessionRef(ServerRef(host, "", 0, 0, "alan"), f"llm-1@{host}"),
            "review", 1, 0, 0, 1, "alan", "", "/work",
            "llm", "waiting")
        fleet = Fleet()
        fleet.sessions[host] = [session]
        fleet.unavailable.clear()
        with mock.patch.object(fleet, "authority", return_value={}) as execute, \
             mock.patch.object(fleet, "wait_for_absence"):
            asyncio.run(fleet.action(
                {"operation": "archive", "source": session.ref.key}))
        execute.assert_awaited_once_with(
            host, {"operation": "archive-alan", "actor": f"llm-1@{host}"})

    def test_archive_verifies_transcript_then_closes_exact_tmux_identity(self):
        session = self.session("lovelace")
        session = Session(**{**session.__dict__, "transcript_id": "thread-1"})
        fleet = Fleet()
        fleet.sessions["lovelace"] = [session]
        fleet.unavailable.clear()
        with mock.patch.object(fleet, "authority", return_value={}) as execute, \
             mock.patch.object(fleet, "wait_for_absence") as absent:
            asyncio.run(fleet.action(
                {"operation": "archive", "source": session.ref.key}))
        execute.assert_awaited_once_with("lovelace", {
            "operation": "archive-tmux", "source": session.ref.key,
            "agent": "codex", "transcript": "thread-1"})
        absent.assert_awaited_once_with(session.ref.key)

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
        context = {"history": [{
            "key": "alan:codex-1@lovelace", "host": "lovelace",
            "agent": "codex", "name": "work", "cwd": "/work", "mtime": 20}]}
        with mock.patch("agent_fleet.actions.commander_projection",
                        return_value=json.dumps(context)):
            rows = actions.history()
        self.assertEqual(rows, [
            ("alan:codex-1@lovelace", "lovelace", "codex", "work", "/work")])

    def test_history_keeps_retired_bare_language_actor_without_native_identity(self):
        context = {"history": [{
            "key": "alan:llm-1@lovelace", "host": "lovelace",
            "agent": "llm", "name": "review", "cwd": "/work", "mtime": 20}]}
        with mock.patch("agent_fleet.actions.commander_projection",
                        return_value=json.dumps(context)):
            rows = actions.history()
        self.assertEqual(rows, [
            ("alan:llm-1@lovelace", "lovelace", "llm", "review", "/work")])

    def test_refresh_restarts_exact_waiting_actor_and_reopens_every_shown_slot(self):
        host = os.uname().nodename
        session = Session(
            SessionRef(ServerRef(host, "", 0, 0, "alan"), f"codex-1@{host}"),
            "work", 1, 0, 0, 1, "alan", "", "/work",
            "codex", "waiting", "", 0, "thread-1", 1)
        with mock.patch("agent_fleet.actions.fleet_action") as action, \
             mock.patch("agent_fleet.actions.viewer.slots",
                        return_value=[("main", session.ref.key),
                                      ("right", session.ref.key)]), \
             mock.patch("agent_fleet.actions.viewer.request") as request:
            actions.refresh(session.ref.key)
        action.assert_called_once_with(
            {"operation": "refresh", "source": session.ref.key})
        self.assertEqual(request.call_args_list, [
            mock.call("main", session.ref.key), mock.call("right", session.ref.key)])

    def test_retained_unavailable_actor_remains_the_native_history_authority(self):
        context = {"history": [{
            "key": "alan:codex-1@lovelace", "host": "lovelace",
            "agent": "codex", "name": "work", "cwd": "/work", "mtime": 20}]}
        with mock.patch("agent_fleet.actions.commander_projection",
                        return_value=json.dumps(context)):
            rows = actions.history()
        self.assertEqual(rows, [
            ("alan:codex-1@lovelace", "lovelace", "codex", "work", "/work")])

    def test_history_open_retries_the_same_alan_address(self):
        key = "alan:codex-1@lovelace"
        with mock.patch("agent_fleet.actions.fleet_action",
                        return_value={"source": key}) as action, \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            actions.open_history(key)
        action.assert_called_once_with(
            {"operation": "restore", "history": key, "name": ""})
        show.assert_called_once_with(key)

    def test_transcript_history_open_captures_remote_resume_failure(self):
        key = "lovelace:codex:full-thread-id"
        with mock.patch("agent_fleet.actions.desktop_input", return_value="work"), \
             mock.patch("agent_fleet.actions.fleet_action",
                        side_effect=RuntimeError("resume refused")) as action:
            with self.assertRaisesRegex(RuntimeError, "resume refused"):
                actions.open_history(key)
        action.assert_called_once_with(
            {"operation": "restore", "history": key, "name": "work"})

    def test_transcript_history_open_places_the_reconciled_source(self):
        key = "lovelace:codex:full-thread-id"
        with mock.patch("agent_fleet.actions.desktop_input", return_value="work"), \
             mock.patch("agent_fleet.actions.fleet_action",
                        return_value={"source": "lovelace:/tmp/tmux:1:2:$3"}), \
             mock.patch("agent_fleet.actions.viewer.open_main") as show:
            actions.open_history(key)
        show.assert_called_once_with("lovelace:/tmp/tmux:1:2:$3")

    def test_archive_failure_is_visible_in_muster(self):
        fleet = Fleet()
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()

        async def exercise():
            reader = asyncio.StreamReader()
            reader.feed_data(b'{"operation":"archive","source":"gone"}\n')
            reader.feed_eof()
            await fleet.reply(reader, writer)

        asyncio.run(exercise())
        response = json.loads(writer.write.call_args.args[0])
        self.assertEqual(response, {"ok": False,
                                    "error": "session disappeared: gone"})
        self.assertEqual(fleet.action_error, "session disappeared: gone")

    def test_stale_archive_row_is_visible_in_muster(self):
        fleet = Fleet()
        fleet.action_error = "session disappeared: gone"
        with mock.patch.object(fleet, "projected", return_value=[]), \
             mock.patch("agent_fleet.daemon.render.header_text",
                        return_value="columns"):
            writer = mock.Mock()
            writer.drain = mock.AsyncMock()

            async def exercise():
                reader = asyncio.StreamReader()
                reader.feed_data(b"header\n")
                reader.feed_eof()
                await fleet.reply(reader, writer)

            asyncio.run(exercise())
        self.assertEqual(writer.write.call_args.args[0],
                         b"Action failed: session disappeared: gone\ncolumns\n")

    def test_history_open_failure_is_visible_in_muster(self):
        with mock.patch("agent_fleet.actions.fleet_action",
                        side_effect=RuntimeError("native identity changed")):
            with self.assertRaisesRegex(RuntimeError, "native identity changed"):
                actions.open_history("alan:codex-1@lovelace")

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

    def test_source_key_contains_server_generation(self):
        session = self.session("newton")
        self.assertEqual(split_key(session.ref.key),
                         ("newton", "/tmp/tmux/default", 12, 10, "$1"))

    def test_archive_is_the_only_destructive_surface(self):
        root = Path(__file__).parents[1]
        paths = list((root / "agent_fleet").glob("*.py"))
        source = "\n".join(path.read_text() for path in paths)
        self.assertEqual(source.count('"kill-session"'), 2)
        for command in ("kill-window", "unlink-window"):
            self.assertNotIn(command, source)

    def test_commander_routes_to_the_lovelace_alan_client(self):
        launcher = (Path(__file__).parents[1] / "fleet-commander").read_text()
        self.assertIn("ssh -tt -o BatchMode=yes lovelace fleet-commander", launcher)
        self.assertIn("from agent_fleet.commander_client import run; run()", launcher)
        self.assertIn('[ "$#" -eq 0 ] || exit 2', launcher)
        self.assertNotIn("session_index.jsonl", launcher)
        self.assertNotIn("codex", launcher)

    def test_muster_and_main_route_to_the_lovelace_hub(self):
        root = Path(__file__).parents[1]
        muster = (root / "fleet-muster").read_text()
        main = (root / "fleet-viewer").read_text()
        service = (root / "fleet.service").read_text()
        self.assertIn("from agent_fleet.workstation import serve", muster)
        self.assertIn('-R "$remote_socket:$local_socket"', muster)
        self.assertIn('set -- --workstation "$workstation"', muster)
        self.assertIn('export SSH_AUTH_SOCK="/run/user/$(id -u)/gnupg/S.gpg-agent.ssh"',
                      muster)
        self.assertIn("new-session -d -s fleet@main", main)
        self.assertIn("set-option -t fleet@main prefix None", main)
        self.assertIn("set-option -t fleet@main status off", main)
        self.assertIn("set-option -t fleet@main mouse on", main)
        self.assertIn("set-option -t fleet@muster mouse off", muster)
        self.assertIn("/usr/bin/nc -U", main)
        self.assertIn("ConditionHost=lovelace", service)

    def test_main_viewer_restores_its_transparent_status(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            environment = {key: value for key, value in os.environ.items()
                           if key != "TMUX"}
            environment["TMUX_TMPDIR"] = str(directory / "tmux")
            environment["XDG_RUNTIME_DIR"] = str(directory / "runtime")
            environment["PYTHONPATH"] = str(root)
            (directory / "tmux").mkdir()
            (directory / "runtime").mkdir()
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", "fleet@main",
                 f"exec {sys.executable} -c 'from agent_fleet.viewer import serve; "
                 "serve(\"main\")'"], check=True, env=environment)
            try:
                subprocess.run(["tmux", "set-option", "-t", "fleet@main",
                                "status", "on"], check=True, env=environment)
                for _ in range(100):
                    if (directory / "runtime" / "agent-fleet" /
                            "viewer-main.sock").exists():
                        break
                    time.sleep(0.01)
                self.assertTrue((directory / "runtime" / "agent-fleet" /
                                 "viewer-main.sock").exists())
                subprocess.run([root / "fleet-viewer", "main"],
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               env=environment)
                status = subprocess.run(
                    ["tmux", "show-option", "-t", "fleet@main", "-v", "status"],
                    check=True, text=True, capture_output=True,
                    env=environment).stdout.strip()
                self.assertEqual(status, "off")
            finally:
                subprocess.run(["tmux", "kill-server"], env=environment)

    def test_muster_always_opens_the_global_main_viewer(self):
        source = (Path(__file__).parents[1] / "agent_fleet/ui.py").read_text()
        self.assertIn("/usr/lib/agent-fleet/fleet-open main {1}", source)
        self.assertNotIn("/usr/lib/agent-fleet/ui show", source)
        self.assertIn("load:transform(/usr/lib/agent-fleet/ui cursor)+unbind(load)", source)
        self.assertIn('"--no-sort"', source)
        self.assertIn("enable-search+toggle-sort", source)
        self.assertNotIn('"--nth=2.."', source)
        self.assertIn("change-prompt(Search: )", source)
        self.assertIn("c:execute-silent(/usr/lib/agent-fleet/ui create-tab)", source)
        self.assertIn("r:execute-silent(/usr/lib/agent-fleet/ui rename-tab {1})", source)
        self.assertIn("l:execute-silent(/usr/lib/agent-fleet/ui fold open {1})+transform-header", source)
        self.assertIn("h:execute-silent(/usr/lib/agent-fleet/ui fold close {1})+transform-header", source)
        self.assertIn("p:execute-silent(/usr/lib/agent-fleet/ui toggle python)+transform-header", source)
        self.assertIn('"--footer-border=bottom"', source)

    def test_muster_projects_recursive_folds_and_python_independently(self):
        root = "codex-root@newton"
        language = "claude-child@newton"
        python = "python-child@newton"
        principal = "will@newton"
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = [
            {"addr": root, "kind": "codex"},
            {"addr": language, "kind": "claude"},
            {"addr": python, "kind": "python"},
            {"addr": principal, "kind": "principal"},
        ]
        graph.add_node(f"{root}#0", stream=root, op="create")
        graph.add_node(f"{principal}#0", stream=principal, op="create")
        graph.add_node(f"{principal}#1", stream=principal, op="spawn")
        graph.add_edge(f"{principal}#1", f"{root}#0", key="spawn")
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

        def projected(opened, python_visible):
            with mock.patch.object(ui, "snapshot", return_value=raw), \
                 mock.patch.object(ui, "expanded", return_value=set(opened)), \
                 mock.patch.object(ui, "option", return_value=python_visible):
                return [item.session.ref.session_id for item in ui.ordered()[0]]

        self.assertEqual(projected((), False), [root])
        self.assertEqual(projected((root,), False), [root, language])
        self.assertEqual(projected((root,), True), [root, language, python])

    def test_toggle_changes_the_named_muster_tmux_option(self):
        with mock.patch.object(ui, "option", return_value=False), \
             mock.patch.object(ui.subprocess, "run") as run:
            ui.toggle("python")
        run.assert_called_once_with(
            ["/usr/bin/tmux", "-N", "set-option", "-t", "=fleet@muster:", "@fleet_show_python", "1"],
            check=True,
        )

    def test_fold_opens_and_closes_only_the_selected_expandable_actor(self):
        root = Session(
            SessionRef(ServerRef("lovelace", "", 0, 0, "alan"),
                       "codex-root@lovelace"),
            "root", 1, 0, 0, 1, "alan", "", "/work", "codex", "waiting")
        projection = alan.Projected(root, 0, 2, False)
        for action, actors, expected in (
                ("open", {"claude-other@lovelace"},
                 f"claude-other@lovelace {root.ref.session_id}"),
                ("open", {"claude-other@lovelace", root.ref.session_id},
                 f"claude-other@lovelace {root.ref.session_id}"),
                ("close", {"claude-other@lovelace", root.ref.session_id},
                 "claude-other@lovelace"),
                ("close", {"claude-other@lovelace"},
                 "claude-other@lovelace")):
            with self.subTest(action=action, actors=actors), \
                 mock.patch.object(ui, "ordered", return_value=([projection], {}, [])), \
                 mock.patch.object(ui, "expanded", return_value=actors), \
                 mock.patch.object(ui.subprocess, "run") as run:
                ui.fold(action, root.ref.key)
            run.assert_called_once_with([
                "/usr/bin/tmux", "-N", "set-option", "-t", "=fleet@muster:", "@fleet_expanded",
                expected], check=True)

    def test_fold_ignores_native_and_leaf_rows(self):
        native = self.session("lovelace")
        actor = Session(
            SessionRef(ServerRef("lovelace", "", 0, 0, "alan"),
                       "claude-leaf@lovelace"),
            "leaf", 1, 0, 0, 1, "alan", "", "/work", "claude", "waiting")
        for projection in (alan.Projected(native, 0, 1, False),
                           alan.Projected(actor, 1, 0, False)):
            with self.subTest(key=projection.session.ref.key), \
                 mock.patch.object(ui, "ordered", return_value=([projection], {}, [])), \
                 mock.patch.object(ui, "expanded") as expanded, \
                 mock.patch.object(ui.subprocess, "run") as run:
                ui.fold("open", projection.session.ref.key)
            expanded.assert_not_called()
            run.assert_not_called()

    def test_recursive_folds_use_ephemeral_state_in_an_isolated_tmux_server(self):
        host = os.uname().nodename
        root = f"codex-root@{host}"
        child = f"claude-child@{host}"
        grandchild = f"llm-grandchild@{host}"
        python = f"python-child@{host}"
        principal = f"will@{host}"
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = [
            {"addr": root, "kind": "codex"},
            {"addr": child, "kind": "claude"},
            {"addr": grandchild, "kind": "llm"},
            {"addr": python, "kind": "python"},
            {"addr": principal, "kind": "principal"},
        ]
        for actor in (root, child, grandchild, python):
            graph.add_node(f"{actor}#0", stream=actor, op="create")
        graph.add_node(f"{principal}#0", stream=principal, op="create")
        graph.add_node(f"{principal}#1", stream=principal, op="spawn")
        graph.add_edge(f"{principal}#1", f"{root}#0", key="spawn")
        for position, (parent, descendant) in enumerate((
                (root, child), (child, grandchild), (root, python)), 1):
            source = f"{parent}#{position}"
            graph.add_node(source, stream=parent, op="spawn")
            graph.add_edge(source, f"{descendant}#0", key="spawn")
        sessions = [
            Session(SessionRef(ServerRef(host, "", 0, 0, "alan"), actor),
                    actor, 1, 0, 0, 1, "alan", "", "/work", kind, "waiting")
            for actor, kind in ((root, "codex"), (child, "claude"),
                                (grandchild, "llm"), (python, "python"))
        ]

        with tempfile.TemporaryDirectory() as directory:
            environment = {key: value for key, value in os.environ.items()
                           if key != "TMUX"}
            environment["TMUX_TMPDIR"] = directory
            subprocess.run(["tmux", "new-session", "-d", "-s", "fleet@muster",
                            "sleep 30"], check=True, env=environment)
            try:
                with mock.patch.dict(os.environ, environment, clear=True):
                    def projected():
                        return alan.project(
                            sessions, graph, expanded=ui.expanded(),
                            show_python=ui.option("@fleet_show_python"))

                    with mock.patch.object(
                            ui, "ordered", side_effect=lambda: (projected(), {}, [])):
                        ui.fold("open", f"alan:{root}")
                        self.assertEqual(
                            [item.session.ref.session_id for item in projected()],
                            [root, child])
                        ui.fold("open", f"alan:{child}")
                        self.assertEqual(
                            [item.session.ref.session_id for item in projected()],
                            [root, child, grandchild])
                        ui.fold("close", f"alan:{root}")
                        self.assertEqual(
                            [item.session.ref.session_id for item in projected()], [root])
                        ui.fold("open", f"alan:{root}")
                        self.assertEqual(
                            [item.session.ref.session_id for item in projected()],
                            [root, child, grandchild])
                        ui.toggle("python")
                        self.assertEqual(
                            [item.session.ref.session_id for item in projected()],
                            [root, child, grandchild, python])
            finally:
                subprocess.run(["tmux", "kill-server"], env=environment)

    def test_fzf_reload_preserves_child_selection_by_stable_key(self):
        root = "alan:codex-root@lovelace"
        child = "alan:claude-child@lovelace"
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            rows = directory / "rows"
            rows.write_text(f"{root}\troot\n{child}\t  child\n")
            socket_path = directory / "fzf.sock"
            tmux_runtime = directory / "tmux"
            tmux_runtime.mkdir()
            environment = {key: value for key, value in os.environ.items()
                           if key != "TMUX"}
            environment["TMUX_TMPDIR"] = str(tmux_runtime)
            command = (
                f"exec fzf --listen {shlex.quote(str(socket_path))} --track --disabled "
                f"--no-input --delimiter='\\t' --with-nth=2.. --id-nth=1 "
                f"--layout=reverse --no-sort "
                f"< {shlex.quote(str(rows))}"
            )
            subprocess.run(["tmux", "new-session", "-d", "-s", "fleet@muster",
                            command], check=True, env=environment)
            try:
                for _ in range(100):
                    if socket_path.exists():
                        break
                    time.sleep(.01)
                self.assertTrue(socket_path.exists())
                endpoint = ["curl", "-fsS", "--unix-socket", str(socket_path)]
                state = {}
                for _ in range(100):
                    state = json.loads(subprocess.run(
                        endpoint + ["http://localhost"], check=True,
                        env=environment, text=True, capture_output=True).stdout)
                    if len(state.get("matches", [])) == 2:
                        break
                    time.sleep(.01)
                position = next(
                    index for index, match in enumerate(state["matches"], 1)
                    if match["text"].partition("\t")[0] == child)
                subprocess.run(endpoint + ["-XPOST", "-d", f"pos({position})",
                                           "http://localhost"],
                               check=True, env=environment,
                               stdout=subprocess.DEVNULL)
                for _ in range(100):
                    state = json.loads(subprocess.run(
                        endpoint + ["http://localhost"], check=True,
                        env=environment, text=True, capture_output=True).stdout)
                    if state.get("current", {}).get("text", "").partition("\t")[0] == child:
                        break
                    time.sleep(.01)
                subprocess.run(endpoint + [
                    "-XPOST", "-d", f"reload-sync(cat {rows})",
                    "http://localhost"], check=True, env=environment,
                    stdout=subprocess.DEVNULL)
                state = json.loads(subprocess.run(
                    endpoint + ["http://localhost"], check=True,
                    env=environment, text=True, capture_output=True).stdout)
                self.assertEqual(state["current"]["text"].partition("\t")[0], child)
            finally:
                subprocess.run(["tmux", "kill-server"], env=environment)

    def test_private_ui_dispatches_fold_and_rejects_language_toggle(self):
        with mock.patch.object(ui, "fold") as fold:
            ui_process.main(["fold", "open", "alan:claude-one@lovelace"])
        fold.assert_called_once_with("open", "alan:claude-one@lovelace")
        with mock.patch.object(ui, "toggle") as toggle:
            ui_process.main(["toggle", "python"])
        toggle.assert_called_once_with("python")
        with self.assertRaises(SystemExit):
            ui_process.main(["toggle", "language"])

    def test_private_ui_has_no_python_show_command(self):
        with self.assertRaises(SystemExit):
            ui_process.main(["show", "alan:claude-child@lovelace", "--slot", "main"])

    def test_next_waiting_unwraps_projected_sessions(self):
        active = replace(self.session("lovelace", "$1"), reported_state="waiting")
        child = replace(self.session("lovelace", "$2"), reported_state="waiting")
        projected = [alan.Projected(active, 0, 1, True),
                     alan.Projected(child, 1, 0, False)]
        with mock.patch.object(ui, "ordered", return_value=(projected, {}, [])), \
             mock.patch.object(actions.viewer, "slots",
                               return_value=[("main", active.ref.key)]), \
             mock.patch.object(actions.viewer, "open_main") as show:
            actions.next_waiting()
        show.assert_called_once_with(child.ref.key)

    def test_header_counts_only_the_current_fold_projection(self):
        root = replace(self.session("lovelace", "$1"), reported_state="waiting")
        child = replace(self.session("lovelace", "$2"), reported_state="working")
        collapsed = render.header_text([alan.Projected(root, 0, 1, False)], {}, [])
        expanded = render.header_text([alan.Projected(root, 0, 1, True),
                                       alan.Projected(child, 1, 0, False)], {}, [])
        self.assertIn("0 working  1 waiting  1 total", collapsed)
        self.assertIn("1 working  1 waiting  2 total", expanded)

    def test_footer_contains_only_action_hints(self):
        from agent_fleet.ui import footer
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((100, 24))):
            self.assertEqual(
                footer(),
                "Enter open  c create  r rename  R refresh  x archive  l open fold  h close fold  p python")

    def test_column_header_renders_the_exact_icon_bytes(self):
        from agent_fleet.render import column_header
        session = replace(self.session("lovelace", "$1"),
                          reported_state="waiting")
        projected = [alan.Projected(session, 0, 0, False)]
        self.assertEqual(
            column_header(projected),
            "\uf108 \uf2db  \uf017   \uf111      \uf02b                    \uf036  0 working  1 waiting  1 total")

    def test_header_wrapper_strips_only_the_protocol_framing(self):
        with mock.patch.object(ui.hot, "fetch",
                               return_value="Claude u\nOpenAI u\ncolumns\n") as fetch:
            self.assertEqual(ui.header(), "Claude u\nOpenAI u\ncolumns")
        fetch.assert_called_once_with("header")

    def test_column_header_counts_sessions_by_state(self):
        from agent_fleet.render import column_header
        states = ["working", "working", "waiting", "needs-action"]
        sessions = [replace(self.session("lovelace", f"${i}"), reported_state=state)
                    for i, state in enumerate(states)]
        projected = [alan.Projected(session, 0, 0, False) for session in sessions]
        self.assertIn("2 working  1 waiting  4 total", column_header(projected))

    def test_rows_render_fold_count_depth_and_stable_identity(self):
        root = replace(self.session("lovelace", "$1"), name="root")
        child = replace(self.session("lovelace", "$2"), name="child")
        projected = [alan.Projected(root, 0, 2, True),
                     alan.Projected(child, 1, 0, False)]
        rendered = render.rows_text(projected, [], 100, now=1)
        self.assertIn(f"{root.ref.key}\t", rendered)
        self.assertIn(f"{child.ref.key}\t", rendered)
        self.assertIn("▾ 2", rendered)
        self.assertIn("  child", rendered)

        projected = [alan.Projected(root, 0, 2, False),
                     alan.Projected(child, 1, 0, False)]
        root_line, child_line = render.rows_text(projected, [], 100,
                                                 now=1).splitlines()
        self.assertIn("▸ 2", root_line)
        self.assertNotIn("▸", child_line)
        self.assertNotIn("▾", child_line)

    def test_rows_size_their_summary_to_the_requested_width(self):
        session = replace(self.session("lovelace", "$1"),
                          summary="s" * 300)
        projected = [alan.Projected(session, 0, 0, False)]
        narrow = render.rows_text(projected, [], 100, now=1)
        wide = render.rows_text(projected, [], 140, now=1)
        self.assertEqual(len(wide) - len(narrow), 40)

    def test_claude_and_codex_use_distinct_provider_colours(self):
        self.assertNotEqual(AGENT_COLOUR["claude"], AGENT_COLOUR["codex"])

    def test_create_opens_inside_the_muster(self):
        with mock.patch("subprocess.run") as run:
            actions.create_tab()
        run.assert_called_once_with(
            ["/usr/bin/tmux", "-N", "new-window", "-t", "fleet@muster", "-n", "create",
             "exec /usr/lib/agent-fleet/ui create"], check=True)

    def test_rename_opens_inside_the_muster(self):
        key = "lovelace:/tmp/tmux:1:2:$3"
        with mock.patch("subprocess.run") as run:
            actions.rename_tab(key)
        run.assert_called_once_with(
            ["/usr/bin/tmux", "-N", "new-window", "-t", "fleet@muster", "-n", "rename",
             "exec /usr/lib/agent-fleet/ui rename-prompt 'lovelace:/tmp/tmux:1:2:$3'"], check=True)

    def test_named_viewers_remain_local(self):
        launcher = (Path(__file__).parents[1] / "fleet-viewer").read_text()
        self.assertIn("from agent_fleet.viewer import serve", launcher)

    def test_ssh_environment_uses_stable_agent_socket(self):
        environment = ssh_environment()
        self.assertEqual(environment["SSH_AUTH_SOCK"],
                         f"/run/user/{os.getuid()}/gnupg/S.gpg-agent.ssh")

    def test_viewer_uses_stable_agent_environment(self):
        source = (Path(__file__).parents[1] / "agent_fleet/viewer.py").read_text()
        self.assertIn("env=ssh_environment()", source)

    def test_management_prompts_never_read_raw_terminal_input(self):
        source = (Path(__file__).parents[1] / "agent_fleet/actions.py").read_text()
        workstation_source = (
            Path(__file__).parents[1] / "agent_fleet/workstation.py").read_text()
        self.assertNotRegex(source, r"(?<![A-Za-z_])input\(")
        self.assertIn('"rofi", "-dmenu"', workstation_source)

    def test_missing_alan_presentation_fails_without_creating_it(self):
        actor = f"codex-1@{os.uname().nodename}"
        session = Session(SessionRef(ServerRef(os.uname().nodename, "", 0, 0, "alan"),
                                     actor), "codex", 0, 0, 0, 1, "alan", "", "/work",
                          "codex")
        state = viewer.Attachment("main", "/dev/pts/9")
        failed = subprocess.CalledProcessError(1, ["tmux"])
        with mock.patch.object(state, "find", return_value=session), \
             mock.patch.object(viewer.subprocess, "run", side_effect=failed), \
             mock.patch.object(viewer.subprocess, "Popen") as popen:
            with self.assertRaises(subprocess.CalledProcessError):
                state.resolve(f"alan:{actor}")
        popen.assert_not_called()

    def test_python_open_retains_the_fleet_owned_presentation(self):
        actor = f"python-1@{os.uname().nodename}"
        session = Session(SessionRef(ServerRef(os.uname().nodename, "", 0, 0, "alan"),
                                     actor), "python", 0, 0, 0, 1, "alan", "", "/work",
                          "python")
        state = viewer.Attachment("main", "/dev/pts/9")
        missing = mock.Mock(stdout="\n")
        listed = mock.Mock(stdout="/tmp/tmux/default\t12\t10\t$1\n")
        with mock.patch.object(state, "find", return_value=session), \
             mock.patch.object(viewer.presentation, "target") as target, \
             mock.patch.object(viewer.subprocess, "run", side_effect=[missing, listed]):
            self.assertEqual(state.resolve(f"alan:{actor}"),
                             ("/tmp/tmux/default", 12, 10, "$1"))
        target.assert_called_once_with(actor, {"kind": "python", "cwd": "/work"})

    def test_existing_python_open_does_not_touch_its_presentation(self):
        actor = f"python-1@{os.uname().nodename}"
        session = Session(SessionRef(ServerRef(os.uname().nodename, "", 0, 0, "alan"),
                                     actor), "python", 0, 0, 0, 1, "alan", "", "/work",
                          "python")
        state = viewer.Attachment("main", "/dev/pts/9")
        listed = mock.Mock(stdout="/tmp/tmux/default\t12\t10\t$1\n")
        with mock.patch.object(state, "find", return_value=session), \
             mock.patch.object(viewer.presentation, "target") as target, \
             mock.patch.object(viewer.subprocess, "run", return_value=listed):
            state.resolve(f"alan:{actor}")
        target.assert_not_called()

    def test_alan_preview_captures_the_actor_owned_terminal(self):
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                self.assert_actor_preview_captures_terminal(kind)

    def assert_actor_preview_captures_terminal(self, kind):
        session = mock.Mock(session_name="fleet@alan-hash")
        server = mock.Mock(sessions=[session])
        with mock.patch.object(tmux.alan.loop, "observe") as observe, \
             mock.patch("agent_fleet.tmux.alan.runtime_name", return_value="hash"), \
             mock.patch("agent_fleet.tmux.server", return_value=server), \
             mock.patch("agent_fleet.tmux.capture_pane",
                        return_value="conversation\n") as preview:
            self.assertEqual(
                tmux.capture(f"alan:{kind}-1@newton", 80, 20),
                "conversation\n")
        preview.assert_called_once_with(session, 80, 20)
        observe.assert_not_called()

    def test_alan_preview_has_no_transcript_fallback(self):
        with mock.patch("agent_fleet.tmux.alan.runtime_name", return_value="hash"), \
             mock.patch("agent_fleet.tmux.server",
                        return_value=mock.Mock(sessions=[])):
            with self.assertRaisesRegex(RuntimeError, "terminal is unavailable"):
                tmux.capture("alan:codex-1@newton", 80, 20)

    def test_viewer_clear_remains_an_internal_primitive(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as runtime:
            env = {**os.environ, "XDG_RUNTIME_DIR": runtime,
                   "PYTHONPATH": str(root)}
            master, slave = pty.openpty()
            process = subprocess.Popen([sys.executable, "-c",
                                        "from agent_fleet.viewer import serve; serve('test')"],
                                       env=env, stdin=slave)
            os.close(slave)
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
                os.close(master)

    def test_quota_only_events_force_an_inventory_emit(self):
        source = (Path(__file__).parents[1] / "agent_fleet/tmux.py").read_text()
        self.assertIn('force = "quota" in events', source)
        self.assertIn("if serial != previous or force:", source)
