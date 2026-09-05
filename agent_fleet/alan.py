import fcntl
import hashlib
import json
import os
import threading
import time
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import loop
from watchfiles import watch

from .model import ServerRef, Session, SessionRef


@dataclass(frozen=True)
class Projected:
    session: Session
    depth: int
    child_count: int
    expanded: bool


class Watcher:
    def __init__(self, changed, consumer=None):
        self.actors = []
        self._descriptors = {}
        self.available = False
        self.error = None
        self.initialized = threading.Event()
        self._changed = changed
        self._consumer = consumer
        self._lock = threading.RLock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._labels = threading.Thread(target=self._watch_labels, daemon=True)
        self._labels.start()
        self.initialized.wait(2)

    def _run(self):
        while not (self._consumer and self._consumer.is_set()):
            stream = None
            try:
                stream = loop.observe(stream=True, actors=True)
                while True:
                    catalogue_changed = False

                    def refresh(observation, change):
                        nonlocal catalogue_changed
                        catalogue_changed = self.refresh(observation, change)

                    stream.next(refresh, self._lock)
                    if catalogue_changed:
                        self._changed.put("alan")
                    if self._consumer and self._consumer.is_set():
                        return
            except StopIteration:
                return
            except (loop.LoopError, OSError, ValueError) as error:
                with self._lock:
                    self._unavailable(f"Alan unavailable: {error}")
                if self._consumer:
                    self._consumer.wait(0.5)
                else:
                    time.sleep(0.5)
            finally:
                if stream is not None:
                    stream.close()

    def refresh(self, actors, change):
        with self._lock:
            descriptors = {
                actor["addr"]: canonical_actor(actor) for actor in actors
            }
            changed = descriptors != self._descriptors or not self.available
            self._descriptors = descriptors
            self.actors = list(self._descriptors.values())
            self.available = True
            self.error = None
            self.initialized.set()
            return changed

    def _watch_labels(self):
        directory = _state_dir() / "labels"
        directory.mkdir(parents=True, exist_ok=True)
        for changes in watch(directory, stop_event=self._consumer):
            addresses = {Path(path).name for _event, path in changes}
            with self._lock:
                changed = False
                for addr in addresses:
                    if addr not in self._descriptors:
                        continue
                    value = label(addr)
                    if self._descriptors[addr].get("label") != value:
                        self._descriptors[addr]["label"] = value
                        changed = True
                if changed:
                    self.actors = list(self._descriptors.values())
            if changed:
                self._changed.put("alan")

    @contextmanager
    def snapshot(self):
        with self._lock:
            yield self.actors

    def _unavailable(self, error):
        self.error = error
        self.initialized.set()
        if self.available or self.actors:
            self.available = False
            self.actors = []
            self._changed.put("alan")


def _position(reference):
    return int(reference.rsplit("#", 1)[1])


def _timestamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def address_identity(addr, kind):
    if kind not in {"claude", "codex", "grok"}:
        return ""
    if addr.startswith("alan:"):
        addr = addr.split(":", 2)[2]
    return addr.split("-", 1)[1].rsplit("@", 1)[0]


ACTOR_FIELDS = {
    "addr", "kind", "state", "hibernation", "cwd", "evaluator", "preset",
    "spawn", "label", "managed", "worked", "created", "last_operation_activity",
    "evaluation_started", "active_evaluation", "latest_displayable_output",
    "source_activity", "unresolved_requests",
}


def canonical_actor(descriptor):
    defaults = {field: None for field in ACTOR_FIELDS}
    defaults.update(managed=False, worked=False, source_activity={},
                    unresolved_requests={})
    defaults.update({field: descriptor[field] for field in ACTOR_FIELDS - {"label"}
                     if field in descriptor})
    defaults["label"] = label(descriptor["addr"])
    return defaults


def inventory(source, actor_descriptors, principals=None):
    source = ServerRef(source, "", 0, 0, "alan")
    principals = principals or set()
    sessions = []
    for actor in actor_descriptors:
        if actor["kind"] == "principal":
            continue
        if (actor["state"] in {"retired", "unavailable"}
                and actor.get("evaluator") != "native"):
            continue
        output = actor["latest_displayable_output"] or {}
        summary = output.get("value", output.get("error", ""))
        human_activity = max((_timestamp(time) for addr, time in
                              actor["source_activity"].items() if addr in principals),
                             default=0)
        transcript_id = address_identity(actor["addr"], actor["kind"])
        sessions.append(Session(
            SessionRef(source, actor["addr"]), actor.get("label") or actor["addr"],
            _timestamp(actor["created"]), 0, 0, 1, "alan", "",
            actor.get("cwd") or "", actor["kind"], actor["state"],
            " ".join(summary.split()),
            _timestamp(actor["last_operation_activity"]),
            transcript_id,
            human_activity, actor.get("active_evaluation") or "",
            _timestamp(actor["evaluation_started"]) if actor["evaluation_started"] else 0, "",
            worked=actor["worked"],
            evaluator=actor.get("evaluator", ""),
            managed=actor.get("managed", False),
            hibernation=actor["hibernation"] or "unsupported"))
    return sessions


