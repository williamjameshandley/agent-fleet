import threading
import time

import loop

from .model import ServerRef, Session, SessionRef


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
        self.initialized.wait(2)

    def _run(self):
        while not (self._consumer and self._consumer.is_set()):
            try:
                for actors in loop.watch():
                    self.actors = actors
                    self.available = True
                    self.error = None
                    self.initialized.set()
                    self._changed.put("alan")
                    if self._consumer and self._consumer.is_set():
                        return
                self._unavailable("Alan watch closed")
            except (loop.LoopError, OSError, ValueError) as error:
                self._unavailable(f"Alan unavailable: {error}")

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
        if (actor.get("type") not in {"python", "claude", "codex"} or
                actor.get("profile") is not None):
            continue
        state = actor.get("state", "live")
        if state in {"retired", "failed"}:
            continue
        attachment = actor.get("attachment") or {"kind": "none"}
        reported_state = ("working" if state in {"busy", "working", "starting"} else
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


def actors():
    return loop.list()


def retire(addr):
    loop.retire(addr)


def resume(addr):
    actor = next((item for item in actors() if item["addr"] == addr), None)
    if not actor:
        raise RuntimeError(f"Alan actor disappeared: {addr}")
    native_id = (actor.get("native") or {}).get("id")
    if actor.get("type") not in {"claude", "codex"} or not native_id:
        raise RuntimeError("open requires a durable Claude or Codex identity")
    if loop.spawn(addr) != addr:
        raise RuntimeError(f"Alan open changed actor identity: {addr}")
    return addr


def rename(addr, label):
    loop.rename(addr, label)


def refresh(addr):
    actor = next((item for item in actors() if item["addr"] == addr), None)
    if not actor:
        raise RuntimeError(f"Alan actor disappeared: {addr}")
    if actor.get("type") not in {"claude", "codex"}:
        raise RuntimeError(f"refresh does not support {actor.get('type')}")
    if not (actor.get("native") or {}).get("id"):
        raise RuntimeError("refresh requires a durable native identity")
    loop.refresh(addr)


def present(addr):
    return loop.present(addr)


def attachment(addr):
    return loop.attachment(addr)


def native_identity_usable(addr, native_id):
    actor = next((item for item in actors() if item["addr"] == addr), None)
    if not actor:
        return False
    return ((actor.get("native") or {}).get("id") == native_id and
            actor.get("state") in {"waiting", "working", "needs-action"})


def peer():
    return loop.peer()


def commander_request(request):
    return loop.commander_request(request)
