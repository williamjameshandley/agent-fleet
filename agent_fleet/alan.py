import fcntl
import hashlib
import json
import os
import threading
import time
import textwrap
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import loop
import networkx as nx

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
        self.graph = None
        self.available = False
        self.error = None
        self.initialized = threading.Event()
        self._changed = changed
        self._consumer = consumer
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.initialized.wait(2)

    def _run(self):
        previous = None
        while not (self._consumer and self._consumer.is_set()):
            try:
                graph = loop.observe()
                current = actors(graph)
                self.actors = current
                self.graph = graph
                self.available = True
                self.error = None
                self.initialized.set()
                snapshot = (
                    graph.graph,
                    tuple(graph.nodes(data=True)),
                    tuple(graph.edges(keys=True, data=True)),
                )
                if snapshot != previous:
                    self._changed.put("alan")
                    previous = snapshot
            except (loop.LoopError, OSError, ValueError) as error:
                self._unavailable(f"Alan unavailable: {error}")
                previous = None
            if self._consumer:
                self._consumer.wait(0.5)
            else:
                time.sleep(0.5)

    def _unavailable(self, error):
        self.error = error
        self.initialized.set()
        if self.available or self.actors or self.graph is not None:
            self.available = False
            self.actors = []
            self.graph = None
            self._changed.put("alan")


def _position(reference):
    return int(reference.rsplit("#", 1)[1])


def _timestamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def provider_identity(addr, kind):
    if kind not in {"claude", "codex"}:
        return ""
    binding = native_dir(addr) / "thread_id"
    if binding.exists():
        return binding.read_text().strip()
    return address_identity(addr, kind)


def address_identity(addr, kind):
    if kind not in {"claude", "codex"}:
        return ""
    return addr.split("-", 1)[1].rsplit("@", 1)[0]


def actors(graph=None):
    if graph is None:
        graph = loop.observe()
    all_descriptors = {actor["addr"]: dict(actor)
                       for actor in graph.graph.get("actors", [])}
    descriptors = {addr: actor for addr, actor in all_descriptors.items()
                   if actor["kind"] != "principal"}
    operations = {actor: [] for actor in descriptors}
    for reference, operation in graph.nodes(data=True):
        if "stream" in operation:
            operations.setdefault(operation["stream"], []).append((reference, operation))

    for addr, descriptor in descriptors.items():
        stream = sorted(operations.get(addr, ()), key=lambda item: _position(item[0]))
        active = None
        active_started = 0
        native = None
        human_activity = 0
        last_output = None
        for reference, operation in stream:
            if operation["op"] == "evaluation":
                active = reference
                active_started = _timestamp(operation["time"])
            elif operation["op"] == "output":
                active = None
                active_started = 0
                last_output = operation
                if evidence := operation.get("native"):
                    native = evidence
            elif operation["op"] == "input" and "send" in operation:
                source = graph.nodes.get(operation["send"], {})
                source_actor = all_descriptors.get(source.get("stream"), {})
                if source_actor.get("kind") == "principal":
                    human_activity = _timestamp(operation["time"])

        descriptor["created"] = _timestamp(stream[0][1]["time"]) if stream else 0
        descriptor["label"] = label(addr)
        descriptor["native_id"] = provider_identity(addr, descriptor["kind"])
        working = descriptor["state"] == "working"
        descriptor["active_evaluation"] = active if working else None
        descriptor["evaluation_started"] = active_started if working else 0
        descriptor["human_activity"] = human_activity
        if native:
            descriptor["native"] = native
        if last_output and last_output.get("status") == "error":
            descriptor["last_error"] = last_output.get("error", "")
        elif last_output and isinstance(last_output.get("value"), str):
            descriptor["summary"] = " ".join(last_output["value"].split())
    return list(descriptors.values())


def inventory(host, actor_descriptors):
    source = ServerRef(host, "", 0, 0, "alan")
    sessions = []
    for actor in actor_descriptors:
        if actor["state"] in {"retired", "unavailable"}:
            continue
        native = actor.get("native") or {}
        transcript_id = actor.get("native_id") or provider_identity(
            actor["addr"], actor["kind"])
        transcript_path = native.get("path", "") if transcript_id else ""
        sessions.append(Session(
            SessionRef(source, actor["addr"]), actor.get("label") or label(actor["addr"]),
            actor["created"], 0, 0, 1, "alan", "",
            actor.get("cwd") or "", actor["kind"], actor["state"],
            actor.get("summary") or actor.get("last_error", ""), 0,
            transcript_id,
            actor["human_activity"], actor.get("active_evaluation") or "",
            actor["evaluation_started"], transcript_path))
    return sessions


