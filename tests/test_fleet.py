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
from agent_fleet.protocol import decode, decode_graph, decode_observation, encode
from agent_fleet.render import AGENT_COLOUR, STATE_ORDER, recency
from agent_fleet.tmux import split_key
from agent_fleet import actions, authority, proc
from agent_fleet import alan
from agent_fleet.config import machine, ssh_environment
from agent_fleet.alan import inventory as alan_inventory
from agent_fleet.alan import Watcher as AlanWatcher
from agent_fleet.alan import resume as alan_resume
from agent_fleet import hot, render, viewer
from agent_fleet import workstation
from agent_fleet import tmux
from agent_fleet import ui
from agent_fleet import ui_process
from agent_fleet import ui_process as cli
from agent_fleet.daemon import Fleet


def without_tmux_client():
    """$TMUX overrides TMUX_TMPDIR, so a fixture server needs it removed."""
    environment = {name: value for name, value in os.environ.items()
                   if name not in {"TMUX", "TMUX_PANE"}}
    environment["FLEET_TMUX"] = str(Path(__file__).parents[1] / "fleet-tmux")
    return environment


class IdentityTests(unittest.TestCase):
    SOURCE_A = "lovelace:/tmp/tmux/default:1:1:$1"
    SOURCE_B = "lovelace:/tmp/tmux/default:1:1:$2"
    SOURCE_C = "lovelace:/tmp/tmux/default:1:1:$3"
    SOURCE_D = "lovelace:/tmp/tmux/default:1:1:$4"
    SOURCE_J = "lovelace:/tmp/tmux/default:1:1:$10"
    SOURCE_K = "lovelace:/tmp/tmux/default:1:1:$11"
    def session(self, host, sid="$1"):
        return Session(SessionRef(ServerRef(host, "/tmp/tmux/default", 12, 10), sid),
                       "work", 1, 2, 0, 1, "codex", "waiting", "/work")

    def fold_fleet(self):
        host = "lovelace"
        root = f"codex-root@{host}"
        child = f"claude-child@{host}"
        python = f"python-child@{host}"
        principal = f"will@{host}"
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = [
            {"addr": root, "kind": "codex"},
            {"addr": child, "kind": "claude"},
            {"addr": python, "kind": "python"},
            {"addr": principal, "kind": "principal"},
        ]
        for actor in (root, child, python, principal):
            graph.add_node(f"{actor}#0", stream=actor, op="create")
        graph.add_node(f"{principal}#1", stream=principal, op="spawn")
        graph.add_edge(f"{principal}#1", f"{root}#0", key="spawn")
        for position, descendant in enumerate((child, python), 1):
            source = f"{root}#{position}"
            graph.add_node(source, stream=root, op="spawn")
            graph.add_edge(source, f"{descendant}#0", key="spawn")
        server = ServerRef(host, "", 0, 0, "alan")
        sessions = [
            Session(SessionRef(server, actor), actor, 1, 0, 0, 1, "alan", "",
                    "/work", kind, "waiting")
            for actor, kind in ((root, "codex"), (child, "claude"),
                                (python, "python"))
        ]
        fleet = Fleet()
        fleet.sessions = {host: sessions}
        fleet.unavailable = set()
        fleet.observed = fleet.view_revision = 1
        fleet._composed = (fleet.observed, graph)
        fleet.muster_generation = ("fixture", 1, 1, "$1")
        return fleet, root, child, python

    def test_proc_start_time_handles_a_process_name_with_spaces(self):
        stat = "123 (tmux: server) S " + " ".join(
            str(number) for number in range(4, 53))
        self.assertEqual(proc.start_time(123, stat), 22)

    def test_identical_tmux_ids_on_different_hosts_are_distinct(self):
        self.assertNotEqual(self.session("newton").ref, self.session("lovelace").ref)

    def test_tmux_wrapper_forces_utf8_for_remote_clients(self):
        wrapper = (Path(__file__).parents[1] / "fleet-tmux").read_text()
        self.assertIn('exec /usr/bin/tmux -N -u "$@"', wrapper)

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
                 "from agent_fleet.tmux import event_stream; "
                 "print(next(event_stream('fixture'))[2])"],
                text=True, capture_output=True, env=environment)
            socket_path = root / "tmux" / f"tmux-{os.getuid()}" / "default"
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), "False")
            self.assertFalse(socket_path.exists())

    def muster_state(self, matches, count=None):
        state = json.dumps({"matches": matches,
                            "matchCount": len(matches) if count is None else count})
        return subprocess.CompletedProcess([], 0, stdout=state)

    def test_cursor_position_comes_from_musters_loaded_identities(self):
        with mock.patch.object(hot, "active_main", return_value="actor:focused"), \
                mock.patch.object(hot, "fetch",
                                  return_value="pos(2)\n") as fetch:
            self.assertEqual(hot.cursor(), "pos(2)")
        fetch.assert_called_once_with("cursor actor:focused")

    def test_cursor_falls_back_to_the_daemons_first_waiting_row(self):
        with mock.patch.object(hot, "active_main", return_value=""), \
                mock.patch.object(hot, "fetch",
                                  return_value="pos(1)\n") as fetch:
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

    def test_cursor_omits_a_missing_daemon_position(self):
        with mock.patch.object(hot, "active_main", return_value="actor:first"), \
                mock.patch.object(hot, "fetch", return_value=""):
            self.assertEqual(hot.cursor(), "")

    def test_daemon_cursor_returns_a_position_in_its_projected_order(self):
        fleet = Fleet()
        sessions = [mock.Mock(ref=mock.Mock(key="actor:first"), state="waiting"),
                    mock.Mock(ref=mock.Mock(key="actor:focused"), state="working")]
        projected = [mock.Mock(session=session) for session in sessions]

        async def position(request):
            reader = asyncio.StreamReader()
            reader.feed_data((request + "\n").encode())
            reader.feed_eof()
            writer = mock.Mock()
            writer.drain = mock.AsyncMock()
            with mock.patch.object(fleet, "projected", return_value=projected), \
                    mock.patch.object(fleet, "schedule_refresh") as refresh:
                await fleet.reply(reader, writer)
            return writer.write.call_args.args[0], refresh.call_count

        self.assertEqual(asyncio.run(position("cursor actor:focused")),
                         (b"pos(2)\n", 1))
        self.assertEqual(asyncio.run(position("cursor")), (b"pos(1)\n", 1))

    def test_machine_labels_are_single_cell_and_noether_uses_ligature(self):
        self.assertEqual([machine(host) for host in
                          ("newton", "lovelace", "boltzmann", "turing", "noether")],
                         ["N", "L", "B", "T", "Œ"])

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

    def test_host_observation_decodes_sessions_and_graph_from_one_json_parse(self):
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = []
        raw = encode([self.session("lovelace")], {}, [], graph)
        with mock.patch("agent_fleet.protocol.json.loads",
                        wraps=json.loads) as loads:
            sessions, usage, unavailable, decoded = decode_observation(raw)
        loads.assert_called_once_with(raw)
        self.assertEqual(len(sessions), 1)
        self.assertEqual((usage, unavailable), ({}, []))
        self.assertEqual(decoded.graph["actors"], [])

    def test_composed_graph_recomposes_only_per_observation_generation(self):
        graph = nx.MultiDiGraph()
        graph.graph["actors"] = [{"addr": "a@h", "kind": "codex"}]
        fleet = Fleet()
        fleet.graphs = {"lovelace": graph}
        fleet.observed = 1
        first = fleet.composed_graph()
        self.assertIs(fleet.composed_graph(), first)
        fleet.graphs["lovelace"] = graph
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
                                   return_value=[]):
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

    def test_daemon_switch_reply_preserves_the_host_control_error(self):
        fleet = Fleet()
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()

        async def exercise():
            reader = asyncio.StreamReader()
            reader.feed_data(b'switch {"key":"source","client":"/dev/pts/8"}\n')
            reader.feed_eof()
            with mock.patch.object(
                    fleet, "switch", mock.AsyncMock(
                        side_effect=RuntimeError("identity changed"))):
                await fleet.reply(reader, writer)

        asyncio.run(exercise())
        self.assertEqual(json.loads(writer.write.call_args.args[0]),
                         {"error": "identity changed"})

    def test_alan_switch_carries_the_selected_presentation_descriptor(self):
        host = "lovelace"
        actor = f"llm-one@{host}"
        session = Session(
            SessionRef(ServerRef(host, "", 0, 0, "alan"), actor),
            "fixture", 1, 0, 0, 1, "alan", "", "/work", "llm")
        fleet = Fleet()
        fleet.sessions = {host: [session]}
        fleet.unavailable.clear()
        process = mock.Mock()
        process.stdin.drain = mock.AsyncMock()
        fleet.processes = {host: process}

        async def exercise():
            pending = asyncio.create_task(
                fleet.switch(session.ref.key, "/dev/pts/8"))
            await asyncio.sleep(0)
            request = json.loads(process.stdin.write.call_args.args[0])
            self.assertEqual(request, {
                "switch": 1, "client": "/dev/pts/8", "actor": actor,
                "agent": "llm", "cwd": "/work",
            })
            fleet.host_reply({"switch": 1, "target": ["/tmp/tmux", 12, 10, "$1"],
                              "duration": .001})
            self.assertEqual(await pending,
                             (("/tmp/tmux", 12, 10, "$1"), .001))

        asyncio.run(exercise())

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

        fleet.unavailable.clear()
        fleet.tmux_unavailable = {"lovelace"}
        with self.assertRaisesRegex(RuntimeError, "tmux server is unavailable"):
            asyncio.run(fleet.preview(self.session("lovelace").ref.key))

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
                            reader = connection.makefile("rb")
                            request = json.loads(reader.readline())
                            if request.get("stream"):
                                initial = observed(False)
                                initial.update({"generation": 1, "revision": 0})
                                connection.sendall((json.dumps({
                                    "ok": True, "observation": {
                                        "kind": "replace", "graph": initial,
                                    },
                                }) + "\n").encode())
                                reader.readline()
                                emit_update.wait()
                                active = observed(True)
                                connection.sendall((json.dumps({
                                    "ok": True, "observation": {
                                        "kind": "delta", "generation": 1,
                                        "revision": 1,
                                        "actors": active["graph"]["actors"],
                                        "nodes": active["nodes"][3:], "edges": [],
                                    },
                                }) + "\n").encode())
                                reader.readline()
                            else:
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
            subprocess.run([
                "tmux", "new-session", "-d", "-s",
                "fleet@alan-" + alan.runtime_name(addr), "sleep", "30",
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
                        await fleet.refresh_muster()
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
        self.assertEqual(projected[0].transcript_id, identity)
        self.assertEqual(projected[0].transcript_path, "")
        self.assertEqual(projected[0].evaluation, f"{codex}#2")
        self.assertEqual(projected[0].evaluation_started, 3)
        self.assertEqual(projected[1].transcript_id, "")
        self.assertEqual(projected[1].transcript_path, "")

    def test_host_inventory_projects_only_actors_with_current_presentations(self):
        with tempfile.TemporaryDirectory() as cwd:
            actors = []
            for addr, kind in [
                ("codex-full@newton", "codex"),
                ("claude-full@newton", "claude"),
                ("codex-read-reviewer@newton", "codex"),
                ("python-one@newton", "python"),
                ("llm-one@newton", "llm"),
            ]:
                actors.append({
                    "addr": addr, "kind": kind, "state": "waiting",
                    "cwd": cwd, "created": 1, "human_activity": 0,
                    "label": "same human label", "active_evaluation": None,
                    "evaluation_started": 0,
                })
            native = ["codex-full@newton", "claude-full@newton"]
            items = [mock.Mock(
                session_name="fleet@alan-" + alan.runtime_name(actor),
                session_id=f"${number}",
            ) for number, actor in enumerate(native, 1)]
            server = mock.Mock(sessions=items)
            server.cmd.return_value.stdout = []
            native = Path(cwd) / "native"
            native.mkdir()
            (native / "kernel.json").touch()
            with mock.patch("agent_fleet.tmux.server", return_value=server), \
                 mock.patch("agent_fleet.presentation.alan.native_dir",
                            return_value=native):
                projected = tmux.inventory("newton", actors)
        self.assertEqual(
            [session.ref.session_id for session in projected],
            ["codex-full@newton", "claude-full@newton",
             "python-one@newton", "llm-one@newton"],
        )
        self.assertNotIn("codex-read-reviewer@newton",
                         [session.ref.session_id for session in projected])

    def test_every_rendered_actor_resolves_from_the_same_host_generation(self):
        host = os.uname().nodename
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {**without_tmux_client(), "TMUX_TMPDIR": str(root),
                           "TERM": "xterm-256color"}
            cwd = root / "work"
            cwd.mkdir()
            specifications = [
                (f"codex-full@{host}", "codex"),
                (f"claude-full@{host}", "claude"),
                (f"codex-read-reviewer@{host}", "codex"),
                (f"claude-disappeared@{host}", "claude"),
                (f"python-one@{host}", "python"),
                (f"llm-one@{host}", "llm"),
            ]
            descriptors = [{
                "addr": addr, "kind": kind, "state": "waiting",
                "cwd": str(cwd), "created": 1, "human_activity": 0,
                "label": "colliding label", "active_evaluation": None,
                "evaluation_started": 0,
            } for addr, kind in specifications]
            native = [specifications[0][0], specifications[1][0],
                      specifications[4][0]]
            try:
                for actor in native:
                    subprocess.run([
                        "tmux", "new-session", "-d", "-s",
                        "fleet@alan-" + alan.runtime_name(actor), "sleep", "30",
                    ], check=True, env=environment)

                graph = nx.MultiDiGraph()
                principal = f"will@{host}"
                graph.graph["actors"] = [
                    {"addr": principal, "kind": "principal"}, *descriptors]
                for number, descriptor in enumerate(descriptors):
                    source = f"{principal}#{number}"
                    target = f'{descriptor["addr"]}#0'
                    graph.add_node(source, stream=principal)
                    graph.add_node(target, stream=descriptor["addr"])
                    graph.add_edge(source, target, key="spawn")

                with mock.patch.dict(os.environ, environment, clear=True):
                    sessions = tmux.inventory(host, descriptors)
                    projected_graph = alan.projection_graph(graph)
                    projected = render.order(
                        sessions, [], projected_graph, show_python=True)
                    rows = render.rows_text(projected, [], 120)
                    emitted = [item.session for item in projected]
                    emitted_by_key = {session.ref.key: session for session in emitted}
                    self.assertTrue(all(session.ref.key in rows for session in emitted))
                    excluded = {specifications[2][0], specifications[3][0]}
                    self.assertTrue(excluded.isdisjoint(
                        session.ref.session_id for session in emitted))
                    graph_actors = {
                        actor["addr"] for actor in projected_graph.graph["actors"]}
                    self.assertTrue(excluded <= graph_actors)

                    attachment = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
                    with mock.patch.object(
                            attachment, "find",
                            side_effect=lambda key: emitted_by_key[key]):
                        targets = [attachment.resolve(session.ref.key)
                                   for session in emitted]
                self.assertEqual(len(targets), 4)
                self.assertEqual(len({target[1:3] for target in targets}), 1)
                self.assertTrue(all(target[0] == targets[0][0] for target in targets))
            finally:
                subprocess.run(["tmux", "kill-server"], env=environment,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

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

    def test_fzf_open_adapter_sends_one_exact_timed_socket_request(self):
        root = Path(__file__).parents[1]
        key = "newton:/tmp/tmux-1000/default:12:10:$1"
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            socket_dir = runtime / "agent-fleet"
            socket_dir.mkdir()
            path = socket_dir / "viewer-main.sock"
            received = []
            ready = threading.Event()

            def answer():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(path)); server.listen(); ready.set()
                    connection, _ = server.accept()
                    with connection:
                        received.append(connection.makefile().readline().strip())
                        connection.sendall(b"OK\n")

            thread = threading.Thread(target=answer); thread.start(); ready.wait(1)
            result = subprocess.run([root / "fleet-open", "main", key],
                                    env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
                                    text=True, capture_output=True)
            thread.join(1)
        self.assertEqual(result.returncode, 0)
        operation, actual, selected = received[0].split(" ")
        self.assertEqual((operation, actual), ("OPEN", key))
        self.assertGreater(float(selected), 0)

    def test_fzf_project_adapter_sends_one_exact_timed_socket_request(self):
        root = Path(__file__).parents[1]
        key = "newton:/tmp/tmux-1000/default:12:10:$1"
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            socket_dir = runtime / "agent-fleet"
            socket_dir.mkdir()
            path = socket_dir / "viewer-main.sock"
            received = []
            ready = threading.Event()

            def answer():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(path)); server.listen(); ready.set()
                    connection, _ = server.accept()
                    with connection:
                        received.append(connection.makefile().readline().strip())
                        connection.sendall(b"OK\n")

            thread = threading.Thread(target=answer); thread.start(); ready.wait(1)
            result = subprocess.run([root / "fleet-open", "project", "main", key],
                                    env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
                                    text=True, capture_output=True)
            thread.join(1)
        self.assertEqual(result.returncode, 0)
        operation, actual, selected = received[0].split(" ")
        self.assertEqual((operation, actual), ("PROJECT", key))
        self.assertGreater(float(selected), 0)

    def test_fzf_focus_adapter_focuses_without_a_source(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            socket_dir = runtime / "agent-fleet"
            socket_dir.mkdir()
            path = socket_dir / "viewer-main.sock"
            received = []
            ready = threading.Event()

            def answer():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(path)); server.listen(); ready.set()
                    connection, _ = server.accept()
                    with connection:
                        received.append(connection.makefile().readline().strip())
                        connection.sendall(b"OK\n")

            thread = threading.Thread(target=answer); thread.start(); ready.wait(1)
            key = "newton:/tmp/tmux-1000/default:12:10:$1"
            result = subprocess.run([root / "fleet-open", "focus", "main", key],
                                    env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
                                    text=True, capture_output=True)
            thread.join(1)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(received, [f"FOCUS {key}"])

    def test_public_show_api_places_an_explicit_named_slot(self):
        with mock.patch.object(viewer, "slots", return_value=[("main", "old")]), \
             mock.patch.object(viewer, "request") as request:
            viewer.show("source", slot="left")
        request.assert_called_once_with("left", "source")

    def test_repeated_local_open_uses_only_atomic_switch(self):
        session = self.session(os.uname().nodename)
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        state.source = "old"
        state.host = os.uname().nodename.split(".", 1)[0]
        entry = mock.Mock(client="/dev/pts/8", source="old")
        state.attachments[state.host] = entry
        with mock.patch.object(state, "resident_switch") as switch, \
             mock.patch.object(state, "select_host") as select, \
             mock.patch.object(state, "create_host") as create:
            state.open(session.ref.key)
        switch.assert_called_once_with(session.ref.key, "/dev/pts/8")
        select.assert_not_called(); create.assert_not_called()
        self.assertEqual(entry.source, session.ref.key)

    def test_initial_local_attachment_does_not_request_nested_tmux(self):
        ui = mock.Mock()
        ui.command.side_effect = [["@2"], ["/dev/pts/8"]]
        state = viewer.Attachment("main", "/dev/pts/9", ui)
        with mock.patch.object(state, "resolve", return_value=("/tmp/tmux", 12, 10, "$1")), \
             mock.patch.object(state, "prove_switch"):
            entry = state.create_host(os.uname().nodename.split(".", 1)[0], "source")
        command = ui.command.call_args_list[0].args[0][-1]
        self.assertIn("env -u TMUX -u TMUX_PANE", command)
        self.assertEqual((entry.window, entry.client), ("@2", "/dev/pts/8"))

    def test_cold_attachment_retries_only_until_tmux_registers_its_client(self):
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        with mock.patch.object(
                state, "resident_switch",
                side_effect=[RuntimeError("can't find client: /dev/pts/8"), ("target",)]) as switch, \
             mock.patch.object(viewer.time, "sleep"):
            self.assertEqual(state.prove_switch("source", "/dev/pts/8"), ("target",))
        self.assertEqual(switch.call_count, 2)
        with mock.patch.object(state, "resident_switch",
                               side_effect=RuntimeError("stale source identity")):
            with self.assertRaisesRegex(RuntimeError, "stale"):
                state.prove_switch("source", "/dev/pts/8")

    def test_switch_protocol_preserves_the_daemon_error(self):
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        with mock.patch.object(state, "daemon", return_value='{"error":"identity changed"}'):
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                state.resident_switch("source", "/dev/pts/8")

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

    def test_failed_cross_host_commit_rolls_back_inactive_attachment(self):
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        state.source = "old-source"
        state.host = "old-host"
        state.attachments = {
            "old-host": mock.Mock(source="old-source", client="/dev/pts/1"),
            "new-host": mock.Mock(source="prior-new", client="/dev/pts/2")}
        with mock.patch.object(viewer, "SOURCE_HOSTS", frozenset({"new-host"})), \
             mock.patch.object(state, "resident_switch") as switch, \
             mock.patch.object(state, "select_host", side_effect=RuntimeError("UI failed")):
            with self.assertRaisesRegex(RuntimeError, "UI failed"):
                state.open("new-host:/tmp/tmux/default:12:10:$1")
        self.assertEqual(switch.call_args_list, [
            mock.call("new-host:/tmp/tmux/default:12:10:$1", "/dev/pts/2"),
            mock.call("prior-new", "/dev/pts/2")])
        self.assertEqual((state.source, state.host), ("old-source", "old-host"))

    def test_failed_rollback_removes_only_the_inactive_attachment(self):
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        state.source = "old-source"
        state.host = "old-host"
        state.attachments = {
            "old-host": mock.Mock(source="old-source", client="/dev/pts/1"),
            "new-host": mock.Mock(source="prior-new", client="/dev/pts/2")}
        with mock.patch.object(viewer, "SOURCE_HOSTS", frozenset({"new-host"})), \
             mock.patch.object(
                state, "resident_switch",
                side_effect=[None, RuntimeError("rollback failed")]), \
             mock.patch.object(state, "select_host", side_effect=RuntimeError("UI failed")), \
             mock.patch.object(state, "remove_host") as remove:
            with self.assertRaisesRegex(RuntimeError, "UI failed"):
                state.open("new-host:/tmp/tmux/default:12:10:$1")
        remove.assert_called_once_with("new-host", "rollback_failed")
        self.assertEqual((state.source, state.host), ("old-source", "old-host"))

    def test_cross_host_open_retains_both_host_windows(self):
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        old = mock.Mock(source="old-source", client="/dev/pts/1", window="@1")
        new = mock.Mock(source="prior-new", client="/dev/pts/2", window="@2")
        state.attachments = {"old-host": old, "new-host": new}
        state.source = "old-source"; state.host = "old-host"
        with mock.patch.object(viewer, "SOURCE_HOSTS", frozenset({"new-host"})), \
             mock.patch.object(state, "resident_switch"), \
             mock.patch.object(state, "select_host"):
            state.open("new-host:/tmp/tmux/default:12:10:$1")
        self.assertEqual(set(state.attachments), {"old-host", "new-host"})
        self.assertEqual((state.host, state.source),
                         ("new-host", "new-host:/tmp/tmux/default:12:10:$1"))

    def test_real_ui_windows_survive_display_detach_and_cross_host_selection(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "TMUX_TMPDIR": directory,
                           "TERM": "xterm-256color"}
            environment.pop("TMUX", None); environment.pop("TMUX_PANE", None)
            for source in ("lovelace-source", "newton-source"):
                subprocess.run(["tmux", "new-session", "-d", "-s", source,
                                "sleep 30"], check=True, env=environment)
            subprocess.run(["tmux", "-L", "agent-fleet-ui", "new-session", "-d",
                            "-s", "fleet@test", "sleep 30"], check=True, env=environment)
            control_process = subprocess.Popen(
                ["tmux", "-L", "agent-fleet-ui", "-C", "attach-session",
                 "-t", "fleet@test"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment)
            control = tmux.ControlClient(control_process, queue.Queue())
            control.command(["refresh-client", "-f", "no-output"])
            state = viewer.Attachment("test", "/dev/pts/9", control)
            display = None
            try:
                entries = {}
                for host, source in (("lovelace", "lovelace-source"),
                                     ("newton", "newton-source")):
                    command = shlex.join(
                        ["env", "-u", "TMUX", "-u", "TMUX_PANE", "tmux", "-N",
                         "attach-session", "-t", source])
                    window = control.command(
                        ["new-window", "-d", "-P", "-F", "#{window_id}",
                         "-t", "fleet@test", "-n", host, command])[0]
                    entries[host] = mock.Mock(
                        window=window,
                        client=state.ui_value(window, "#{pane_tty}"),
                        source=f"{host}:/tmp/tmux/default:1:1:$1",
                        remote_file=None, master=None)
                state.attachments = entries
                state.host = "lovelace"; state.source = entries["lovelace"].source
                identities = {
                    host: (entry.window,
                           state.ui_value(entry.window, "#{pane_pid}"), entry.client)
                    for host, entry in entries.items()}
                with mock.patch.object(state, "resident_switch"):
                    state.open(entries["newton"].source)
                    state.open(entries["lovelace"].source)
                self.assertEqual(
                    {host: (entry.window,
                            state.ui_value(entry.window, "#{pane_pid}"), entry.client)
                     for host, entry in entries.items()}, identities)

                master, slave = pty.openpty()
                display = subprocess.Popen(
                    ["tmux", "-L", "agent-fleet-ui", "attach-session",
                     "-t", "fleet@test"], stdin=slave, stdout=slave, stderr=slave,
                    env=environment, start_new_session=True)
                os.close(slave)
                for _ in range(100):
                    clients = control.command(
                        ["list-clients", "-t", "fleet@test", "-F",
                         "#{client_name} #{client_control_mode}"])
                    if any(line.endswith(" 0") for line in clients):
                        break
                    time.sleep(.01)
                display_clients = [line.rsplit(" ", 1)[0] for line in clients
                                   if line.endswith(" 0")]
                self.assertEqual(len(display_clients), 1)
                control.command(["detach-client", "-t", display_clients[0]])
                display.wait(timeout=2); os.close(master); display = None
                self.assertIsNone(control_process.poll())
                self.assertEqual(set(state.attachments), {"lovelace", "newton"})
                self.assertEqual(state.source, entries["lovelace"].source)
            finally:
                if display and display.poll() is None:
                    display.terminate(); display.wait()
                control_process.terminate(); control_process.wait()
                subprocess.run(["tmux", "-L", "agent-fleet-ui", "kill-server"],
                               env=environment, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                subprocess.run(["tmux", "kill-server"], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_focus_failure_does_not_turn_a_completed_open_into_failure(self):
        state = mock.Mock()
        with mock.patch.object(viewer, "focus", side_effect=OSError("no display")), \
             mock.patch.object(viewer, "viewer_error") as report:
            error = viewer.activate(state, "side", "source", 12.0)
        state.open.assert_called_once_with("source", 12.0)
        self.assertEqual(str(error), "no display")
        self.assertEqual((error.stage, error.cause), ("focus", "workstation"))
        report.assert_not_called()

    def test_focus_requires_the_selected_source_to_be_projected(self):
        state = mock.Mock(source="source-a", workstation="boltzmann")
        with mock.patch.object(viewer, "focus") as focus:
            with self.assertRaisesRegex(RuntimeError, "source-a, not source-b"):
                viewer.focus_projected(state, "main", "source-b")
            focus.assert_not_called()
            viewer.focus_projected(state, "main", "source-a")
        focus.assert_called_once_with("main", "boltzmann")

    def test_project_skips_an_already_projected_source(self):
        state = mock.Mock(source="source-a")
        viewer.project(state, "source-a", 12.0)
        state.open.assert_not_called()
        viewer.project(state, "source-b", 13.0)
        state.open.assert_called_once_with("source-b", 13.0)

    def test_dead_attachment_clears_the_advertised_source_without_an_open(self):
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        state.source = "source"
        state.host = "newton"
        state.attachments["newton"] = mock.Mock(
            window="@2", remote_file="/run/user/1000/agent-fleet/viewer.tty",
            owner="lovelace", master=None)
        with mock.patch.object(state, "ui_value", return_value="1"), \
             mock.patch.object(state, "reclaim_marker") as reclaim, \
             mock.patch.object(state, "ssh") as ssh:
            error = state.check()
        self.assertEqual(state.source, "")
        self.assertEqual(error, "Viewer attachment exited unexpectedly")
        self.assertNotIn("newton", state.attachments)
        reclaim.assert_called_once_with("newton", "lovelace")
        ssh.assert_not_called()

    def test_dead_remote_master_removes_only_that_host_without_starting_ssh(self):
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        state.host = "lovelace"
        state.source = "local-source"
        state.attachments = {
            "lovelace": mock.Mock(window="@1", remote_file=None, master=None),
            "newton": mock.Mock(window="@2", remote_file="marker",
                                 owner="lovelace", master=(82, 10))}
        with mock.patch.object(viewer, "process_alive", return_value=False), \
             mock.patch.object(state, "ui_value", return_value="0"), \
             mock.patch.object(state, "reclaim_marker",
                               side_effect=RuntimeError(
                                   "newton is disconnected; refusing cleanup")), \
             mock.patch.object(state, "ssh") as ssh:
            self.assertEqual(state.check(), "")
        self.assertEqual(set(state.attachments), {"lovelace"})
        self.assertEqual((state.host, state.source), ("lovelace", "local-source"))
        ssh.assert_not_called()

    def test_repeated_remote_open_retains_master_and_interactive_client(self):
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        state.source = "remote:/tmp/tmux/default:12:10:$1"
        state.host = "remote"
        entry = mock.Mock(source=state.source, client="/dev/pts/8", window="@2")
        state.attachments["remote"] = entry
        with mock.patch.object(viewer, "SOURCE_HOSTS", frozenset({"remote"})), \
             mock.patch.object(state, "resident_switch") as switch, \
             mock.patch.object(state, "create_host") as create, \
             mock.patch.object(state, "select_host") as select:
            state.open("remote:/tmp/tmux/default:12:10:$2")
        switch.assert_called_once_with("remote:/tmp/tmux/default:12:10:$2", "/dev/pts/8")
        create.assert_not_called(); select.assert_not_called()
        self.assertIs(state.attachments["remote"], entry)

    def test_remote_start_retries_until_the_allocated_client_is_registered(self):
        ui = mock.Mock()
        ui.command.side_effect = [["@2"]]
        state = viewer.Attachment("main", "/dev/pts/9", ui)
        empty = mock.Mock(stdout="", returncode=0)
        tty = mock.Mock(stdout="/dev/pts/8\n", returncode=0)
        with mock.patch.object(state, "ensure_master", return_value=(82, 10)), \
             mock.patch.object(state, "resolve",
                               return_value=("/tmp/tmux", 12, 10, "$1")), \
             mock.patch.object(state, "ssh",
                               side_effect=[empty, tty]) as ssh, \
             mock.patch.object(state, "reclaim_marker") as reclaim, \
             mock.patch.object(state, "prove_switch") as switch:
            result = state.create_host("newton", "newton:/tmp/tmux:12:10:$1")
        self.assertEqual((result.window, result.client), ("@2", "/dev/pts/8"))
        switch.assert_called_once_with("newton:/tmp/tmux:12:10:$1", "/dev/pts/8")
        reclaim.assert_called_once_with("newton", os.uname().nodename.split(".", 1)[0])
        self.assertEqual(len(ssh.call_args_list), 2)

    def test_non_hub_actor_lookup_reuses_one_forward_to_the_fleet_daemon(self):
        state = viewer.Attachment("side", "/dev/pts/9", mock.Mock())
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
        state = viewer.Attachment("side", "/dev/pts/9", mock.Mock())
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
        state = viewer.Attachment("side", "/dev/pts/9", mock.Mock())
        policy = mock.Mock(stdout="controlmaster no\ncontrolpath none\ncontrolpersist no\n")
        with mock.patch.object(viewer.subprocess, "run", return_value=policy):
            with self.assertRaisesRegex(RuntimeError, "ControlMaster"):
                state.master_policy("newton")

    def test_daemon_forward_recovery_cancels_only_the_exact_slot_rule(self):
        state = viewer.Attachment("left", "/dev/pts/9", mock.Mock())
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
        with mock.patch("agent_fleet.viewer.workstation.request") as request:
            viewer.focus("main", "boltzmann")
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
        request.assert_not_called()

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
            host, {"operation": "archive-alan", "actor": f"claude-1@{host}",
                   "agent": "claude"})
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
            host, {"operation": "archive-alan", "actor": f"llm-1@{host}",
                   "agent": "llm"})

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
        key = f"{os.uname().nodename}:/tmp/tmux:12:10:$1"
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
        with mock.patch("agent_fleet.actions.history_projection",
                        return_value=json.dumps(context["history"])):
            rows = actions.history()
        self.assertEqual(rows, [
            ("alan:codex-1@lovelace", "lovelace", "codex", "work", "/work")])

    def test_history_keeps_retired_bare_language_actor_without_native_identity(self):
        context = {"history": [{
            "key": "alan:llm-1@lovelace", "host": "lovelace",
            "agent": "llm", "name": "review", "cwd": "/work", "mtime": 20}]}
        with mock.patch("agent_fleet.actions.history_projection",
                        return_value=json.dumps(context["history"])):
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
        request.assert_not_called()

    def test_retained_unavailable_actor_remains_the_native_history_authority(self):
        context = {"history": [{
            "key": "alan:codex-1@lovelace", "host": "lovelace",
            "agent": "codex", "name": "work", "cwd": "/work", "mtime": 20}]}
        with mock.patch("agent_fleet.actions.history_projection",
                        return_value=json.dumps(context["history"])):
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
        self.assertNotIn("unlink-window", source)
        self.assertEqual(source.count('"kill-window"'), 1)
        self.assertIn('self.ui.command(["kill-window"',
                      (root / "agent_fleet/viewer.py").read_text())

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
        self.assertIn("exec env -u TMUX -u TMUX_PANE python", main)
        self.assertIn("/usr/bin/tmux -L agent-fleet-ui", main)
        self.assertIn(
            "exec /usr/bin/tmux -N -L agent-fleet-ui -u attach-session "
            "-t fleet@main", main)
        self.assertIn("set-option -t fleet@main prefix None", main)
        self.assertIn("set-option -t fleet@main status off", main)
        self.assertIn("set-option -t fleet@main mouse on", main)
        self.assertIn("#{==:#{client_control_mode},0}", main)
        self.assertNotIn("attach-session -d", main)
        self.assertIn("--destroy", main)
        self.assertIn('ui.command(["refresh-client", "-f", "no-output"])',
                      (root / "agent_fleet/viewer.py").read_text())
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
            subprocess.run(["tmux", "new-session", "-d", "-s", "source",
                            "sleep 30"], check=True, env=environment)
            subprocess.run(
                ["tmux", "-L", "agent-fleet-ui", "new-session", "-d",
                 "-s", "fleet@main",
                 f"exec {sys.executable} -c 'from agent_fleet.viewer import serve; "
                 "serve(\"main\")'"], check=True, env=environment)
            try:
                subprocess.run(["tmux", "-L", "agent-fleet-ui", "set-option",
                                "-t", "fleet@main",
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
                    ["tmux", "-L", "agent-fleet-ui", "show-option",
                     "-t", "fleet@main", "-v", "status"],
                    check=True, text=True, capture_output=True,
                    env=environment).stdout.strip()
                self.assertEqual(status, "off")
                source_sessions = subprocess.run(
                    ["tmux", "list-sessions", "-F", "#{session_name}"],
                    check=True, text=True, capture_output=True,
                    env=environment).stdout.splitlines()
                self.assertEqual(source_sessions, ["source"])
            finally:
                subprocess.run(["tmux", "-L", "agent-fleet-ui", "kill-server"],
                               env=environment)
                subprocess.run(["tmux", "kill-server"], env=environment)

    def test_main_viewer_creates_its_dedicated_tmux_server(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "TMUX_TMPDIR": str(Path(directory) / "tmux"),
                           "XDG_RUNTIME_DIR": str(Path(directory) / "runtime"),
                           "PYTHONPATH": str(root)}
            (Path(directory) / "tmux").mkdir()
            (Path(directory) / "runtime").mkdir()
            master, slave = pty.openpty()
            process = subprocess.Popen([root / "fleet-viewer", "main"],
                                       stdin=slave, stdout=slave, stderr=slave,
                                       env=environment, start_new_session=True)
            os.close(slave)
            try:
                socket = Path(directory) / "runtime/agent-fleet/viewer-main.sock"
                for _ in range(100):
                    if socket.exists():
                        break
                    time.sleep(.01)
                self.assertTrue(socket.exists())
                sessions = subprocess.run(
                    ["tmux", "-L", "agent-fleet-ui", "list-sessions", "-F",
                     "#{session_name}"], check=True, text=True, capture_output=True,
                    env=environment).stdout.splitlines()
                self.assertEqual(sessions, ["fleet@main"])
            finally:
                os.killpg(process.pid, signal.SIGHUP)
                process.wait()
                os.close(master)
                subprocess.run(["tmux", "-L", "agent-fleet-ui", "kill-server"],
                               env=environment)

    def test_muster_always_opens_the_global_main_viewer(self):
        source = (Path(__file__).parents[1] / "agent_fleet/ui.py").read_text()
        self.assertIn("focus:execute-silent(exec /usr/lib/agent-fleet/fleet-open project main {1})", source)
        self.assertIn("enter:execute-silent(exec /usr/lib/agent-fleet/fleet-open focus main {1})", source)
        self.assertNotIn("/usr/lib/agent-fleet/ui show", source)
        self.assertIn("load:transform(/usr/lib/agent-fleet/ui cursor)+unbind(load)", source)
        self.assertIn('"--no-sort"', source)
        self.assertIn("enable-search+toggle-sort", source)
        self.assertNotIn('"--nth=2.."', source)
        self.assertIn("change-prompt(Search: )", source)
        self.assertIn("c:execute-silent(/usr/lib/agent-fleet/ui create-tab)", source)
        self.assertIn("r:execute-silent(/usr/lib/agent-fleet/ui rename-tab {1})", source)
        self.assertIn("f\"--bind=l:transform({fold_open})\"", source)
        self.assertIn("f\"--bind=h:transform({fold_close})\"", source)
        self.assertIn("f\"--bind=right:transform({fold_open})\"", source)
        self.assertIn("f\"--bind=left:transform({fold_close})\"", source)
        self.assertIn("f\"--bind=p:transform({toggle_python})\"", source)
        self.assertIn("f\"--bind=resize:transform({resize})\"", source)
        self.assertIn("f\"--bind=x:transform({archive})\"", source)
        self.assertIn("f\"--bind=R:transform({refresh})\"", source)
        self.assertIn("/usr/bin/nc -U", source)
        self.assertNotIn("ui fold", source)
        self.assertNotIn("ui toggle", source)
        self.assertNotIn("ui archive", source)
        self.assertNotIn("ui refresh", source)
        self.assertIn('"--footer-border=bottom"', source)
        self.assertNotIn('"--preview=', source)
        self.assertNotIn('"--preview-window=', source)

    def test_muster_projects_recursive_folds_and_python_independently(self):
        fleet, root, language, python = self.fold_fleet()
        self.assertEqual([item.session.ref.session_id for item in fleet.projected()],
                         [root])
        fleet.expanded.add(root)
        fleet.view_revision += 1; fleet._view_cache = None
        self.assertEqual([item.session.ref.session_id for item in fleet.projected()],
                         [root, language])
        fleet.show_python = True
        fleet.view_revision += 1; fleet._view_cache = None
        self.assertEqual([item.session.ref.session_id for item in fleet.projected()],
                         [root, language, python])

    def test_host_observation_updates_availability_and_usage_before_projection(self):
        fleet = Fleet()
        host = "lovelace"
        fleet._view_cache = ("stale",)
        usage = {"claude": {"five_hour": {"utilization": 37}}}
        raw = encode([self.session(host)], usage)
        fleet.processes[host] = mock.Mock(pid=42)
        with mock.patch("agent_fleet.daemon.hosts", return_value=[host]):
            fleet.update_host(host, raw)
        self.assertNotIn(host, fleet.unavailable)
        self.assertEqual(fleet.usage, usage)
        self.assertIsNone(fleet._view_cache)
        self.assertNotIn("offline lovelace", fleet.view(100)[2])

    def test_hidden_orphan_python_does_not_break_the_projection(self):
        fleet, root, _, python = self.fold_fleet()
        graph = fleet._composed[1]
        for source, target, relation in list(graph.edges(keys=True)):
            if relation == "spawn" and graph.nodes[target]["stream"] == python:
                graph.remove_edge(source, target, relation)
        self.assertEqual([item.session.ref.session_id for item in fleet.projected()],
                         [root])
        fleet.show_python = True
        self.assertNotIn(python, [item.session.ref.session_id
                                  for item in fleet.projected()])

    def test_actor_without_current_principal_ancestry_stays_folded(self):
        fleet, root, child, _ = self.fold_fleet()
        graph = fleet._composed[1]
        for source, target, relation in list(graph.edges(keys=True)):
            if relation == "spawn" and graph.nodes[target]["stream"] == child:
                graph.remove_edge(source, target, relation)
        fleet.expanded.add(root)
        self.assertEqual([item.session.ref.session_id for item in fleet.projected()],
                         [root])

    def test_muster_socket_refuses_a_second_live_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "muster.sock"
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
                server.listen()
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    ui.prepare_socket(path)
                self.assertTrue(path.exists())

    def test_muster_socket_removes_only_a_stale_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "muster.sock"
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(path))
            ui.prepare_socket(path)
            self.assertFalse(path.exists())

    def test_fold_opens_and_closes_only_the_selected_expandable_actor(self):
        fleet, root, child, _ = self.fold_fleet()
        key = f"alan:{root}"
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("agent_fleet.daemon.RUNTIME", Path(directory)):
            revision = fleet.view_revision
            action = fleet.mutate_view(f"fold\topen\t{key}\t{revision}\t100")
            self.assertIn(root, fleet.expanded)
            self.assertEqual([item.session.ref.session_id for item in fleet.projected()],
                             [root, child])
            self.assertIn("reload-sync(/usr/bin/cat", action)
            self.assertTrue(action.startswith("transform-header(/usr/bin/cat"))
            self.assertNotIn("unbind(focus)", action)
            self.assertTrue(action.endswith(")"))
            self.assertEqual(action.count("/usr/bin/rm -f"), 2)
            fleet.mutate_view(
                f"fold\tclose\t{key}\t{fleet.view_revision}\t100")
            self.assertNotIn(root, fleet.expanded)

    def test_fold_accepts_an_exact_current_parent_across_an_unrelated_revision(self):
        fleet, root, _, _ = self.fold_fleet()
        displayed = fleet.view_revision
        raw = encode(fleet.sessions["lovelace"],
                     {"claude": {"five_hour": {"utilization": 1}}},
                     graph=fleet._composed[1])
        fleet.update_host("lovelace", raw)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("agent_fleet.daemon.RUNTIME", Path(directory)):
            action = fleet.mutate_view(
                f"fold\topen\talan:{root}\t{displayed}\t100")
        self.assertIn(root, fleet.expanded)
        self.assertNotIn("unbind(focus)", action)

    def test_fold_rejections_preserve_state_and_are_visible_at_fzf(self):
        for case in ("missing", "non-parent"):
            with self.subTest(case=case):
                fleet, root, child, _ = self.fold_fleet()
                if case == "missing":
                    key = "alan:missing@lovelace"
                    message = "session is not in the displayed view"
                else:
                    fleet.expanded.add(root)
                    fleet.view_revision += 1
                    fleet._view_cache = None
                    key = f"alan:{child}"
                    message = "fold requires an Alan parent with children"
                expanded = set(fleet.expanded)
                with tempfile.TemporaryDirectory() as directory:
                    directory = Path(directory)
                    runtime = directory / "runtime"
                    runtime.mkdir()
                    tmux_runtime = directory / "tmux"
                    tmux_runtime.mkdir()
                    socket_path = runtime / "fzf.sock"
                    environment = {**without_tmux_client(),
                                   "TMUX_TMPDIR": str(tmux_runtime)}
                    subprocess.run(
                        ["tmux", "new-session", "-d", "-s", "fleet@muster",
                         f"printf 'old\\n' | exec fzf --listen {socket_path} "
                         "--header initial"], check=True, env=environment)
                    try:
                        for _ in range(100):
                            if socket_path.exists():
                                break
                            time.sleep(.01)
                        with mock.patch("agent_fleet.daemon.RUNTIME", runtime):
                            action = fleet.mutate_view(
                                f"fold\topen\t{key}\t{fleet.view_revision}\t100")
                            self.assertNotIn("unbind(focus)", action)
                            subprocess.run(
                                ["curl", "-fsS", "--unix-socket", str(socket_path),
                                 "-XPOST", "-d", action, "http://localhost"],
                                check=True, stdout=subprocess.DEVNULL)
                        self.assertEqual(fleet.expanded, expanded)
                        for _ in range(100):
                            screen = subprocess.run(
                                ["tmux", "capture-pane", "-p",
                                 "-t", "=fleet@muster:"], check=True, text=True,
                                capture_output=True, env=environment).stdout
                            if f"Action failed: {message}" in screen:
                                break
                            time.sleep(.01)
                        self.assertIn(f"Action failed: {message}", screen)
                    finally:
                        subprocess.run(["tmux", "kill-server"], env=environment,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)

    def test_archive_transform_uses_one_action_and_one_coherent_redraw(self):
        fleet, root, _, _ = self.fold_fleet()
        key = f"alan:{root}"

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("agent_fleet.daemon.RUNTIME", Path(directory)), \
             mock.patch.object(fleet, "archive_authority", return_value=(
                 fleet.sessions["lovelace"][0], "lovelace", {"operation": "archive"})), \
             mock.patch.object(fleet, "viewers", return_value=[]), \
             mock.patch.object(fleet, "complete_archive", new_callable=mock.AsyncMock):
            result = asyncio.run(fleet.mutate_action(
                f"archive\t{key}\t{fleet.view_revision}\t100"))
        self.assertIn("reload-sync(/usr/bin/cat", result)
        self.assertEqual(fleet.projected(), [])
        self.assertIn(key, fleet.pending_archives)
        raw = encode(fleet.sessions["lovelace"], {}, graph=fleet._composed[1])
        fleet.update_host("lovelace", raw)
        self.assertEqual(fleet.projected(), [])

    def test_archive_accepts_an_exact_current_source_across_an_unrelated_revision(self):
        fleet, root, _, _ = self.fold_fleet()
        displayed = fleet.view_revision
        raw = encode(fleet.sessions["lovelace"],
                     {"claude": {"five_hour": {"utilization": 1}}},
                     graph=fleet._composed[1])
        fleet.update_host("lovelace", raw)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("agent_fleet.daemon.RUNTIME", Path(directory)), \
             mock.patch.object(fleet, "viewers", return_value=[]), \
             mock.patch.object(fleet, "complete_archive", new_callable=mock.AsyncMock):
            result = asyncio.run(fleet.mutate_action(
                f"archive\talan:{root}\t{displayed}\t100"))
        self.assertIn("transform-header", result)

    def test_unregistered_muster_rejects_view_mutation(self):
        fleet, _, _, _ = self.fold_fleet()
        fleet.muster_generation = None
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("agent_fleet.daemon.RUNTIME", Path(directory)):
            action = fleet.mutate_view("toggle\tpython\t100")
            self.assertFalse(fleet.show_python)
            self.assertIn("transform-header", action)
            [header] = Path(directory).glob("*.header")
            self.assertIn("Muster generation is not registered", header.read_text())

    def test_python_toggle_needs_no_selected_row(self):
        fleet, root, child, python = self.fold_fleet()
        fleet.sessions = {"lovelace": [session for session in fleet.sessions["lovelace"]
                                        if session.ref.session_id == python]}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("agent_fleet.daemon.RUNTIME", Path(directory)):
            self.assertEqual(fleet.projected(), [])
            fleet.mutate_view("toggle\tpython\t100")
            self.assertEqual([item.session.ref.session_id for item in fleet.projected()],
                             [python])

    def test_muster_generation_preserves_respawns_and_resets_replacements(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            runtime = directory / "runtime"
            runtime.mkdir()
            environment = {**without_tmux_client(),
                           "TMUX_TMPDIR": str(directory / "first")}
            Path(environment["TMUX_TMPDIR"]).mkdir()

            def start():
                subprocess.run(["tmux", "new-session", "-d", "-s", "fleet@muster",
                                "sleep 30"], check=True, env=environment)
                socket_path, pid, session_id = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", "=fleet@muster:",
                     "#{socket_path}\t#{pid}\t#{session_id}"],
                    check=True, text=True, capture_output=True,
                    env=environment).stdout.rstrip("\n").split("\t")
                started = proc.start_time(pid)
                return socket_path, int(pid), started, session_id

            fleet = Fleet()
            try:
                first = start()
                with mock.patch("agent_fleet.daemon.RUNTIME", runtime):
                    asyncio.run(fleet.register_muster(first, 100))
                    fleet.expanded.add("root")
                    fleet.show_python = True
                    asyncio.run(fleet.register_muster(first, 120))
                    self.assertEqual(fleet.expanded, {"root"})
                    self.assertTrue(fleet.show_python)
                    (runtime / "muster-view-orphan.rows").write_text("old")
                    subprocess.run(["tmux", "kill-server"], check=True,
                                   env=environment)
                    environment["TMUX_TMPDIR"] = str(directory / "second")
                    Path(environment["TMUX_TMPDIR"]).mkdir()
                    second = start()
                    self.assertNotEqual(first[1:3], second[1:3])
                    asyncio.run(fleet.register_muster(second, 100))
                    self.assertEqual(fleet.expanded, set())
                    self.assertFalse(fleet.show_python)
                    self.assertFalse((runtime / "muster-view-orphan.rows").exists())
            finally:
                subprocess.run(["tmux", "kill-server"], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_daemon_restart_registers_an_existing_muster_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            environment = {**without_tmux_client(),
                           "TMUX_TMPDIR": str(directory / "tmux")}
            Path(environment["TMUX_TMPDIR"]).mkdir()
            subprocess.run(["tmux", "new-session", "-d", "-s", "fleet@muster",
                            "sleep 30"], check=True, env=environment)
            fleet = Fleet()
            try:
                with mock.patch.dict(os.environ, environment, clear=True):
                    asyncio.run(fleet.register_existing_muster())
                self.assertIsNotNone(fleet.muster_generation)
                socket_path, pid, started, session_id = fleet.muster_generation
                self.assertEqual(started, proc.start_time(pid))
                self.assertTrue(Path(socket_path).is_socket())
                self.assertRegex(session_id, r"^\$[0-9]+$")
            finally:
                subprocess.run(["tmux", "kill-server"], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_resize_width_governs_the_next_observation_projection(self):
        fleet, _, _, _ = self.fold_fleet()
        graph = fleet._composed[1]
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("agent_fleet.daemon.RUNTIME", Path(directory)), \
             mock.patch("agent_fleet.daemon.render.rows_text",
                        wraps=render.rows_text) as rows:
            fleet.mutate_view("resize\t143")
            fleet.observed += 1
            fleet.view_revision += 1
            fleet._view_cache = None
            fleet._composed = (fleet.observed, graph)
            fleet.publish_view(fleet.view_width)
        self.assertEqual(rows.call_args.args[2], 143)

    def test_muster_launcher_registers_before_starting_live_fzf(self):
        source = (Path(__file__).parents[1] / "fleet-muster").read_text()
        inert = source.index("tmux new-session -d -s fleet@muster -n live 'exec sleep infinity'")
        register = source.index("/usr/lib/agent-fleet/ui register")
        live = source.index("tmux respawn-pane -k -t '=fleet@muster:live'")
        self.assertLess(inert, register)
        self.assertLess(register, live)

    def test_ui_register_sends_the_complete_text_generation(self):
        result = mock.Mock(stdout="/tmp/tmux.sock\t123\t$4\n")
        with mock.patch.object(ui.subprocess, "run", return_value=result) as run, \
             mock.patch.object(ui.proc, "start_time", return_value=456), \
             mock.patch.object(ui.shutil, "get_terminal_size",
                               return_value=os.terminal_size((120, 40))), \
             mock.patch.object(ui, "request", return_value="OK\n") as request:
            ui.register()
        run.assert_called_once()
        request.assert_called_once_with(
            "muster-register\t/tmp/tmux.sock\t123\t456\t$4\t120")

    def test_stock_fzf_consumes_one_revision_and_cleans_its_artifacts(self):
        fleet, _, _, _ = self.fold_fleet()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            runtime = directory / "runtime"
            runtime.mkdir()
            tmux_runtime = directory / "tmux"
            tmux_runtime.mkdir()
            muster_socket = runtime / "muster.sock"
            environment = {**without_tmux_client(),
                           "TMUX_TMPDIR": str(tmux_runtime)}
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", "fleet@muster",
                 f"printf 'old\\n' | exec fzf --listen {muster_socket}"],
                check=True, env=environment)
            try:
                for _ in range(100):
                    if muster_socket.exists():
                        break
                    time.sleep(.01)
                with mock.patch("agent_fleet.daemon.RUNTIME", runtime):
                    revision = fleet.view_revision
                    action, artifacts = fleet.publish_view(100)
                    self.assertLess(action.index("transform-header"),
                                    action.index("reload-sync"))
                    old_header = artifacts[1].read_text()
                    fleet.sessions["lovelace"].append(self.session("lovelace", "$99"))
                    fleet.view_revision += 1
                    fleet._view_cache = None
                    self.assertNotEqual(fleet.view(100)[2], old_header.rstrip("\n"))
                    subprocess.run(
                        ["curl", "-fsS", "--unix-socket", str(muster_socket),
                         "-XPOST", "-d", action, "http://localhost"],
                        check=True, stdout=subprocess.DEVNULL)
                for _ in range(100):
                    state = json.loads(subprocess.run(
                        ["curl", "-fsS", "--unix-socket", str(muster_socket),
                         "http://localhost?limit=1000"], check=True, text=True,
                        capture_output=True).stdout)
                    if (state["matches"] and
                            all("\t" in row["text"] for row in state["matches"]) and
                            all(not path.exists() for path in artifacts)):
                        break
                    time.sleep(.01)
                self.assertTrue(state["matches"])
                self.assertTrue(all(row["text"].split("\t")[1] == str(revision)
                                    for row in state["matches"]))
                screen = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", "=fleet@muster:"],
                    check=True, text=True, capture_output=True,
                    env=environment).stdout
                self.assertIn("1 total", screen)
                self.assertNotIn("2 total", screen)
                self.assertTrue(all(not path.exists() for path in artifacts))
            finally:
                subprocess.run(["tmux", "kill-server"], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_stock_fzf_x_and_refresh_send_the_displayed_identity_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            runtime = directory / "runtime"
            runtime.mkdir()
            daemon_socket = runtime / "fleet.sock"
            received = []

            def serve():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(daemon_socket))
                    server.listen()
                    while len(received) < 2:
                        connection, _ = server.accept()
                        with connection:
                            request = connection.makefile().readline().rstrip("\n")
                            if request.startswith(("archive\t", "refresh\t")):
                                received.append(request)
                            connection.sendall(b"change-header(done)\n")

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            with mock.patch("agent_fleet.ui.RUNTIME", runtime), \
                 mock.patch.object(ui, "header", return_value="header"), \
                 mock.patch.object(ui, "footer", return_value="footer"), \
                 mock.patch.object(ui.os, "execvp", side_effect=RuntimeError) as execute, \
                 self.assertRaises(RuntimeError):
                ui.muster()
            command = execute.call_args.args[1]
            selected = [command[0], "--disabled", "--no-input", "--delimiter=\t",
                        "--with-nth=3..", "--id-nth=1"]
            selected.extend(argument for argument in command
                            if argument.startswith(("--bind=x:", "--bind=R:")))
            tmux_runtime = directory / "tmux"
            tmux_runtime.mkdir()
            environment = {**without_tmux_client(),
                           "TMUX_TMPDIR": str(tmux_runtime)}
            key = "alan:codex-one@lovelace"
            shell = f"printf {shlex.quote(f'{key}\t7\tvisible\n')} | exec {shlex.join(selected)}"
            subprocess.run(["tmux", "new-session", "-d", "-s", "fixture", shell],
                           check=True, env=environment)
            try:
                time.sleep(.05)
                subprocess.run(["tmux", "send-keys", "-t", "=fixture:", "x"],
                               check=True, env=environment)
                for _ in range(100):
                    if received:
                        break
                    time.sleep(.01)
                subprocess.run(["tmux", "send-keys", "-t", "=fixture:", "R"],
                               check=True, env=environment)
                for _ in range(100):
                    if len(received) == 2:
                        break
                    time.sleep(.01)
                self.assertEqual([request.split("\t")[:3] for request in received],
                                 [["archive", key, "7"], ["refresh", key, "7"]])
            finally:
                subprocess.run(["tmux", "kill-server"], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

    def test_private_ui_has_no_fold_or_toggle_commands(self):
        with self.assertRaises(SystemExit):
            ui_process.main(["fold", "open", "alan:claude-one@lovelace"])
        with self.assertRaises(SystemExit):
            ui_process.main(["toggle", "language"])

    def test_private_ui_has_no_python_show_command(self):
        with self.assertRaises(SystemExit):
            ui_process.main(["show", "alan:claude-child@lovelace", "--slot", "main"])

    def test_next_waiting_unwraps_projected_sessions(self):
        active = replace(self.session("lovelace", "$1"), reported_state="waiting")
        child = replace(self.session("lovelace", "$2"), reported_state="waiting")
        with mock.patch("agent_fleet.daemon.request", return_value=child.ref.key + "\n") as request, \
             mock.patch.object(actions.viewer, "slots",
                               return_value=[("main", active.ref.key)]), \
             mock.patch.object(actions.viewer, "open_main") as show:
            actions.next_waiting()
        request.assert_called_once_with("next-waiting\t" + active.ref.key)
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
                "Enter view  c create  r rename  R refresh  x archive  l open fold  h close fold  p python")

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

    def test_named_viewers_route_to_the_resident_lovelace_compositor(self):
        launcher = (Path(__file__).parents[1] / "fleet-viewer").read_text()
        self.assertIn('exec ssh -tt -o BatchMode=yes lovelace fleet-viewer "$slot"',
                      launcher)
        self.assertIn('new-session -d -s "fleet@$slot"', launcher)

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
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        failed = subprocess.CalledProcessError(1, ["tmux"])
        with mock.patch.object(state, "find", return_value=session), \
             mock.patch.object(viewer.subprocess, "run", side_effect=failed), \
             mock.patch.object(viewer.subprocess, "Popen") as popen:
            with self.assertRaises(viewer.ViewerFailure) as raised:
                state.resolve(f"alan:{actor}")
        self.assertEqual((raised.exception.stage, raised.exception.cause,
                          raised.exception.error_type),
                         ("resolve", "unavailable", "CalledProcessError"))
        popen.assert_not_called()

    def test_python_open_retains_the_fleet_owned_presentation(self):
        actor = f"python-1@{os.uname().nodename}"
        session = Session(SessionRef(ServerRef(os.uname().nodename, "", 0, 0, "alan"),
                                     actor), "python", 0, 0, 0, 1, "alan", "", "/work",
                          "python")
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
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
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        listed = mock.Mock(stdout="/tmp/tmux/default\t12\t10\t$1\n")
        with mock.patch.object(state, "find", return_value=session), \
             mock.patch.object(viewer.presentation, "target") as target, \
             mock.patch.object(viewer.subprocess, "run", return_value=listed):
            state.resolve(f"alan:{actor}")
        target.assert_not_called()

    def test_remote_actor_resolution_survives_the_remote_shell(self):
        actor = "codex-1@newton"
        session = mock.Mock(agent="codex", state="waiting", cwd="/work")
        state = viewer.Attachment("main", "/dev/pts/9", mock.Mock())
        listed = mock.Mock(
            stdout="/tmp/tmux-1000/default 2548 1784382062 \\$191\n")
        with mock.patch.object(state, "find", return_value=session), \
                mock.patch.object(state, "ssh", return_value=listed) as ssh:
            self.assertEqual(state.resolve(f"alan:{actor}", remote=True),
                             ("/tmp/tmux-1000/default", 2548, 1784382062, "$191"))
        self.assertIn("#{q:socket_path}", ssh.call_args.args[1])

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

    def test_collector_preview_fails_when_retained_alan_graph_is_unavailable(self):
        with mock.patch.object(tmux.alan, "preview") as preview:
            with self.assertRaisesRegex(RuntimeError, "observation is unavailable"):
                tmux.capture("alan:llm-1@newton", 80, 20, None)
        preview.assert_not_called()

    def test_viewer_clear_remains_an_internal_primitive(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as runtime:
            env = {**os.environ, "XDG_RUNTIME_DIR": runtime,
                   "PYTHONPATH": str(root), "TMUX_TMPDIR": runtime}
            subprocess.run(["tmux", "-L", "agent-fleet-ui", "new-session", "-d",
                            "-s", "fleet@test", "sleep 30"], check=True, env=env)
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
                subprocess.run(["tmux", "-L", "agent-fleet-ui", "kill-server"],
                               env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

    def test_viewer_clear_exact_key_is_atomic_and_ignores_a_moved_viewer(self):
        with tempfile.TemporaryDirectory() as runtime:
            runtime = Path(runtime)
            state = mock.Mock()
            state.source = "source-new"
            state.check.return_value = ""
            with mock.patch.object(viewer, "RUNTIME", runtime), \
                 mock.patch.object(viewer.os, "ttyname", return_value="/dev/pts/9"), \
                 mock.patch.object(viewer, "Attachment", return_value=state), \
                 mock.patch.object(viewer, "viewer_error"):
                thread = threading.Thread(target=viewer.serve, args=("test",))
                thread.start()
                path = runtime / "viewer-test.sock"
                for _ in range(100):
                    if path.exists():
                        break
                    time.sleep(.01)
                async def send(message):
                    await Fleet.update_viewer(path, message)

                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall(b"SOURCE\n")
                    self.assertEqual(client.recv(64), b"source-new\n")
                asyncio.run(send("CLEAR source-old"))
                state.clear.assert_not_called()
                asyncio.run(send("CLEAR source-new"))
                state.clear.assert_called_once_with()
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall(b"SHUTDOWN\n")
                    self.assertEqual(client.recv(16), b"OK\n")
                thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_viewer_worker_collapses_pending_projection_to_latest(self):
        state = mock.Mock()
        state.source = ""
        state.check.return_value = ""
        entered = threading.Event()
        release = threading.Event()

        def open_source(key, _selected=None, _host=None):
            if key == self.SOURCE_A:
                entered.set()
                release.wait(2)
            state.source = key

        state.open.side_effect = open_source
        worker = viewer.ViewerWorker(state, "main")
        try:
            worker.intent("PROJECT", self.SOURCE_A)
            self.assertTrue(entered.wait(1))
            worker.intent("PROJECT", self.SOURCE_B)
            worker.intent("PROJECT", self.SOURCE_C)
            release.set()
            self.assertEqual(worker.barrier("SOURCE"), self.SOURCE_C)
        finally:
            worker.close()
        self.assertEqual(state.open.call_args_list,
                         [mock.call(self.SOURCE_A, None, "lovelace"),
                          mock.call(self.SOURCE_C, None, "lovelace")])

    def test_viewer_worker_enter_supersedes_pending_projection_then_focuses(self):
        state = mock.Mock()
        state.source = ""
        state.workstation = "boltzmann"
        state.check.return_value = ""
        entered = threading.Event()
        release = threading.Event()

        def open_source(key, _selected=None, _host=None):
            if key == self.SOURCE_A:
                entered.set()
                release.wait(2)
            state.source = key

        state.open.side_effect = open_source
        worker = viewer.ViewerWorker(state, "main")
        try:
            worker.intent("PROJECT", self.SOURCE_A)
            self.assertTrue(entered.wait(1))
            worker.intent("PROJECT", self.SOURCE_B)
            with mock.patch.object(viewer, "focus") as focus:
                worker.intent("FOCUS", self.SOURCE_D)
                release.set()
                self.assertEqual(worker.barrier("SOURCE"), self.SOURCE_D)
            focus.assert_called_once_with("main", "boltzmann")
        finally:
            worker.close()
        self.assertEqual(state.open.call_args_list,
                         [mock.call(self.SOURCE_A, None, "lovelace"),
                          mock.call(self.SOURCE_D, None, "lovelace")])

    def test_viewer_worker_exact_clear_cancels_pending_key_before_new_source(self):
        state = mock.Mock()
        state.source = ""
        state.check.return_value = ""
        entered = threading.Event()
        release = threading.Event()

        def open_source(key, _selected=None, _host=None):
            if key == self.SOURCE_K:
                entered.set()
                release.wait(2)
            state.source = key

        state.open.side_effect = open_source
        state.clear.side_effect = lambda: setattr(state, "source", "")
        worker = viewer.ViewerWorker(state, "main")
        try:
            worker.intent("PROJECT", self.SOURCE_K)
            self.assertTrue(entered.wait(1))
            worker.intent("PROJECT", self.SOURCE_K)
            cleared = threading.Thread(
                target=worker.barrier, args=("CLEAR", self.SOURCE_K))
            cleared.start()
            release.set()
            cleared.join(1)
            self.assertFalse(cleared.is_alive())
            worker.intent("PROJECT", self.SOURCE_J)
            self.assertEqual(worker.barrier("SOURCE"), self.SOURCE_J)
        finally:
            worker.close()
        self.assertEqual(state.open.call_args_list,
                         [mock.call(self.SOURCE_K, None, "lovelace"),
                          mock.call(self.SOURCE_J, None, "lovelace")])
        state.clear.assert_called_once_with()

    def test_viewer_worker_terminal_failure_rejects_future_work_without_hanging(self):
        state = mock.Mock()
        state.source = ""
        state.host = ""
        state.check.return_value = ""
        entered = threading.Event()
        release = threading.Event()

        def fail(_key, _selected=None, _host=None):
            entered.set()
            release.wait(2)
            raise AssertionError("planted bug")

        state.open.side_effect = fail
        pending_error = []

        def record_pending_error():
            try:
                worker.barrier("SOURCE")
            except Exception as error:
                pending_error.append(error)

        with mock.patch.object(viewer, "viewer_error") as report:
            worker = viewer.ViewerWorker(state, "main")
            worker.intent("PROJECT", self.SOURCE_A)
            self.assertTrue(entered.wait(1))
            pending = threading.Thread(target=record_pending_error)
            pending.start()
            release.set()
            worker.thread.join(1)
            pending.join(1)
            self.assertFalse(worker.thread.is_alive())
            self.assertFalse(pending.is_alive())
            self.assertRegex(str(pending_error[0]), "planted bug")
            with self.assertRaisesRegex(RuntimeError, "planted bug"):
                worker.barrier("SOURCE")
            with self.assertRaisesRegex(RuntimeError, "planted bug"):
                worker.intent("PROJECT", self.SOURCE_B)
        report.assert_called_with("viewer worker failed: planted bug")

    def test_viewer_worker_close_terminates_after_expected_shutdown_failure(self):
        state = mock.Mock()
        state.source = ""
        state.host = ""
        state.check.return_value = ""
        state.shutdown.side_effect = RuntimeError("cleanup failed")
        with mock.patch.object(viewer, "viewer_error") as report:
            worker = viewer.ViewerWorker(state, "main")
            closed = threading.Thread(target=worker.close)
            closed.start()
            closed.join(1)
            self.assertFalse(closed.is_alive())
            self.assertFalse(worker.thread.is_alive())
        report.assert_called_with("Open failed: cleanup failed")

    def test_viewer_worker_reports_projection_failure_then_applies_newest(self):
        state = mock.Mock()
        state.source = ""
        state.host = ""
        state.check.return_value = ""
        failed = threading.Event()

        def open_source(key, _selected=None, _host=None):
            if key == self.SOURCE_A:
                failed.set()
                raise RuntimeError("planted projection failure")
            state.source = key

        state.open.side_effect = open_source
        with mock.patch.object(viewer, "viewer_error") as report:
            worker = viewer.ViewerWorker(state, "main")
            try:
                worker.intent("PROJECT", self.SOURCE_A)
                self.assertTrue(failed.wait(1))
                worker.intent("PROJECT", self.SOURCE_B)
                self.assertEqual(worker.barrier("SOURCE"), self.SOURCE_B)
                self.assertTrue(worker.thread.is_alive())
            finally:
                worker.close()
        self.assertIn(mock.call("Open failed: planted projection failure"),
                      report.call_args_list)

    def test_viewer_worker_open_and_source_are_completion_barriers(self):
        state = mock.Mock()
        state.source = ""
        state.workstation = "boltzmann"
        state.check.return_value = ""
        entered = threading.Event()
        release = threading.Event()
        result = []

        def open_source(key, _selected=None, _host=None):
            entered.set()
            release.wait(2)
            state.source = key

        state.open.side_effect = open_source
        worker = viewer.ViewerWorker(state, "main")
        with mock.patch.object(viewer, "focus"):
            opened = threading.Thread(
                target=worker.barrier, args=("OPEN", self.SOURCE_A))
            opened.start()
            self.assertTrue(entered.wait(1))
            sourced = threading.Thread(
                target=lambda: result.append(worker.barrier("SOURCE")))
            sourced.start()
            time.sleep(.02)
            self.assertTrue(opened.is_alive())
            self.assertTrue(sourced.is_alive())
            release.set()
            opened.join(1)
            sourced.join(1)
        worker.close()
        self.assertEqual(result, [self.SOURCE_A])

    def test_project_socket_acknowledges_acceptance_before_switch_completion(self):
        with tempfile.TemporaryDirectory() as runtime:
            runtime = Path(runtime)
            state = mock.Mock()
            state.source = ""
            state.workstation = "boltzmann"
            state.check.return_value = ""
            entered = threading.Event()
            release = threading.Event()

            def open_source(key, _selected=None, _host=None):
                entered.set()
                release.wait(2)
                state.source = key

            state.open.side_effect = open_source
            with mock.patch.object(viewer, "RUNTIME", runtime), \
                 mock.patch.object(viewer.os, "ttyname", return_value="/dev/pts/9"), \
                 mock.patch.object(viewer, "Attachment", return_value=state), \
                 mock.patch.object(viewer, "viewer_error"), \
                 mock.patch.object(viewer, "focus") as focus:
                thread = threading.Thread(target=viewer.serve, args=("test",))
                thread.start()
                path = runtime / "viewer-test.sock"
                for _ in range(100):
                    if path.exists():
                        break
                    time.sleep(.01)
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall(f"PROJECT {self.SOURCE_A}\n".encode())
                    self.assertEqual(client.recv(16), b"OK\n")
                self.assertTrue(entered.wait(1))
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall(f"FOCUS {self.SOURCE_D}\n".encode())
                    self.assertEqual(client.recv(16), b"OK\n")
                release.set()
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall(b"SOURCE\n")
                    self.assertEqual(client.recv(128), (self.SOURCE_D + "\n").encode())
                focus.assert_called_once_with("test", "boltzmann")
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall(b"SHUTDOWN\n")
                    self.assertEqual(client.recv(16), b"OK\n")
                thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_stock_fzf_processes_live_keys_while_projection_is_blocked(self):
        root = Path(__file__).parents[1]
        for key in ("x", "h", "enter"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                runtime = directory / "runtime" / "agent-fleet"
                runtime.mkdir(parents=True)
                tmux_runtime = directory / "tmux"
                tmux_runtime.mkdir()
                fzf_socket = runtime / "fzf.sock"
                state = mock.Mock()
                state.source = ""
                state.host = ""
                state.workstation = "boltzmann"
                state.check.return_value = ""
                entered = threading.Event()
                release = threading.Event()

                def open_source(source, _selected=None, _host=None):
                    entered.set()
                    release.wait(2)
                    state.source = source

                state.open.side_effect = open_source
                environment = {**without_tmux_client(),
                               "XDG_RUNTIME_DIR": str(directory / "runtime"),
                               "TMUX_TMPDIR": str(tmux_runtime),
                               "SHELL": "/bin/sh"}
                focus = shlex.join(
                    (str(root / "fleet-open"), "project", "test", "{1}"))
                activate = shlex.join(
                    (str(root / "fleet-open"), "focus", "test", "{1}"))
                command = (f"printf '{self.SOURCE_A}\\t1\\trow\\n' | exec fzf "
                           f"--listen {shlex.quote(str(fzf_socket))} --disabled "
                           f"--bind {shlex.quote('focus:execute-silent(' + focus + ')')} "
                           f"--bind {shlex.quote('enter:execute-silent(' + activate + ')')} "
                           "--bind 'x:reload-sync(:)' "
                           "--bind 'h:reload-sync(:)'")
                with mock.patch.object(viewer, "RUNTIME", runtime), \
                     mock.patch.object(viewer.os, "ttyname",
                                       return_value="/dev/pts/9"), \
                     mock.patch.object(viewer, "source_host", return_value="lovelace"), \
                     mock.patch.object(viewer, "Attachment", return_value=state), \
                     mock.patch.object(viewer, "viewer_error"), \
                     mock.patch.object(viewer, "focus"):
                    server = threading.Thread(target=viewer.serve, args=("test",))
                    server.start()
                    subprocess.run(["tmux", "new-session", "-d", "-s", "fzf",
                                    command], check=True, env=environment)
                    try:
                        for _ in range(100):
                            if fzf_socket.exists():
                                break
                            time.sleep(.01)
                        self.assertTrue(entered.wait(1))
                        if key == "enter":
                            subprocess.run(["tmux", "send-keys", "-t", "fzf",
                                            "Enter"], check=True, env=environment)
                            time.sleep(.02)
                            sent = "x"
                        else:
                            sent = key
                        started = time.monotonic()
                        subprocess.run(["tmux", "send-keys", "-t", "fzf", sent],
                                       check=True, env=environment)
                        while True:
                            result = subprocess.run(
                                ["curl", "-fsS", "--unix-socket", str(fzf_socket),
                                 "http://localhost?limit=10"], text=True,
                                capture_output=True, check=True)
                            if json.loads(result.stdout)["matchCount"] == 0:
                                break
                            self.assertLess(time.monotonic() - started, .5)
                    finally:
                        release.set()
                        with socket.socket(socket.AF_UNIX) as client:
                            client.connect(str(runtime / "viewer-test.sock"))
                            client.sendall(b"SHUTDOWN\n")
                            client.recv(16)
                        server.join(1)
                        subprocess.run(["tmux", "kill-server"], env=environment,
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
                self.assertFalse(server.is_alive())

    def test_shutdown_acknowledges_only_after_cleanup_and_socket_removal(self):
        with tempfile.TemporaryDirectory() as runtime:
            runtime = Path(runtime)
            entered = threading.Event()
            release = threading.Event()
            state = mock.Mock()

            def shutdown():
                entered.set()
                release.wait(2)

            state.shutdown.side_effect = shutdown
            state.check.return_value = ""
            with mock.patch.object(viewer, "RUNTIME", runtime), \
                 mock.patch.object(viewer.os, "ttyname", return_value="/dev/pts/9"), \
                 mock.patch.object(viewer, "Attachment", return_value=state), \
                 mock.patch.object(viewer, "viewer_error"):
                thread = threading.Thread(target=viewer.serve, args=("test",))
                thread.start()
                path = runtime / "viewer-test.sock"
                for _ in range(100):
                    if path.exists():
                        break
                    time.sleep(.01)
                with socket.socket(socket.AF_UNIX) as client:
                    for _ in range(100):
                        try:
                            client.connect(str(path))
                            break
                        except ConnectionRefusedError:
                            time.sleep(.01)
                    else:
                        self.fail("viewer socket did not accept connections")
                    client.sendall(b"SHUTDOWN\n")
                    self.assertTrue(entered.wait(1))
                    client.settimeout(.05)
                    with self.assertRaises(TimeoutError):
                        client.recv(16)
                    release.set()
                    self.assertEqual(client.recv(16), b"OK\n")
                    self.assertFalse(path.exists())
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertFalse(path.exists())
            state.shutdown.assert_called_once_with()

    def test_shutdown_cleanup_failure_keeps_controller_available(self):
        with tempfile.TemporaryDirectory() as runtime:
            runtime = Path(runtime)
            state = mock.Mock()
            state.host = state.source = ""
            state.shutdown.side_effect = [OSError("planted cleanup failure"), None]
            state.check.return_value = ""
            with mock.patch.object(viewer, "RUNTIME", runtime), \
                 mock.patch.object(viewer.os, "ttyname", return_value="/dev/pts/9"), \
                 mock.patch.object(viewer, "Attachment", return_value=state), \
                 mock.patch.object(viewer, "viewer_error"):
                thread = threading.Thread(target=viewer.serve, args=("test",))
                thread.start()
                path = runtime / "viewer-test.sock"
                for _ in range(100):
                    if path.exists():
                        break
                    time.sleep(.01)
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall(b"SHUTDOWN\n")
                    self.assertEqual(
                        client.recv(128), b"ERROR planted cleanup failure\n")
                    self.assertTrue(path.exists())
                self.assertTrue(thread.is_alive())
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(path))
                    client.sendall(b"SHUTDOWN\n")
                    self.assertEqual(client.recv(16), b"OK\n")
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertFalse(path.exists())
            self.assertEqual(state.shutdown.call_count, 2)

    @unittest.skipUnless(os.uname().nodename.split(".", 1)[0] == "lovelace",
                         "launcher ownership boundary is on lovelace")
    def test_destroy_preserves_ui_session_when_controller_rejects_shutdown(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            tmux_tmp = Path(directory) / "tmux"
            socket_dir = runtime / "agent-fleet"
            runtime.mkdir(); tmux_tmp.mkdir(); socket_dir.mkdir()
            environment = {**os.environ, "XDG_RUNTIME_DIR": str(runtime),
                           "TMUX_TMPDIR": str(tmux_tmp)}
            subprocess.run(["tmux", "-L", "agent-fleet-ui", "new-session", "-d",
                            "-s", "fleet@failure", "sleep 30"],
                           check=True, env=environment)
            path = socket_dir / "viewer-failure.sock"
            ready = threading.Event()

            def reject():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(str(path)); server.listen(); ready.set()
                    connection, _ = server.accept()
                    with connection:
                        connection.makefile().readline()
                        connection.sendall(b"ERROR marker cleanup failed\n")

            thread = threading.Thread(target=reject); thread.start()
            ready.wait(1)
            try:
                result = subprocess.run([root / "fleet-viewer", "--destroy", "failure"],
                                        env=environment, text=True,
                                        capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ERROR marker cleanup failed", result.stderr)
                alive = subprocess.run(
                    ["tmux", "-L", "agent-fleet-ui", "has-session",
                     "-t", "fleet@failure"], env=environment)
                self.assertEqual(alive.returncode, 0)
            finally:
                thread.join(1)
                subprocess.run(["tmux", "-L", "agent-fleet-ui", "kill-server"],
                               env=environment, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

    def test_quota_only_events_force_an_inventory_emit(self):
        source = (Path(__file__).parents[1] / "agent_fleet/tmux.py").read_text()
        self.assertIn('force = bool({"alan", "quota"} & set(events))', source)
        self.assertIn("if serial != previous or force or current_available != available:", source)
