"""Subprocess tests of the Muster hot commands at the real entry point."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROHIBITED = ("agent_fleet.actions", "agent_fleet.ui", "agent_fleet.viewer",
              "agent_fleet.presentation", "networkx", "jupyter_console",
              "IPython", "libtmux", "watchfiles")

DRIVER = """
import json, sys
from agent_fleet.ui_process import main
main(sys.argv[1:])
print(json.dumps(sorted(sys.modules)), file=sys.stderr)
"""


class LineServer(threading.Thread):
    """One-shot line-protocol server mimicking the daemon and viewer sockets."""

    def __init__(self, path, responses):
        super().__init__(daemon=True)
        self.requests = []
        self.responses = responses
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen()
        self.start()

    def run(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                request = client.makefile().readline().rstrip("\n")
                self.requests.append(request)
                client.sendall((self.responses[request] + "\n").encode())


class MusterHTTP(threading.Thread):
    """Minimal fzf --listen stand-in answering curl over the unix socket."""

    def __init__(self, path, state):
        super().__init__(daemon=True)
        self.body = json.dumps(state).encode()
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen()
        self.start()

    def run(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                client.recv(65536)
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "
                               + str(len(self.body)).encode()
                               + b"\r\n\r\n" + self.body)


class HotCommands(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = Path(self.directory.name) / "agent-fleet"
        self.runtime.mkdir()
        self.addCleanup(self.directory.cleanup)
        self.environment = {**os.environ,
                            "XDG_RUNTIME_DIR": self.directory.name,
                            "COLUMNS": "97", "LINES": "24"}

    def invoke(self, *arguments):
        result = subprocess.run(
            [sys.executable, "-c", DRIVER, *arguments],
            capture_output=True, text=True, env=self.environment, check=True)
        modules = json.loads(result.stderr.splitlines()[-1])
        loaded = [name for name in modules
                  if name in PROHIBITED
                  or any(name.startswith(f"{p}.") for p in PROHIBITED)]
        self.assertEqual(loaded, [], "hot path imported heavy modules")
        return result.stdout

    def test_items_requests_the_client_width_and_prints_rendered_rows(self):
        daemon = LineServer(self.runtime / "fleet.sock",
                            {"items 97": "actor:one\trow one\nactor:two\trow two"})
        output = self.invoke("items")
        self.assertEqual(daemon.requests, ["items 97"])
        self.assertEqual(output, "actor:one\trow one\nactor:two\trow two\n")

    def test_empty_items_emit_no_bytes(self):
        daemon = LineServer(self.runtime / "fleet.sock", {"items 97": ""})
        output = self.invoke("items")
        self.assertEqual(daemon.requests, ["items 97"])
        self.assertEqual(output, "")

    def test_header_is_served_verbatim_from_the_daemon(self):
        daemon = LineServer(self.runtime / "fleet.sock",
                            {"header": "Claude usage\nOpenAI usage\ncolumns"})
        output = self.invoke("header")
        self.assertEqual(daemon.requests, ["header"])
        self.assertEqual(output, "Claude usage\nOpenAI usage\ncolumns\n")

    def test_preview_preserves_key_columns_and_lines_framing(self):
        key = "lovelace:/tmp/tmux-1000/default:1:2:$3"
        daemon = LineServer(self.runtime / "fleet.sock",
                            {f"preview {key} 120 30": "captured pane"})
        output = self.invoke("preview", key, "120", "30")
        self.assertEqual(daemon.requests, [f"preview {key} 120 30"])
        self.assertEqual(output, "captured pane\n")

    def test_cursor_places_the_active_main_identity(self):
        LineServer(self.runtime / "viewer-main.sock",
                   {"STATUS": "actor:focused"})
        daemon = LineServer(self.runtime / "fleet.sock",
                            {"cursor actor:focused": "actor:focused"})
        MusterHTTP(self.runtime / "muster.sock",
                   {"matches": [{"text": "actor:first\tfirst"},
                                {"text": "actor:focused\tfocused"}],
                    "matchCount": 2})
        self.assertEqual(self.invoke("cursor"), "pos(2)")
        self.assertEqual(daemon.requests, ["cursor actor:focused"])

    def test_cursor_without_a_viewer_uses_the_daemons_first_waiting(self):
        daemon = LineServer(self.runtime / "fleet.sock",
                            {"cursor": "actor:first"})
        MusterHTTP(self.runtime / "muster.sock",
                   {"matches": [{"text": "actor:first\tfirst"}],
                    "matchCount": 1})
        self.assertEqual(self.invoke("cursor"), "pos(1)")
        self.assertEqual(daemon.requests, ["cursor"])


if __name__ == "__main__":
    unittest.main()
