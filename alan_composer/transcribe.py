import json
import os
import queue
import socket
import threading
import tomllib
import urllib.request


HOST = os.environ.get("ALAN_HOME_HOST", "lovelace")
PORT = int(os.environ.get("ALAN_HOME_AUDIO_PORT", "10300"))
AUTH_URL = os.environ.get(
    "ALAN_HOME_AUTH_URL",
    "http://lovelace:8083/auth/composer-boltzmann.toml",
)


class Transcriber:
    """Full-duplex client for Alan Home's PCM-to-utterance boundary."""

    def __init__(self, utterance):
        self.utterance = utterance
        self.audio = None
        self.socket = None

    def start(self, preroll=()):
        auth = tomllib.loads(urllib.request.urlopen(AUTH_URL).read().decode())
        self.audio = queue.Queue()
        self.socket = socket.create_connection((HOST, PORT))
        header = {
            "protocol": "alan-audio/2",
            "endpoint_id": auth["endpoint_id"],
            "auth_token": auth["auth_token"],
            "codec": "s16le",
            "rate": 16000,
            "channels": 1,
        }
        self.socket.sendall((json.dumps(header) + "\n").encode())
        reader = self.socket.makefile()
        ack = json.loads(reader.readline())
        if not ack["ok"]:
            raise RuntimeError(ack["error"])
        threading.Thread(
            target=self._write, args=(self.socket, self.audio), daemon=True).start()
        threading.Thread(target=self._read, args=(reader,), daemon=True).start()
        for block in preroll:
            self.feed(block)

    def feed(self, block):
        if self.socket:
            self.audio.put(block.tobytes())

    def stop(self):
        if self.socket:
            self.audio.put(None)
            self.socket = None

    def _write(self, sock, audio):
        for block in iter(audio.get, None):
            sock.sendall(block)
        sock.shutdown(socket.SHUT_WR)

    def _read(self, reader):
        with reader:
            for line in reader:
                self.utterance(json.loads(line)["text"])
