import json
import socket
import threading
import time
from pathlib import Path

from .config import CONFIG
from .model import ServerRef, Session, SessionRef


def socket_path():
    for path in (CONFIG / "alan-socket", Path("/etc/agent-fleet/alan-socket")):
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


def raw_request(payload):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(socket_path()))
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        line = client.makefile().readline()
    return json.loads(line)


def request(payload):
    result = raw_request(payload)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Alan request failed"))
    return result


class Watcher:
    def __init__(self, changed, consumer=None):
        self.actors = []
        self.available = False
        self.error = None
        self.initialized = threading.Event()
        self._changed = changed
        self._consumer = consumer
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if configured():
            self.initialized.wait(2)

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

def inventory(host, actors):
    source = ServerRef(host, "", 0, 0, "alan")
    sessions = []
    for actor in actors:
        if actor.get("type") not in {"claude", "codex"}:
            continue
        attachment = actor.get("attachment") or {"kind": "none"}
        if attachment.get("kind") == "none":
            continue
        state = actor.get("state", "live")
        reported_state = ("working" if state in {"busy", "working"} else
                          "needs-action" if state == "needs-action" else "waiting")
        sessions.append(Session(
            SessionRef(source, actor["addr"]), actor.get("label") or actor["addr"],
            actor.get("created", 0), 0, 0, 1, attachment.get("kind", "alan"),
            actor.get("label", ""),
            actor.get("cwd") or "",
            actor.get("type", "alan"), reported_state,
            "", 0, (actor.get("native") or {}).get("id", ""), attachment,
            actor.get("human_activity", 0)))
    return sessions


def spawn_codex(label, cwd):
    return request({"op": "spawn", "source": "codex", "label": label, "cwd": cwd})["addr"]


def spawn_claude(label, cwd):
    return request({"op": "spawn", "source": "claude", "label": label, "cwd": cwd})["addr"]


def actors():
    return request({"op": "list"})["actors"] if configured() else []


def retire(addr):
    request({"op": "retire", "addr": addr})


def resume(addr):
    actor = next((item for item in actors() if item["addr"] == addr), None)
    if not actor:
        raise RuntimeError(f"Alan actor disappeared: {addr}")
    native_id = (actor.get("native") or {}).get("id")
    if actor.get("type") not in {"claude", "codex"} or not native_id:
        raise RuntimeError("open requires a durable Claude or Codex identity")
    result = request({"op": "spawn", "source": addr})
    if result["addr"] != addr:
        raise RuntimeError(f"Alan open changed actor identity: {addr}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        actor = next((item for item in actors() if item["addr"] == addr), None)
        if actor:
            attachment = actor.get("attachment") or {"kind": "none"}
            if ((actor.get("native") or {}).get("id") == native_id and
                    attachment.get("kind") != "none"):
                return addr
        time.sleep(.1)
    raise RuntimeError(f"Alan open did not restore native identity for {addr}")


def rename(addr, label):
    request({"op": "rename", "addr": addr, "label": label})


def refresh(addr):
    actors = request({"op": "list"})["actors"]
    actor = next((item for item in actors if item["addr"] == addr), None)
    if not actor:
        raise RuntimeError(f"Alan actor disappeared: {addr}")
    if actor.get("type") not in {"claude", "codex"}:
        raise RuntimeError(f"refresh does not support {actor.get('type')}")
    native_id = (actor.get("native") or {}).get("id")
    if not native_id:
        raise RuntimeError("refresh requires a durable native identity")
    result = request({"op": "refresh", "addr": addr})
    if result["addr"] != addr:
        raise RuntimeError(f"Alan refresh changed actor identity: {addr}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        actors = request({"op": "list"})["actors"]
        actor = next((item for item in actors if item["addr"] == addr), None)
        if actor:
            attachment = actor.get("attachment") or {"kind": "none"}
            if ((actor.get("native") or {}).get("id") == native_id and
                    attachment.get("kind") != "none"):
                return
        time.sleep(.1)
    raise RuntimeError(f"Alan refresh did not restore attachment for {addr}")


def attachment_usable(addr, native_id):
    actors = request({"op": "list"})["actors"]
    actor = next((item for item in actors if item["addr"] == addr), None)
    if not actor:
        return False
    attachment = actor.get("attachment") or {"kind": "none"}
    return ((actor.get("native") or {}).get("id") == native_id and
            attachment.get("kind") != "none")
