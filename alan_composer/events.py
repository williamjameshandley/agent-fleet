import json
import os
import socket
import threading


class EventServer:
    def __init__(self, path, event):
        self.path = path
        self.event = event
        self.mode = "closed"
        self.client = None
        self.lock = threading.Lock()

    def serve(self):
        self.path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen()
            while True:
                client, _ = server.accept()
                with client:
                    with self.lock:
                        self.client = client
                        self._send_mode()
                    self.event({"type": "status", "status": "Voice connected"})
                    with client.makefile() as stream:
                        for line in stream:
                            event = json.loads(line)
                            self._validate(event)
                            self.event(event)
                    with self.lock:
                        if self.client is client:
                            self.client = None
                    self.event({"type": "status", "status": "Voice disconnected"})

    @staticmethod
    def _validate(event):
        if event["type"] not in {"revision", "open", "action", "final", "status"}:
            raise ValueError(f"unknown voice event: {event['type']!r}")
        if (event["type"] == "action"
                and event["action"] not in {
                    "dictate", "edit", "pause", "resume", "send", "cancel"}):
            raise ValueError(f"unknown voice action: {event['action']!r}")

    def set_mode(self, mode):
        with self.lock:
            self.mode = mode
            if self.client is not None:
                try:
                    self._send_mode()
                except OSError:
                    self.client = None

    def _send_mode(self):
        self.client.sendall((json.dumps({
            "type": "mode", "state": self.mode,
        }) + "\n").encode())