def project(sessions, descriptors, expanded=(), show_python=False):
    """Fold centrally qualified actor catalogue entries into Muster rows."""
    raw_index = {}
    for key, descriptor in descriptors.items():
        addr = descriptor["addr"]
        if addr in raw_index:
            raise RuntimeError(f"multiple Alan runtime sources claim {addr}")
        raw_index[addr] = key
    parents = {}
    for key, descriptor in descriptors.items():
        spawn = descriptor.get("spawn")
        if spawn:
            raw_parent = spawn.rsplit("#", 1)[0]
            if raw_parent in raw_index:
                parents[key] = raw_index[raw_parent]

    def ancestors(actor):
        result = []
        seen = {actor}
        while actor in parents:
            actor = parents[actor]
            if actor in seen:
                raise RuntimeError("cycle in Alan actor ancestry")
            seen.add(actor)
            result.append(actor)
        return result

    def catalogue_actor(session):
        return session.ref.key

    actor_sessions = {
        catalogue_actor(session): session
        for session in sessions
        if session.ref.server.kind == "alan"
    }
    expanded = set(expanded)
    children = {}
    eligible = set()
    session_order = [catalogue_actor(session) for session in sessions
                     if session.ref.server.kind == "alan"
                     and (show_python or session.agent != "python"
                          or session.state == "hibernated")]
    principals = {addr for addr, descriptor in descriptors.items()
                  if descriptor["kind"] == "principal"}
    native_roots = {addr for addr, descriptor in descriptors.items()
                    if descriptor.get("evaluator") == "native"}
    roots = {}
    for actor in session_order:
        lineage = ancestors(actor)
        candidates = set(lineage) & principals
        if not candidates:
            terminal = lineage[-1] if lineage else actor
            if (set(lineage) | {actor}) & native_roots:
                roots[actor] = actor
            elif descriptors[terminal].get("spawn") and terminal not in parents:
                roots[actor] = terminal
            continue
        principal = next(item for item in lineage if item in candidates)
        path = list(reversed(lineage[:lineage.index(principal)])) + [actor]
        first = path[0]
        if descriptors[first].get("preset") == "commander" or (
            descriptors[first]["kind"] == "python" and not show_python
        ):
            continue
        roots[actor] = principal

    visible = [actor for actor in session_order
               if actor in roots
               and descriptors[actor].get("preset") != "commander"
               and (descriptors[actor]["kind"] != "python" or show_python
                    or descriptors[actor]["state"] == "hibernated")]
    visible_set = set(visible)
    for actor in visible:
        candidates = set(ancestors(actor)) & visible_set
        if candidates:
            parent = next(item for item in ancestors(actor) if item in candidates)
            children.setdefault(parent, []).append(actor)
        else:
            eligible.add(actor)
        children.setdefault(actor, [])

    def visible(actor):
        result = [actor]
        if actor in expanded:
            for child in children[actor]:
                result.extend(visible(child))
        return result

    emitted = set()
    for root in eligible:
        emitted.update(visible(root))

    attention = {}
    for source, target, request in _outstanding_requests(descriptors, raw_index):
        candidates = (set(ancestors(source)) | {source}) & emitted
        if not candidates:
            continue
        anchor = next(actor for actor in [source] + ancestors(source) if actor in candidates)
        if roots[anchor] != target:
            continue
        attention.setdefault(anchor, []).append(request)

    def emit(actor, depth):
        session = actor_sessions[actor]
        requests = attention.get(actor, ())
        if requests:
            latest = max(requests, key=lambda request: request[0])
            session = replace(
                session,
                reported_state="needs-action",
                summary=f"{len(requests)} awaiting — {latest[2]}",
                human_activity=max(session.human_activity, latest[1]),
            )
        descendants = children[actor]
        yield Projected(session, depth, len(descendants), actor in expanded)
        if actor in expanded:
            for child in descendants:
                yield from emit(child, depth + 1)

    result = []
    for session in sessions:
        if session.ref.server.kind != "alan":
            result.append(Projected(session, 0, 0, False))
            continue
        actor = catalogue_actor(session)
        if actor in eligible:
            result.extend(emit(actor, 0))
    return result


