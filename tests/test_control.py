import os
import pty
import fcntl
import queue
import shlex
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_fleet.tmux import ControlClient


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
        for _ in range(10):
            self.control.switch(self.target("source-two"), self.client_name)
            self.control.switch(self.target("source-one"), self.client_name)
        self.assertEqual((self.process.pid, self.viewer.pid), identities)
        self.assertIsNone(self.process.poll())
        self.assertIsNone(self.viewer.poll())

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


class ProtocolCorrelationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
