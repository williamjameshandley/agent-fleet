import asyncio
import json
import os
import pty
import fcntl
import queue
import select
import shlex
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_fleet.tmux import ControlClient, event_stream
from agent_fleet.daemon import Fleet
from agent_fleet import daemon
from agent_fleet.model import ServerRef, SessionRef, Session


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
    def test_authority_barrier_acknowledges_only_after_forced_observation(self):
        changed = queue.Queue()
        consumer = threading.Event()
        process = mock.Mock(stdout=mock.Mock(), stdin=mock.Mock(), stderr=mock.Mock())
        process.poll.return_value = None
        alan = mock.Mock(error=None, actors=["stale"], graph="stale graph")
        alan.snapshot.side_effect = lambda: (alan.actors, alan.graph)
        def refresh():
            alan.actors = ["fresh"]
            alan.graph = "fresh graph"
            return alan.actors, alan.graph
        alan.refresh.side_effect = refresh
        control = mock.Mock()
        tmux = mock.Mock()
        tmux.has_session.return_value = True
        tmux_change = mock.Mock(ref="tmux change")
        with mock.patch("agent_fleet.tmux.AlanWatcher", return_value=alan), \
             mock.patch("agent_fleet.tmux.subprocess.run",
                        return_value=mock.Mock(returncode=0)), \
             mock.patch("agent_fleet.tmux.subprocess.Popen", return_value=process), \
             mock.patch("agent_fleet.tmux.ControlClient", return_value=control), \
             mock.patch("agent_fleet.tmux.server", return_value=tmux), \
             mock.patch("agent_fleet.tmux.inventory",
                        side_effect=[[], [], [tmux_change]]), \
             mock.patch("agent_fleet.tmux.alan_inventory", return_value=[]) as inventory, \
             mock.patch("agent_fleet.tmux.observe",
                        side_effect=lambda current, _catalog: current), \
             mock.patch("agent_fleet.tmux.native_transcripts.catalog",
                        return_value={}):
            stream = event_stream("fixture", consumer, changed=changed)
            self.assertEqual(next(stream), ([], "stale graph"))
            observed = threading.Event()
            changed.put(("authority", observed, True))
            self.assertEqual(next(stream), ([], "fresh graph"))
            alan.refresh.assert_called_once_with()
            self.assertEqual(inventory.call_args.args, ("fixture", ["fresh"]))
            self.assertFalse(observed.is_set())
            changed.put("tmux")
            self.assertEqual(next(stream), ([tmux_change], "fresh graph"))
            self.assertTrue(observed.is_set())
            finished = threading.Event()

            def resume():
                try:
                    next(stream)
                except StopIteration:
                    finished.set()

            thread = threading.Thread(target=resume)
            thread.start()
            self.assertTrue(observed.wait(1))
            consumer.set()
            changed.put("consumer")
            thread.join(1)
            self.assertTrue(finished.is_set())

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
                             (("/tmp/tmux/default", 12, 10, "$1"), .001))
            self.assertIsNone(await cleanup)

        asyncio.run(exercise())

    def test_disconnect_removes_inventory_and_fails_outstanding_requests(self):
        async def exercise():
            fleet = Fleet(); session = self.session()
            fleet.sessions = {"fixture": [session]}
            fleet.observations = {"fixture": b"observation"}
            loop = asyncio.get_running_loop()
            preview = loop.create_future(); switch = loop.create_future()
            cleanup = loop.create_future()
            fleet.previews[1] = ("fixture", preview)
            fleet.switches[2] = ("fixture", switch)
            fleet.cleanups[3] = ("fixture", cleanup)
            await fleet.host_disconnected("fixture")
            self.assertNotIn("fixture", fleet.sessions)
            self.assertNotIn("fixture", fleet.observations)
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
