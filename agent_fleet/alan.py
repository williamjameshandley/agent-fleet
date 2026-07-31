import fcntl
import hashlib
import os
import threading
import time
import textwrap
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import loop
import networkx as nx

from .model import ServerRef, Session, SessionRef


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


def actors(graph=None):
    if graph is None:
        graph = loop.observe()
    descriptors = {actor["addr"]: dict(actor)
                   for actor in graph.graph.get("actors", [])}
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
            elif operation["op"] == "input" and "sender" in operation:
                human_activity = _timestamp(operation["time"])

        descriptor["created"] = _timestamp(stream[0][1]["time"]) if stream else 0
        descriptor["label"] = label(addr)
        descriptor["active_evaluation"] = active
        descriptor["evaluation_started"] = active_started
        descriptor["human_activity"] = human_activity
        if native:
            descriptor["native"] = {
                **native,
                "id": native.get("thread_id") or native.get("session_id", ""),
            }
        if descriptor["state"] == "live":
            descriptor["state"] = "working" if active else "waiting"
        if last_output and last_output.get("status") == "error":
            descriptor["last_error"] = last_output.get("error", "")
    return list(descriptors.values())


def inventory(host, actor_descriptors):
    source = ServerRef(host, "", 0, 0, "alan")
    sessions = []
    for actor in actor_descriptors:
        if actor["state"] in {"retired", "unavailable"}:
            continue
        native = actor.get("native") or {}
        sessions.append(Session(
            SessionRef(source, actor["addr"]), actor.get("label") or label(actor["addr"]),
            actor["created"], 0, 0, 1, "alan", "",
            actor.get("cwd") or "", actor["kind"], actor["state"],
            actor.get("last_error", ""), 0, native.get("id", ""),
            actor["human_activity"], actor.get("active_evaluation") or "",
            actor["evaluation_started"]))
    return sessions


def project(sessions, graph, show_language=False, show_python=False):
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
    roots = {}
    for actor in actor_sessions:
        candidates = [
            node for node in nx.ancestors(ancestry, actor) | {actor}
            if ancestry.in_degree(node) == 0
        ]
        [root] = candidates
        roots[actor] = root if root in descriptors else None

    result = []
    for session in sessions:
        if session.ref.server.kind != "alan":
            result.append(session)
            continue
        actor = session.ref.session_id
        descriptor = descriptors[actor]
        if roots[actor] != actor or descriptor.get("preset") == "commander" or (
            descriptor["kind"] == "python" and not show_python
        ):
            continue
        result.append(session)
        for descendant in sessions:
            if descendant.ref.server.kind != "alan":
                continue
            child = descendant.ref.session_id
            if child == actor or roots[child] != actor:
                continue
            kind = descriptors[child]["kind"]
            if descriptors[child].get("preset") == "commander":
                continue
            if (kind == "python" and show_python) or (
                kind in {"llm", "claude", "codex"} and show_language
            ):
                result.append(replace(descendant, name="  ↳ " + descendant.name))
    return result


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


def actor_socket(actor):
    return native_dir(actor).parent.parent / (runtime_name(actor) + ".sock")


def codex_socket(actor):
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "alan" / "codex" / runtime_name(actor) / "codex.sock"


def codex_gateway(actor):
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    cages = Path(os.environ.get("LOOP_CAGE_RUNTIME_DIR", runtime / "alan" / "cages"))
    return cages / (runtime_name(actor) + ".sock")


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
