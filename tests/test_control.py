import asyncio
import contextlib
import json
import os
import pty
import fcntl
import queue
import select
import shlex
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import networkx as nx
from pathlib import Path
from unittest import mock

from libtmux.exc import LibTmuxException

from agent_fleet.tmux import (ControlClient, event_stream, watched_event,
                              watch_socket)
from agent_fleet.daemon import Fleet
from agent_fleet import alan, daemon
from agent_fleet.model import ServerRef, SessionRef, Session


class WatchBoundaryTests(unittest.TestCase):
    def test_runtime_artifacts_do_not_become_transcript_events(self):
        transcripts = [Path("/home/will/.codex/sessions"),
                       Path("/home/will/.claude/projects")]
        quota = Path("/run/user/1000/agent-fleet/quota.changed")
        self.assertEqual(watched_event(
            "/home/will/.codex/sessions/2026/thread.jsonl", transcripts, quota),
            "transcript")
        self.assertEqual(watched_event(quota, transcripts, quota), "quota")
        self.assertIsNone(watched_event(
            "/run/user/1000/agent-fleet/muster-view-12-3.rows",
            transcripts, quota))


class ResidentControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = {**os.environ, "TMUX_TMPDIR": self.temporary.name}
        self.environment["TERM"] = "xterm-256color"
        self.environment.pop("TMUX", None)
        self.environment.pop("TMUX_PANE", None)
        for name in ("source-one", "source-two", "fleet@events"):
            subprocess.run(["tmux", "new-session", "-d", "-s", name,
                            "sleep 30"], check=True, env=self.environment)
        master, slave = pty.openpty()
        self.master = master
        self.client_name = os.ttyname(slave)
        self.viewer = subprocess.Popen(
            ["tmux", "-N", "attach-session", "-t", "source-one"],
            stdin=slave, stdout=slave, stderr=slave, env=self.environment,
            preexec_fn=lambda: (os.setsid(), fcntl.ioctl(0, termios.TIOCSCTTY, 0)))
        os.close(slave)
        self.process = subprocess.Popen(
            ["tmux", "-N", "-C", "attach-session", "-t", "fleet@events"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.environment)
        self.changed = queue.Queue()
        self.control = ControlClient(self.process, self.changed)
        self.control.command(["refresh-client", "-f", "no-output"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            clients = self.control.command(
                ["list-clients", "-f", "#{==:#{client_control_mode},0}",
                 "-F", "#{client_name}"])
            if clients:
                self.client_name = clients[0]
                break
            time.sleep(.01)
        else:
            self.fail("fixture tmux client did not attach")

    def tearDown(self):
        self.process.terminate(); self.process.wait()
        self.viewer.terminate(); self.viewer.wait()
        os.close(self.master)
        subprocess.run(["tmux", "-N", "kill-server"], env=self.environment,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.temporary.cleanup()

    def target(self, name):
        output = self.control.command([
            "list-sessions", "-f", f"#{{==:#{{session_name}},{name}}}", "-F",
            "#{q:socket_path} #{pid} #{start_time} #{q:session_id}"])
        socket, pid, started, session_id = shlex.split(output[0])
        return socket, int(pid), int(started), session_id

    def test_repeated_switches_reuse_control_and_interactive_processes(self):
        identities = self.process.pid, self.viewer.pid
        server_pid = self.target("source-one")[1]
        children = Path(f"/proc/{server_pid}/task/{server_pid}/children").read_text()
        for _ in range(10):
            self.control.switch(self.target("source-two"), self.client_name)
            self.control.switch(self.target("source-one"), self.client_name)
        self.assertEqual((self.process.pid, self.viewer.pid), identities)
        self.assertIsNone(self.process.poll())
        self.assertIsNone(self.viewer.poll())
        self.assertEqual(
            Path(f"/proc/{server_pid}/task/{server_pid}/children").read_text(), children)

    def test_exact_switch_rejects_stale_missing_and_wrong_client(self):
        target = self.target("source-two")
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            self.control.switch((target[0], target[1] + 1, *target[2:]),
                                self.client_name)
        with self.assertRaises(RuntimeError):
            self.control.switch((*target[:3], "$999"), self.client_name)
        with self.assertRaises(RuntimeError):
            self.control.switch(target, "/dev/pts/does-not-exist")

    def test_alan_target_is_exact_and_absence_fails(self):
        with mock.patch("agent_fleet.tmux.alan.runtime_name", return_value="actor"):
            with self.assertRaisesRegex(RuntimeError, "unavailable or ambiguous"):
                self.control.alan_target("codex-actor@host")
            subprocess.run(["tmux", "new-session", "-d", "-s",
                            "fleet@alan-actor", "sleep 30"], check=True,
                           env=self.environment)
            self.assertEqual(self.control.alan_target("codex-actor@host"),
                             self.target("fleet@alan-actor"))

    def test_resident_control_constructs_exact_bare_target_but_not_codex(self):
        identities = self.process.pid, self.target("source-one")[:3]
        descriptor = {"addr": "llm-actor@host", "kind": "llm",
                      "cwd": str(Path.cwd())}
        codex = {"addr": "codex-actor@host", "kind": "codex",
                 "cwd": str(Path.cwd())}
        with mock.patch.dict(os.environ, self.environment, clear=True), \
             mock.patch("agent_fleet.presentation.shlex",
                        mock.Mock(join=mock.Mock(return_value="sleep 30"))):
            self.assertEqual(self.control.alan_target("llm-actor@host", descriptor),
                             self.target("fleet@alan-" +
                                         alan.runtime_name("llm-actor@host")))
            with self.assertRaisesRegex(RuntimeError, "unavailable or ambiguous"):
                self.control.alan_target("codex-actor@host", codex)
        self.assertEqual((self.process.pid, self.target("source-one")[:3]), identities)
        names = self.control.command(["list-sessions", "-F", "#{session_name}"])
        self.assertNotIn("fleet@alan-" + alan.runtime_name(codex["addr"]), names)

    def test_native_preview_does_not_wait_for_alan_graph_lock(self):
        graph_lock = threading.Lock()
        graph_lock.acquire()
        watcher = mock.Mock()
        watcher.full_graph.side_effect = lambda: graph_lock
        controls = mock.Mock()
        request = {"key": "alan:codex-actor@host", "columns": 80, "lines": 20}
        finished = threading.Event()

        def preview():
            daemon.capture_preview(controls, watcher, request)
            finished.set()

        with mock.patch.object(daemon, "capture", return_value="screen") as capture:
            thread = threading.Thread(target=preview)
            thread.start()
            self.assertTrue(finished.wait(1))
            thread.join()
        controls.get.assert_called_once_with()
        watcher.full_graph.assert_not_called()
        capture.assert_called_once_with("alan:codex-actor@host", 80, 20)
        graph_lock.release()

    def test_graph_preview_uses_coherent_alan_graph(self):
        graph = object()
        watcher = mock.Mock()
        watcher.full_graph.return_value = contextlib.nullcontext(graph)
        controls = mock.Mock()
        request = {"key": "alan:llm-actor@host", "columns": 80, "lines": 20}
        with mock.patch.object(daemon, "capture", return_value="screen") as capture:
            self.assertEqual(daemon.capture_preview(controls, watcher, request), "screen")
        controls.get.assert_not_called()
        capture.assert_called_once_with("alan:llm-actor@host", 80, 20, graph)

    def test_disconnect_fails_an_outstanding_command(self):
        self.process.terminate()
        self.process.wait()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.control.command(["display-message", "-p", "never"])

    def test_tagged_host_process_switch_and_preview_boundary(self):
        target = self.target("source-two")
        self.process.terminate(); self.process.wait()
        command = [
            os.environ.get("PYTHON", "python"), "-c",
            "import sys; from agent_fleet.daemon import events; events(sys.argv[1])",
            "fixture"]
        host = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env={**self.environment,
                 "PYTHONPATH": str(Path(__file__).parents[1])})
        try:
            while True:
                message = json.loads(host.stdout.readline())
                if message.get("version") == 1:
                    break
            host.stdin.write(json.dumps({"switch": 7, "target": target,
                                         "client": self.client_name}) + "\n")
            host.stdin.write(json.dumps({"preview": 8,
                                         "key": "fixture:/tmp/absent:1:1:$1",
                                         "columns": 80, "lines": 20}) + "\n")
            host.stdin.flush()
            replies = {}
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and set(replies) != {"switch", "preview"}:
                ready, _, _ = select.select([host.stdout], [], [], .2)
                if not ready: continue
                message = json.loads(host.stdout.readline())
                for tag in ("switch", "preview"):
                    if tag in message: replies[tag] = message
            self.assertEqual(replies["switch"]["target"], list(target))
            self.assertLess(replies["switch"]["duration"], 1)
            self.assertIn("error", replies["preview"])
        finally:
            host.terminate(); host.wait()


class ProtocolCorrelationTests(unittest.TestCase):
    def test_socket_watch_closes_directory_to_socket_handoff_race(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket = root / "tmux-1000" / "default"
            changed = queue.Queue()
            consumer = threading.Event()
            calls = 0

            def raced_watch(parent, **_arguments):
                nonlocal calls
                calls += 1
                if calls == 1:
                    socket.parent.mkdir()
                else:
                    socket.touch()
                yield set()

            with mock.patch("agent_fleet.tmux.default_socket", return_value=socket), \
                 mock.patch("agent_fleet.tmux.watch", side_effect=raced_watch):
                thread = threading.Thread(
                    target=watch_socket, args=(changed, consumer), daemon=True)
                thread.start()
                self.assertEqual(changed.get(timeout=1), "socket")
                consumer.set()
                thread.join(1)

    def test_inventory_loss_returns_same_collector_to_unavailable(self):
        consumer = threading.Event()
        changed = queue.Queue()
        alan = mock.Mock(error=None)
        alan.snapshot.return_value = contextlib.nullcontext(([], None))
        process = mock.Mock(stdout=mock.Mock(), stdin=mock.Mock(), stderr=mock.Mock())
        process.poll.return_value = None
        control = mock.Mock(closed=False)
        tmux = mock.Mock()
        tmux.has_session.return_value = True
        probes = [mock.Mock(returncode=0), mock.Mock(returncode=1),
                  mock.Mock(returncode=1)]
        with mock.patch("agent_fleet.tmux.AlanWatcher", return_value=alan), \
             mock.patch("agent_fleet.tmux.subprocess.run", side_effect=probes), \
             mock.patch("agent_fleet.tmux.subprocess.Popen", return_value=process), \
             mock.patch("agent_fleet.tmux.ControlClient", return_value=control), \
             mock.patch("agent_fleet.tmux.server", return_value=tmux), \
             mock.patch("agent_fleet.tmux.inventory",
                        side_effect=LibTmuxException("server disappeared")):
            stream = event_stream("fixture", consumer, changed=changed)
            self.assertEqual(next(stream), ([], None, False))
            consumer.set()
            changed.put("consumer")
            with self.assertRaises(StopIteration):
                next(stream)

    def test_non_tmux_adapter_failure_keeps_control_available(self):
        consumer = threading.Event()
        changed = queue.Queue()
        alan = mock.Mock(error=None)
        alan.snapshot.return_value = contextlib.nullcontext(([], None))
        process = mock.Mock(stdout=mock.Mock(), stdin=mock.Mock(), stderr=mock.Mock())
        process.poll.return_value = None
        control = mock.Mock(closed=False)
        tmux = mock.Mock()
        tmux.has_session.return_value = True
        item = mock.Mock(ref="tmux session")
        failure = subprocess.CalledProcessError(1, ["claude", "agents", "--json"])
        with mock.patch("agent_fleet.tmux.AlanWatcher", return_value=alan), \
             mock.patch("agent_fleet.tmux.subprocess.run",
                        side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=0)]), \
             mock.patch("agent_fleet.tmux.subprocess.Popen", return_value=process), \
             mock.patch("agent_fleet.tmux.ControlClient", return_value=control), \
             mock.patch("agent_fleet.tmux.server", return_value=tmux), \
             mock.patch("agent_fleet.tmux.inventory", return_value=[item]), \
             mock.patch("agent_fleet.tmux.alan_inventory", return_value=[]), \
             mock.patch("agent_fleet.tmux.observe", side_effect=failure):
            stream = event_stream("fixture", consumer, changed=changed)
            sessions, _, available = next(stream)
            self.assertEqual(sessions, [item])
            self.assertTrue(available)
            process.terminate.assert_not_called()
            consumer.set()
            changed.put("consumer")
            with self.assertRaises(StopIteration):
                next(stream)

    def test_non_tmux_adapter_failure_retains_cached_agent_projection(self):
        consumer = threading.Event()
        changed = queue.Queue()
        alan = mock.Mock(error=None)
        alan.snapshot.return_value = contextlib.nullcontext(([], None))
        process = mock.Mock(stdout=mock.Mock(), stdin=mock.Mock(), stderr=mock.Mock())
        process.poll.return_value = None
        control = mock.Mock(closed=False)
        tmux = mock.Mock()
        tmux.has_session.return_value = True
        ref = SessionRef(ServerRef("fixture", "/tmp/tmux", 1, 2), "$1")
        raw = Session(ref, "source", 1, 1, 0, 1, "zsh", "", "/tmp")
        changed_raw = Session(ref, "source", 1, 2, 0, 1, "zsh", "", "/tmp")
        projected = Session(ref, "source", 1, 1, 0, 1, "zsh", "", "/tmp",
                            agent_name="claude", reported_state="working",
                            summary="derived", transcript_id="identity")
        failure = subprocess.CalledProcessError(1, ["claude", "agents", "--json"])
        with mock.patch("agent_fleet.tmux.AlanWatcher", return_value=alan), \
             mock.patch("agent_fleet.tmux.subprocess.run",
                        side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=0)]), \
             mock.patch("agent_fleet.tmux.subprocess.Popen", return_value=process), \
             mock.patch("agent_fleet.tmux.ControlClient", return_value=control), \
             mock.patch("agent_fleet.tmux.server", return_value=tmux), \
             mock.patch("agent_fleet.tmux.inventory",
                        side_effect=[[raw], [changed_raw]]), \
             mock.patch("agent_fleet.tmux.observe",
                        side_effect=[[projected], failure]):
            stream = event_stream("fixture", consumer, changed=changed)
            self.assertEqual(next(stream)[0], [projected])
            changed.put("transcript")
            [cached] = next(stream)[0]
            self.assertEqual((cached.activity, cached.agent_name, cached.summary),
                             (2, "claude", "derived"))
            self.assertTrue(control.closed is False)
            consumer.set()
            changed.put("consumer")
            with self.assertRaises(StopIteration):
                next(stream)

    def test_internal_observation_invariant_is_not_replaced_by_cached_fields(self):
        consumer = threading.Event()
        changed = queue.Queue()
        alan = mock.Mock(error=None)
        alan.snapshot.return_value = contextlib.nullcontext(([], None))
        process = mock.Mock(stdout=mock.Mock(), stdin=mock.Mock(), stderr=mock.Mock())
        process.poll.return_value = None
        control = mock.Mock(closed=False)
        tmux = mock.Mock()
        tmux.has_session.return_value = True
        alan_server = ServerRef("fixture", "", 0, 0, "alan")
        tmux_server = ServerRef("fixture", "/tmp/tmux", 1, 2)
        actor = Session(SessionRef(alan_server, "claude-one@fixture"), "one",
                        1, 1, 0, 1, "", "", "/tmp", agent_name="claude",
                        transcript_id="identity")
        duplicate = Session(SessionRef(alan_server, "claude-two@fixture"), "two",
                            1, 2, 0, 1, "", "", "/tmp", agent_name="claude",
                            transcript_id="identity")
        provider = Session(SessionRef(tmux_server, "$1"), "provider", 1, 1, 0, 1,
                           "zsh", "", "/tmp", agent_name="claude",
                           transcript_id="identity")

        def subprocess_result(arguments, **_options):
            if arguments[:2] == ["claude", "agents"]:
                return mock.Mock(returncode=0, stdout="[]")
            return mock.Mock(returncode=0, stdout="")

        with mock.patch("agent_fleet.tmux.AlanWatcher", return_value=alan), \
             mock.patch("agent_fleet.tmux.subprocess.run",
                        side_effect=subprocess_result), \
             mock.patch("agent_fleet.tmux.subprocess.Popen", return_value=process), \
             mock.patch("agent_fleet.tmux.ControlClient", return_value=control), \
             mock.patch("agent_fleet.tmux.server", return_value=tmux), \
             mock.patch("agent_fleet.tmux.inventory",
                        side_effect=[[actor, provider], [actor, duplicate, provider]]), \
             mock.patch("agent_fleet.tmux.native_transcripts.catalog", return_value={}):
            stream = event_stream("fixture", consumer, changed=changed)
            [projected] = next(stream)[0]
            self.assertEqual(projected.attachment, provider.ref)
            changed.put("alan")
            with self.assertRaisesRegex(
                    RuntimeError,
                    "expected at most one Alan actor and provider session.*found 2 and 1"):
                next(stream)

    def test_one_collector_survives_tmux_absence_loss_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tmux_root = root / "tmux"
            runtime = root / "runtime"
            home = root / "home"
            for path in (tmux_root, runtime, home):
                path.mkdir()
            environment = {
                **os.environ, "TMUX_TMPDIR": str(tmux_root),
                "XDG_RUNTIME_DIR": str(runtime), "HOME": str(home),
                "FLEET_TMUX": str(Path(__file__).parents[1] / "fleet-tmux"),
            }
            environment.pop("TMUX", None)
            consumer = threading.Event()
            changed = queue.Queue()
            alan = mock.Mock(error=None)
            alan.snapshot.return_value = contextlib.nullcontext(([], None))

            def advance(stream):
                result = queue.Queue()
                thread = threading.Thread(
                    target=lambda: result.put(next(stream)), daemon=True)
                thread.start()
                value = result.get(timeout=3)
                thread.join(1)
                return value

            with mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch("agent_fleet.tmux.AlanWatcher", return_value=alan), \
                 mock.patch("agent_fleet.tmux.RUNTIME", runtime / "agent-fleet"):
                stream = event_stream("fixture", consumer, changed=changed)
                self.assertEqual(next(stream), ([], None, False))
                subprocess.run(["tmux", "new-session", "-d", "-s", "source",
                                "sleep 30"], check=True, env=environment)
                sessions, _, available = advance(stream)
                self.assertTrue(available)
                self.assertEqual([item.name for item in sessions], ["source"])
                subprocess.run(["tmux", "kill-server"], check=True, env=environment)
                self.assertEqual(advance(stream), ([], None, False))
                subprocess.run(["tmux", "new-session", "-d", "-s", "again",
                                "sleep 30"], check=True, env=environment)
                sessions, _, available = advance(stream)
                self.assertTrue(available)
                self.assertEqual([item.name for item in sessions], ["again"])
                consumer.set()
                changed.put("consumer")
                with self.assertRaises(StopIteration):
                    next(stream)
                subprocess.run(["tmux", "kill-server"], env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_absent_tmux_refuses_switch_and_preview_but_allows_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                **os.environ, "TMUX_TMPDIR": str(root / "tmux"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "HOME": str(root / "home"),
                "PYTHONPATH": str(Path(__file__).parents[1]),
            }
            environment.pop("TMUX", None)
            for name in ("tmux", "runtime", "home"):
                (root / name).mkdir()
            config = root / "home" / ".config" / "agent-fleet"
            config.mkdir(parents=True)
            (config / "hosts").write_text("fixture\n")
            host = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; from agent_fleet.daemon import events; events(sys.argv[1])",
                 "fixture"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment)
            try:
                observation = json.loads(host.stdout.readline())
                self.assertEqual(observation["unavailable"], ["fixture"])
                requests = [
                    {"switch": 1, "target": ["/tmp/socket", 1, 1, "$1"],
                     "client": "/dev/pts/9"},
                    {"preview": 2, "key": "fixture:/tmp/socket:1:1:$1",
                     "columns": 80, "lines": 20},
                    {"cleanup": 3, "owner": "lovelace", "slot": "main"},
                ]
                for request in requests:
                    host.stdin.write(json.dumps(request) + "\n")
                host.stdin.flush()
                replies = {}
                deadline = time.monotonic() + 3
                while len(replies) < 3 and time.monotonic() < deadline:
                    ready, _, _ = select.select([host.stdout], [], [], .2)
                    if ready:
                        message = json.loads(host.stdout.readline())
                        for tag in ("switch", "preview", "cleanup"):
                            if tag in message:
                                replies[tag] = message
                self.assertIn("unavailable", replies["switch"]["error"])
                self.assertIn("unavailable", replies["preview"]["error"])
                self.assertEqual(replies["cleanup"], {"cleanup": 3})
            finally:
                host.terminate()
                host.wait()

    def test_event_stream_keeps_tmux_inventory_when_alan_is_unavailable(self):
        changed = queue.Queue()
        consumer = threading.Event()
        alan = mock.Mock(error="Alan unavailable", actors=[], graph=None)
        alan.snapshot.return_value = contextlib.nullcontext(([], None))
        process = mock.Mock(stdout=mock.Mock(), stdin=mock.Mock(),
                            stderr=mock.Mock())
        process.poll.return_value = None
        control = mock.Mock()
        tmux = mock.Mock()
        tmux.has_session.return_value = True
        item = mock.Mock(ref="tmux session")
        with mock.patch("agent_fleet.tmux.AlanWatcher", return_value=alan), \
             mock.patch("agent_fleet.tmux.subprocess.run",
                        return_value=mock.Mock(returncode=0)), \
             mock.patch("agent_fleet.tmux.subprocess.Popen", return_value=process), \
             mock.patch("agent_fleet.tmux.ControlClient", return_value=control), \
             mock.patch("agent_fleet.tmux.server", return_value=tmux), \
             mock.patch("agent_fleet.tmux.inventory", return_value=[item]), \
             mock.patch("agent_fleet.tmux.alan_inventory", return_value=[]), \
             mock.patch("agent_fleet.tmux.observe",
                        side_effect=lambda current, _catalog: current), \
             mock.patch("agent_fleet.tmux.native_transcripts.catalog",
                        return_value={}):
            stream = event_stream("fixture", consumer, changed=changed)
            self.assertEqual(next(stream), ([item], None, True))
            consumer.set()
            changed.put("consumer")
            with self.assertRaises(StopIteration):
                next(stream)

    def test_alan_watcher_change_publishes_without_an_authority_barrier(self):
        changed = queue.Queue()
        consumer = threading.Event()
        process = mock.Mock(stdout=mock.Mock(), stdin=mock.Mock(), stderr=mock.Mock())
        process.poll.return_value = None
        alan = mock.Mock(error=None, actors=["stale"], graph="stale graph")
        alan.snapshot.side_effect = lambda: contextlib.nullcontext(
            (alan.actors, alan.graph))
        control = mock.Mock()
        tmux = mock.Mock()
        tmux.has_session.return_value = True
        with mock.patch("agent_fleet.tmux.AlanWatcher", return_value=alan), \
             mock.patch("agent_fleet.tmux.subprocess.run",
                        return_value=mock.Mock(returncode=0)), \
             mock.patch("agent_fleet.tmux.subprocess.Popen", return_value=process), \
             mock.patch("agent_fleet.tmux.ControlClient", return_value=control), \
             mock.patch("agent_fleet.tmux.server", return_value=tmux), \
             mock.patch("agent_fleet.tmux.inventory", return_value=[]) as inventory, \
             mock.patch("agent_fleet.tmux.observe",
                        side_effect=lambda current, _catalog: current), \
             mock.patch("agent_fleet.tmux.native_transcripts.catalog",
                        return_value={}):
            stream = event_stream("fixture", consumer, changed=changed)
            self.assertEqual(next(stream), ([], "stale graph", True))
            alan.actors = ["fresh"]
            alan.graph = "fresh graph"
            changed.put("alan")
            self.assertEqual(next(stream), ([], "fresh graph", True))
            self.assertEqual(inventory.call_args.args, ("fixture", ["fresh"]))
            finished = threading.Event()

            def resume():
                try:
                    next(stream)
                except StopIteration:
                    finished.set()

            thread = threading.Thread(target=resume)
            thread.start()
            consumer.set()
            changed.put("consumer")
            thread.join(1)
            self.assertTrue(finished.is_set())

    def test_neutral_alan_deltas_do_not_reenter_authoritative_host_scan(self):
        class ControlledStream:
            def __init__(self):
                self.inputs = queue.Queue()
                self.processed = queue.Queue()

            def next(self, update, lock):
                item = self.inputs.get()
                if item is None:
                    raise StopIteration
                graph, change = item
                try:
                    with lock:
                        update(graph, change)
                except Exception as error:
                    self.processed.put(error)
                    raise
                self.processed.put(None)

            def close(self):
                pass

        changed = queue.Queue()
        consumer = threading.Event()
        observations = ControlledStream()
        watcher = object.__new__(alan.Watcher)
        watcher.actors = []
        watcher.graph = None
        watcher.projected = None
        watcher._descriptors = {}
        watcher.available = False
        watcher.error = None
        watcher.initialized = threading.Event()
        watcher._changed = changed
        watcher._consumer = consumer
        watcher._lock = threading.RLock()
        watcher._thread = threading.Thread(target=watcher._run, daemon=True)

        actor = {"addr": "codex-a@fixture", "kind": "codex", "host": "fixture",
                 "cwd": "/work", "state": "waiting"}
        current = nx.MultiDiGraph()
        current.graph["actors"] = [actor]
        current.add_node("codex-a@fixture#0", stream="codex-a@fixture", op="create",
                         time="2026-08-24T12:00:00Z")
        process = mock.Mock(stdout=mock.Mock(), stdin=mock.Mock(), stderr=mock.Mock())
        process.poll.return_value = None
        control = mock.Mock(closed=False)
        tmux = mock.Mock()
        tmux.has_session.return_value = True
        baseline = mock.Mock(ref="baseline")
        relevant = mock.Mock(ref="relevant")

        with mock.patch.object(alan.loop, "observe", return_value=observations), \
             mock.patch("agent_fleet.tmux.watch_socket"), \
             mock.patch("agent_fleet.tmux.watch", return_value=iter(())), \
             mock.patch("agent_fleet.tmux.subprocess.run",
                        return_value=mock.Mock(returncode=0)), \
             mock.patch("agent_fleet.tmux.subprocess.Popen", return_value=process), \
             mock.patch("agent_fleet.tmux.ControlClient", return_value=control), \
             mock.patch("agent_fleet.tmux.server", return_value=tmux), \
             mock.patch("agent_fleet.tmux.inventory",
                        side_effect=[[baseline], [relevant]]) as inventory, \
             mock.patch("agent_fleet.tmux.native_transcripts.catalog",
                        return_value={}) as catalog, \
             mock.patch("agent_fleet.tmux.observe",
                        side_effect=lambda sessions, _catalog: sessions) as observe:
            watcher._thread.start()
            observations.inputs.put((current, {"kind": "replace", "graph": {}}))
            observations.processed.get(timeout=1)
            self.assertEqual(changed.get(timeout=1), "alan")
            stream = event_stream("fixture", consumer, changed=changed,
                                  alan_watcher=watcher)
            publications = queue.Queue()
            finish = threading.Event()

            def publish():
                publications.put(next(stream))
                publications.put(next(stream))
                finish.wait()
                with self.assertRaises(StopIteration):
                    next(stream)

            publisher = threading.Thread(target=publish, daemon=True)
            publisher.start()
            self.assertEqual(publications.get(timeout=1)[0], [baseline])

            for position in range(1, 11):
                current = current.copy()
                node = {
                    "id": f"codex-a@fixture#{position}",
                    "stream": "codex-a@fixture", "op": "output", "status": "ok",
                    "time": f"2026-08-24T12:00:{position:02d}Z",
                    "native": {"viewport": position},
                }
                current.add_node(node["id"], **{key: value for key, value in node.items()
                                                if key != "id"})
                observations.inputs.put((current, {"kind": "delta", "actors": [],
                                                    "nodes": [node], "edges": []}))
                self.assertIsNone(observations.processed.get(timeout=1))
            observations.inputs.put((current, {"kind": "delta", "actors": [],
                                                "nodes": [], "edges": []}))
            self.assertIsNone(observations.processed.get(timeout=1))
            self.assertTrue(changed.empty())
            self.assertEqual((inventory.call_count, catalog.call_count,
                              observe.call_count), (1, 1, 1))
            self.assertTrue(publications.empty())

            current = current.copy()
            current.graph["actors"] = [{**actor, "state": "working"}]
            evaluation = {
                "id": "codex-a@fixture#11", "stream": "codex-a@fixture",
                "op": "evaluation", "time": "2026-08-24T12:00:11Z",
            }
            current.add_node(evaluation["id"], **{
                key: value for key, value in evaluation.items() if key != "id"})
            observations.inputs.put((current, {
                "kind": "delta", "actors": current.graph["actors"],
                "nodes": [evaluation], "edges": [],
            }))
            self.assertIsNone(observations.processed.get(timeout=1))
            self.assertEqual(publications.get(timeout=1)[0], [relevant])
            self.assertEqual((inventory.call_count, catalog.call_count,
                              observe.call_count), (2, 2, 2))

            consumer.set()
            changed.put("consumer")
            finish.set()
            publisher.join(1)
            self.assertFalse(publisher.is_alive())
            observations.inputs.put(None)
            watcher._thread.join(1)

    def test_mismatched_terminal_frame_cannot_acknowledge_command(self):
        lines = queue.Queue()

        class Output:
            def __iter__(self):
                return self

            def __next__(self):
                value = lines.get()
                if value is None:
                    raise StopIteration
                return value

        class Input:
            def write(self, value):
                self.value = value

            def flush(self):
                pass

        process = mock.Mock(stdout=Output(), stdin=Input(), poll=lambda: None)
        control = ControlClient(process, queue.Queue())
        lines.put("%begin 1 10 0\n"); lines.put("%end 1 10 0\n")
        result = []
        thread = threading.Thread(
            target=lambda: result.append(control.command(["display-message", "-p", "ok"])))
        thread.start()
        while not hasattr(process.stdin, "value"):
            time.sleep(.001)
        lines.put("%begin 1 20 1\n"); lines.put("ok\n")
        lines.put("%end 1 21 1\n")
        time.sleep(.01)
        self.assertTrue(thread.is_alive())
        lines.put("%end 1 20 1\n")
        thread.join(1)
        self.assertEqual(result, [["ok"]])
        lines.put(None)

    def test_notification_survives_inflight_reply_and_disconnect_fails_it(self):
        lines = queue.Queue()

        class Output:
            def __iter__(self): return self
            def __next__(self):
                value = lines.get()
                if value is None: raise StopIteration
                return value

        class Input:
            def write(self, value): self.value = value
            def flush(self): pass

        process = mock.Mock(stdout=Output(), stdin=Input(), poll=lambda: None)
        changed = queue.Queue()
        control = ControlClient(process, changed)
        lines.put("%begin 1 10 0\n"); lines.put("%end 1 10 0\n")
        error = []

        def request():
            try:
                control.command(["display-message", "-p", "never"])
            except RuntimeError as caught:
                error.append(str(caught))

        thread = threading.Thread(target=request); thread.start()
        while not hasattr(process.stdin, "value"): time.sleep(.001)
        lines.put("%begin 1 20 1\n")
        lines.put("%sessions-changed\n")
        self.assertEqual(changed.get(timeout=1), "tmux")
        lines.put(None); thread.join(1)
        self.assertEqual(error, ["tmux control client closed"])


