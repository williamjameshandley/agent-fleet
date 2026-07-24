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
        self.attention = {}
        self.activity_baseline = {}
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
            baselines = {}
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
                            last_touch = payload.get("last_touch")
                            migration_id = payload.get("migration_id")
                            source = payload.get("source")
                            if (isinstance(last_touch, int) and last_touch >= 0 and
                                    isinstance(migration_id, str) and migration_id and
                                    isinstance(source, dict) and
                                    isinstance(source.get("host"), str) and
                                    isinstance(source.get("key"), str) and
                                    payload.get("provider") in {"codex", "claude"} and
                                    isinstance(payload.get("native_id"), str)):
                                current = baselines.get(addr)
                                if current is None or last_touch > current["last_touch"]:
                                    baselines[addr] = {
                                        "last_touch": last_touch,
                                        "provider": payload["provider"],
                                        "native_id": payload["native_id"]}
                            changed = True
                    replay_complete = len(messages) < 100
                    if ((replaying and replay_complete and
                         reconstructed != self.attention) or
                            (not replaying and changed)):
                        self.attention = dict(reconstructed)
                        self.activity_baseline = dict(baselines)
                        self._changed.put("alan-attention")
                    if replay_complete:
                        replaying = False
            except (ConnectionError, FileNotFoundError, json.JSONDecodeError,
                    OSError, RuntimeError, KeyError, TypeError):
                if self.attention:
                    self.attention = {}
                    self.activity_baseline = {}
                    self._changed.put("alan-attention")
                time.sleep(1)


def inventory(host, actors, attention=None, activity_baseline=None):
    source = ServerRef(host, "", 0, 0, "alan")
    attention = attention or {}
    activity_baseline = activity_baseline or {}
    sessions = []
    for actor in actors:
        if actor.get("type") not in {"claude", "codex"}:
            continue
        attachment = actor.get("attachment") or {"kind": "none"}
        if attachment.get("kind") == "none":
            continue
        state = actor.get("state", "live")
        baseline = activity_baseline.get(actor["addr"])
        baseline_epoch = 0
        if (isinstance(baseline, dict) and baseline.get("provider") == actor.get("type") and
                baseline.get("native_id") == (actor.get("native") or {}).get("id")):
            baseline_epoch = baseline.get("last_touch", 0)
        reported_state = ("working" if state in {"busy", "working"} else
                          "needs-action" if state == "needs-action" else "waiting")
        sessions.append(Session(
            SessionRef(source, actor["addr"]), actor.get("label") or actor["addr"],
            actor.get("created", 0), 0, 0, 1, attachment.get("kind", "alan"),
            actor.get("label", ""),
            actor.get("cwd") or "", attention.get(actor["addr"], "tracked"),
            actor.get("type", "alan"), reported_state,
            "", 0, (actor.get("native") or {}).get("id", ""), attachment,
            max(actor.get("human_activity", 0), baseline_epoch)))
    return sessions


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