def project(sessions, graph, expanded=(), show_python=False):
    descriptors = {actor["addr"]: actor for actor in graph.graph.get("actors", [])}
    ancestry = nx.DiGraph()
    ancestry.add_nodes_from(descriptors)
    for source, target, relation in graph.edges(keys=True):
        if relation != "spawn":
            continue
        child = graph.nodes[target]["stream"]
        parent = graph.nodes[source].get("stream") or source.rsplit("#", 1)[0]
        ancestry.add_edge(parent, child)

    actor_sessions = {
        session.ref.session_id: session
        for session in sessions
        if session.ref.server.kind == "alan"
    }
    expanded = set(expanded)
    children = {}
    eligible = set()
    session_order = [session.ref.session_id for session in sessions
                     if session.ref.server.kind == "alan"]
    principals = {addr for addr, descriptor in descriptors.items()
                  if descriptor["kind"] == "principal"}
    roots = {}
    for actor in session_order:
        candidates = nx.ancestors(ancestry, actor) & principals
        [principal] = candidates
        first = nx.shortest_path(ancestry, principal, actor)[1]
        if descriptors[first].get("preset") == "commander" or (
            descriptors[first]["kind"] == "python" and not show_python
        ):
            continue
        roots[actor] = principal

    visible = [actor for actor in session_order
               if actor in roots
               and descriptors[actor].get("preset") != "commander"
               and (descriptors[actor]["kind"] != "python" or show_python)]
    visible_set = set(visible)
    for actor in visible:
        candidates = nx.ancestors(ancestry, actor) & visible_set
        if candidates:
            parent = max(candidates, key=lambda item: nx.shortest_path_length(
                ancestry, roots[actor], item))
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
    for source, request in _outstanding_requests(graph, descriptors):
        candidates = (nx.ancestors(ancestry, source) | {source}) & emitted
        if not candidates:
            continue
        anchor = min(candidates, key=lambda actor: nx.shortest_path_length(
            ancestry, actor, source))
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
        actor = session.ref.session_id
        if actor in eligible:
            result.extend(emit(actor, 0))
    return result


def _outstanding_requests(graph, descriptors):
    principals = {addr for addr, actor in descriptors.items()
                  if actor["kind"] == "principal"}
    replied = {source for source, _target, relation in graph.edges(keys=True)
               if relation == "reply"}
    requests = []
    for reference, operation in graph.nodes(data=True):
        if operation.get("op") != "send" or operation.get("to") not in principals \
                or operation.get("reply") or reference in replied:
            continue
        accepted = any(
            relation == "send"
            and graph.nodes[target].get("stream") == operation["to"]
            for _source, target, relation in graph.out_edges(reference, keys=True)
        )
        if not accepted:
            continue
        payload = operation.get("payload", "")
        if isinstance(payload, dict):
            payload = payload.get("text", payload.get("code", payload))
        preview = " ".join(
            (json.dumps(payload, sort_keys=True) if not isinstance(payload, str)
             else payload).split()
        )
        requests.append((operation["stream"],
                         (operation["time"], _timestamp(operation["time"]), preview)))
    return requests


def create(kind, name, cwd):
    addr = loop.spawn({"kind": kind, "cwd": cwd})
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


def wait_output(input_reference):
    actor = input_reference.rsplit("#", 1)[0]
    while True:
        graph = loop.observe()
        stream = sorted(
            ((reference, operation)
             for reference, operation in graph.nodes(data=True)
             if operation.get("stream") == actor),
            key=lambda item: _position(item[0]),
        )
        inputs = [reference for reference, operation in stream
                  if operation["op"] == "input"]
        ordinal = inputs.index(input_reference)
        outputs = [operation for _, operation in stream
                   if operation["op"] == "output"]
        if len(outputs) > ordinal:
            return outputs[ordinal]
        time.sleep(0.1)


def preview(addr, columns=0, lines=0):
    graph = loop.observe()
    operations = sorted(
        ((reference, operation)
         for reference, operation in graph.nodes(data=True)
         if operation.get("stream") == addr and operation["op"] in {"input", "output"}),
        key=lambda item: _position(item[0]),
    )
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
        commanders = [
            actor["addr"]
            for actor in loop.observe().graph.get("actors", [])
            if actor.get("preset") == "commander"
        ]
        if len(commanders) > 1:
            raise RuntimeError("multiple Commander actors")
        if commanders:
            return commanders[0]
        return loop.spawn({"kind": "llm", "preset": "commander"})


def _label_path(addr):
    return _state_dir() / "labels" / addr


def _state_dir():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-fleet"
