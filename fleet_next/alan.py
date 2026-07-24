import json
import os
import socket
import threading
import time
from pathlib import Path

from .model import ServerRef, Session, SessionRef


def socket_path():
    if value := os.environ.get("LOOP_SOCKET"):
        path = Path(value)
        if path.is_absolute():
            return path
        raise RuntimeError("Alan socket path must be absolute")
    for path in (Path.home() / ".config/agent-fleet/alan-socket",
                 Path("/etc/agent-fleet/alan-socket")):
        if path.exists():
            configured_path = Path(path.read_text().strip())
            if configured_path.is_absolute():
                return configured_path
            raise RuntimeError(f"Alan socket path in {path} must be absolute")
    raise RuntimeError("Alan socket is not configured")


def configured():
    try:
        socket_path()
        return True
    except RuntimeError:
        return False


def request(payload):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(socket_path()))
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        line = client.makefile().readline()
    result = json.loads(line)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Alan request failed"))
    return result


class Watcher:
    def __init__(self, changed, consumer=None):
        self.actors = []
        self.attention = {}
        self.available = False
        self.error = None
        self.initialized = threading.Event()
        self._changed = changed
        self._consumer = consumer
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if configured():
            self.initialized.wait(2)
            self._attention_thread = threading.Thread(
                target=self._run_attention, daemon=True)
            self._attention_thread.start()

    def _run(self):
        if not configured():
            return
        while not (self._consumer and self._consumer.is_set()):
            try:
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(socket_path()))
                    client.sendall(b'{"op":"watch"}\n')
                    stream = client.makefile()
                    for line in stream:
                        message = json.loads(line)
                        if not message.get("ok"):
                            raise RuntimeError(message.get("error", "Alan watch failed"))
                        self.actors = message["actors"]
                        self.available = True
                        self.error = None
                        self.initialized.set()
                        self._changed.put("alan")
                        if self._consumer and self._consumer.is_set():
                            return
            except RuntimeError as error:
                self._unavailable(str(error))
            except (ConnectionError, FileNotFoundError, json.JSONDecodeError, OSError):
                self._unavailable(f"Alan unavailable at {socket_path()}")

    def _unavailable(self, error):
        self.error = error
        self.initialized.set()
        if self.available or self.actors:
            self.available = False
            self.actors = []
            self._changed.put("alan")
        time.sleep(1)

    def _run_attention(self):
        if not configured():
            return
        while not (self._consumer and self._consumer.is_set()):
            after = -1
            reconstructed = {}
            replaying = True
            try:
                while not (self._consumer and self._consumer.is_set()):
                    result = request({"op": "tail", "addr": "fleet", "after": after,
                                      "limit": 100, "wait_ms": 1000})
                    messages = result["messages"]
                    changed = False
                    for message in messages:
                        after = max(after, message["idx"])
                        payload = message.get("payload", {})
                        if payload.get("kind") != "fleet_attention":
                            continue
                        addr = payload.get("actor")
                        attention = payload.get("attention")
                        if isinstance(addr, str) and attention in {"tracked", "done"}:
                            reconstructed[addr] = attention
                            changed = True
                    replay_complete = len(messages) < 100
                    if ((replaying and replay_complete and
                         reconstructed != self.attention) or
                            (not replaying and changed)):
                        self.attention = dict(reconstructed)
                        self._changed.put("alan-attention")
                    if replay_complete:
                        replaying = False
            except (ConnectionError, FileNotFoundError, json.JSONDecodeError,
                    OSError, RuntimeError, KeyError, TypeError):
                if self.attention:
                    self.attention = {}
                    self._changed.put("alan-attention")
                time.sleep(1)


def inventory(host, actors, attention=None, viewer_activity=None):
    source = ServerRef(host, "", 0, 0, "alan")
    attention = attention or {}
    viewer_activity = viewer_activity or {}
    sessions = []
    for actor in actors:
        attachment = actor.get("attachment") or {"kind": "none"}
        if attachment.get("kind") == "none":
            continue
        state = actor.get("state", "live")
        sessions.append(Session(
            SessionRef(source, actor["addr"]), actor.get("label") or actor["addr"],
            0, 0, 0, 1, attachment.get("kind", "alan"), actor.get("label", ""),
            actor.get("cwd") or "", attention.get(actor["addr"], "tracked"),
            actor.get("type", "alan"),
            "working" if state in {"busy", "working"} else "waiting",
            "", 0, (actor.get("native") or {}).get("id", ""), attachment,
            viewer_activity.get(attachment.get("session", ""), 0)))
    return sessions


def spawn_python(label, cwd):
    return request({"op": "spawn", "source": "python", "label": label, "cwd": cwd})["addr"]


def spawn_codex(label, cwd):
    return request({"op": "spawn", "source": "codex", "label": label, "cwd": cwd})["addr"]


def spawn_claude(label, cwd):
    return request({"op": "spawn", "source": "claude", "label": label, "cwd": cwd})["addr"]


def rename(addr, label):
    request({"op": "rename", "addr": addr, "label": label})


def set_attention(addr, attention):
    if attention not in {"tracked", "done"}:
        raise ValueError(f"invalid Fleet attention {attention!r}")
    request({"op": "send", "to": "fleet", "payload": {
        "kind": "fleet_attention", "actor": addr, "attention": attention}})