def _outstanding_requests(descriptors, raw_index):
    requests = []
    for target, descriptor in descriptors.items():
        if descriptor["kind"] != "principal":
            continue
        for reference, request in descriptor["unresolved_requests"].items():
            source = raw_index.get(reference.rsplit("#", 1)[0])
            if source is None:
                continue
            payload = request["payload"]
            if isinstance(payload, dict):
                payload = payload.get("text", payload.get("code", payload))
            preview = " ".join((json.dumps(payload, sort_keys=True)
                                if not isinstance(payload, str) else payload).split())
            requests.append((source, target, (request["time"],
                            _timestamp(request["time"]), preview)))
    return requests


def create(kind, name, cwd):
    spec = {"kind": kind, "cwd": cwd}
    if kind == "claude":
        spec["name"] = name
    addr = loop.spawn(spec)
    rename(addr, name)
    return addr


def native_dir(actor):
    state = Path(os.environ.get("LOOP_STORE_DIR",
                               Path(os.environ.get("XDG_STATE_HOME",
                                                   Path.home() / ".local/state")) / "alan"))
    return state / "actors" / actor / "native"


def runtime_name(actor):
    return hashlib.sha256(actor.encode()).digest()[:16].hex()


def retire(addr):
    loop.control(addr, "retire")


def hibernate(addr):
    loop.control(addr, "hibernate")


def verify_pristine(addr):
    observation = loop.observe(stream=True, actor=addr)
    try:
        graph = next(observation)
        for _reference, operation in graph.nodes(data=True):
            if operation.get("stream") == addr and operation.get("op") == "input":
                raise RuntimeError(f"actor has conversational work: {addr}")
    finally:
        observation.close()


def resume(addr):
    loop.control(addr, "resume")
    return addr


def rename(addr, name):
    path = _label_path(addr)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name + "\n")


def label(addr):
    path = _label_path(addr)
    return path.read_text().rstrip("\n") if path.exists() else addr


def wait_output(actor, result_reference, observations=None):
    owned = observations is None
    if owned:
        observations = loop.observe(stream=True, actor=actor)
    try:
        for graph in observations:
            result = graph.nodes.get(result_reference)
            if result is None:
                continue
            if "input" not in result:
                raise RuntimeError(f"delivery reported no input: {result_reference}")
            delivered = result["input"]
            stream = sorted(
                ((reference, operation)
                 for reference, operation in graph.nodes(data=True)
                 if operation.get("stream") == actor),
                key=lambda item: _position(item[0]),
            )
            inputs = [reference for reference, operation in stream
                      if operation["op"] == "input"]
            if delivered not in inputs:
                continue
            ordinal = inputs.index(delivered)
            outputs = [operation for _, operation in stream
                       if operation["op"] == "output"]
            if len(outputs) > ordinal:
                return outputs[ordinal]
    finally:
        if owned:
            observations.close()


def preview(addr, columns=0, lines=0):
    observation = loop.observe(stream=True, actor=addr)
    try:
        graph = next(observation)
        operations = sorted(
            ((reference, operation)
             for reference, operation in graph.nodes(data=True)
             if operation.get("stream") == addr and operation["op"] in {"input", "output"}),
            key=lambda item: _position(item[0]),
        )
    finally:
        observation.close()
    rendered = []
    for _reference, operation in operations:
        if operation["op"] == "input":
            payload = operation["payload"]
            value = payload.get("text", payload.get("code", payload)) \
                if isinstance(payload, dict) else payload
            rendered.append(f"Input\n{value}")
        else:
            rendered.append(
                f"{operation['status'].capitalize()}\n"
                f"{operation.get('value', operation.get('error', ''))}"
            )
    text = "\n\n".join(rendered)
    if columns:
        text = "\n".join(
            line if not line else "\n".join(textwrap.wrap(
                line, width=columns, replace_whitespace=False,
                drop_whitespace=False))
            for line in text.splitlines())
    if lines:
        text = "\n".join(text.splitlines()[-lines:])
    return text + ("\n" if text else "")


def commander_actor():
    path = _state_dir() / "commander.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        observation = loop.observe(stream=True, actors=True)
        try:
            actors = next(observation)
        finally:
            observation.close()
        commanders = [actor["addr"] for actor in actors
                      if actor.get("preset") == "commander"
                      and actor.get("state") != "retired"]
        if len(commanders) > 1:
            raise RuntimeError("multiple Commander actors")
        if commanders:
            return commanders[0]
        return loop.spawn({"kind": "llm", "preset": "commander"})


def _label_path(addr):
    return _state_dir() / "labels" / addr


def _state_dir():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-fleet"