class DaemonBoundaryTests(unittest.TestCase):
    @staticmethod
    def session():
        ref = SessionRef(ServerRef("fixture", "/tmp/tmux/default", 12, 10), "$1")
        return Session(ref, "one", 1, 1, 0, 1, "zsh", "", "/tmp")

    def test_preview_switch_and_cleanup_share_tagged_host_stream(self):
        class Input:
            def __init__(self): self.writes = []
            def write(self, value): self.writes.append(json.loads(value))
            async def drain(self): pass

        async def exercise():
            fleet = Fleet(); session = self.session(); stdin = Input()
            fleet.sessions = {"fixture": [session]}; fleet.unavailable.clear()
            fleet.processes = {"fixture": mock.Mock(stdin=stdin)}
            preview = asyncio.create_task(fleet.preview(session.ref.key, 80, 20))
            switch = asyncio.create_task(fleet.switch(session.ref.key, "/dev/pts/9"))
            cleanup = asyncio.create_task(fleet.cleanup("fixture", "lovelace", "main"))
            await asyncio.sleep(0)
            self.assertEqual({next(iter(item)) for item in stdin.writes},
                             {"preview", "switch", "cleanup"})
            preview_request = next(item for item in stdin.writes if "preview" in item)
            switch_request = next(item for item in stdin.writes if "switch" in item)
            cleanup_request = next(item for item in stdin.writes if "cleanup" in item)
            fleet.host_reply({"switch": switch_request["switch"],
                              "target": ["/tmp/tmux/default", 12, 10, "$1"],
                              "duration": .001})
            fleet.host_reply({"preview": preview_request["preview"], "text": "screen"})
            fleet.host_reply({"cleanup": cleanup_request["cleanup"]})
            self.assertEqual(await preview, "screen")
            self.assertEqual(await switch,
                             (("/tmp/tmux/default", 12, 10, "$1"), .001,
                              session.name, "fixture"))
            self.assertIsNone(await cleanup)

        asyncio.run(exercise())

    def test_disconnect_removes_inventory_and_fails_outstanding_requests(self):
        async def exercise():
            fleet = Fleet(); session = self.session()
            fleet.sessions = {"fixture": [session]}
            fleet.graphs = {"fixture": mock.Mock()}
            loop = asyncio.get_running_loop()
            preview = loop.create_future(); switch = loop.create_future()
            cleanup = loop.create_future()
            fleet.previews[1] = ("fixture", preview)
            fleet.switches[2] = ("fixture", switch)
            fleet.cleanups[3] = ("fixture", cleanup)
            await fleet.host_disconnected("fixture", 42, 1)
            self.assertNotIn("fixture", fleet.sessions)
            self.assertNotIn("fixture", fleet.graphs)
            self.assertFalse(fleet.previews); self.assertFalse(fleet.switches)
            self.assertFalse(fleet.cleanups)
            for future in (preview, switch, cleanup):
                with self.assertRaisesRegex(RuntimeError, "fixture disconnected"):
                    await future

        asyncio.run(exercise())

    def test_resident_host_removes_only_its_exact_viewer_marker(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(daemon, "RUNTIME", Path(directory)):
            marker = Path(directory) / "viewer-lovelace-main-fixture.tty"
            other = Path(directory) / "viewer-lovelace-side-fixture.tty"
            marker.write_text("/dev/pts/8\n"); other.write_text("/dev/pts/9\n")
            daemon.remove_viewer_marker("fixture", "lovelace", "main")
            self.assertFalse(marker.exists())
            self.assertTrue(other.exists())
            with self.assertRaisesRegex(ValueError, "invalid"):
                daemon.remove_viewer_marker("fixture", "../escape", "main")


if __name__ == "__main__":
    unittest.main()
