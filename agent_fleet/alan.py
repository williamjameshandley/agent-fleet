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
import networkx as nx
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
        self.graph = None
        self.projected = None
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
                stream = loop.observe(stream=True)
                while True:
                    stream.next(self.refresh, self._lock)
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

    def refresh(self, graph, change):
        with self._lock:
            if change["kind"] == "replace":
                self._descriptors = {
                    actor["addr"]: actor for actor in actors(graph)}
                self.projected = projection_graph(graph)
            else:
                raw = {actor["addr"]: actor
                       for actor in graph.graph.get("actors", [])}
                nodes = {}
                for node in change["nodes"]:
                    nodes.setdefault(node["stream"], []).append(node)
                changed = {actor["addr"] for actor in change["actors"]}
                changed.update(nodes)
                for addr in changed:
                    descriptor = raw.get(addr)
                    if descriptor is None or descriptor["kind"] == "principal":
                        self._descriptors.pop(addr, None)
                    else:
                        self._descriptors[addr] = update_actor_descriptor(
                            graph, raw, self._descriptors.get(addr), descriptor,
                            nodes.get(addr, ()))
                apply_projection_delta(self.projected, graph, change)
            current = list(self._descriptors.values())
            self.projected.graph["actors"] = [
                actor for actor in graph.graph.get("actors", [])
                if actor["kind"] == "principal"
            ] + current
            self.actors = current
            self.graph = graph
            self.available = True
            self.error = None
            self.initialized.set()
            return current, graph

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
                    if self.projected is not None:
                        principals = [actor for actor in self.projected.graph.get("actors", [])
                                      if actor["kind"] == "principal"]
                        self.projected.graph["actors"] = principals + self.actors
            if changed:
                self._changed.put("alan")

    @contextmanager
    def snapshot(self):
        with self._lock:
            yield self.actors, self.projected

    @contextmanager
    def full_graph(self):
        with self._lock:
            yield self.graph

    def _unavailable(self, error):
        self.error = error
        self.initialized.set()
        if self.available or self.actors or self.graph is not None:
            self.available = False
            self.actors = []
            self.graph = None
            self.projected = None
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


def actor_address(descriptor):
    return descriptor.get("actor", descriptor["addr"])


def actors(graph=None, operations=None):
    if graph is None:
        graph = loop.observe()
    all_descriptors = {actor["addr"]: dict(actor)
                       for actor in graph.graph.get("actors", [])}
    descriptors = {addr: actor for addr, actor in all_descriptors.items()
                   if actor["kind"] != "principal"}
    operations = operation_index(graph) if operations is None else operations
    return [actor_descriptor(graph, all_descriptors, descriptor,
                             operations.get(addr, ()))
            for addr, descriptor in descriptors.items()]


def operation_index(graph):
    operations = {}
    for reference, operation in graph.nodes(data=True):
        if "stream" in operation:
            operations.setdefault(operation["stream"], []).append((reference, operation))
    return operations


def actor_descriptor(graph, all_descriptors, descriptor, operations):
    descriptor = dict(descriptor)
    addr = descriptor["addr"]
    stream = sorted(operations, key=lambda item: _position(item[0]))
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
            if operation.get("status") == "error" or isinstance(operation.get("value"), str):
                last_output = operation
            if evidence := operation.get("native"):
                native = evidence
        elif operation["op"] == "input" and "send" in operation:
            source = graph.nodes.get(operation["send"], {})
            source_actor = all_descriptors.get(source.get("stream"), {})
            if source_actor.get("kind") == "principal":
                human_activity = _timestamp(operation["time"])

    descriptor["created"] = _timestamp(stream[0][1]["time"]) if stream else 0
    descriptor["worked"] = any(operation["op"] == "input" for _, operation in stream)
    descriptor["label"] = label(addr)
    descriptor["native_id"] = address_identity(addr, descriptor["kind"])
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
    return descriptor


def update_actor_descriptor(graph, all_descriptors, current, raw, nodes):
    if current is None:
        operations = operation_index(graph).get(raw["addr"], ())
        return actor_descriptor(graph, all_descriptors, raw, operations)
    descriptor = {**current, **raw}
    for node in sorted(nodes, key=lambda item: _position(item["id"])):
        operation = node["op"]
        if operation == "evaluation":
            descriptor["active_evaluation"] = node["id"]
            descriptor["evaluation_started"] = _timestamp(node["time"])
        elif operation == "output":
            descriptor["active_evaluation"] = None
            descriptor["evaluation_started"] = 0
            if evidence := node.get("native"):
                descriptor["native"] = evidence
            if node.get("status") == "error":
                descriptor.pop("summary", None)
                descriptor["last_error"] = node.get("error", "")
            elif isinstance(node.get("value"), str):
                descriptor.pop("last_error", None)
                descriptor["summary"] = " ".join(node["value"].split())
        elif operation == "input" and "send" in node:
            source = graph.nodes.get(node["send"], {})
            source_actor = all_descriptors.get(source.get("stream"), {})
            if source_actor.get("kind") == "principal":
                descriptor["human_activity"] = _timestamp(node["time"])
    if descriptor["state"] != "working":
        descriptor["active_evaluation"] = None
        descriptor["evaluation_started"] = 0
    return descriptor


def inventory(source, actor_descriptors):
    source = ServerRef(source, "", 0, 0, "alan")
    sessions = []
    for actor in actor_descriptors:
        if (actor["state"] == "closed"
                and actor.get("evaluator") != "native"):
            continue
        transcript_id = address_identity(actor["addr"], actor["kind"])
        sessions.append(Session(
            SessionRef(source, actor["addr"]), actor.get("label") or label(actor["addr"]),
            actor["created"], 0, 0, 1, "alan", "",
            actor.get("cwd") or "", actor["kind"], actor["state"],
            actor.get("summary") or actor.get("last_error", ""),
            _timestamp(actor["last_operation_activity"]),
            transcript_id,
            actor["human_activity"], actor.get("active_evaluation") or "",
            actor["evaluation_started"], "",
            worked=actor.get("worked", True),
            evaluator=actor.get("evaluator", ""),
            managed=actor.get("managed", False),
            stop=actor["stop"]))
    return sessions


def projection_graph(graph):
    if graph is None:
        return None
    projected = nx.MultiDiGraph()
    projected.graph["actors"] = graph.graph.get("actors", [])

    def add_reference(reference):
        stream = graph.nodes[reference].get("stream")
        projected.add_node(reference, **({"stream": stream}
                                         if stream is not None else {}))

    for source, target, relation in graph.edges(keys=True):
        if relation not in {"spawn", "send", "reply"}:
            continue
        add_reference(source)
        add_reference(target)
        projected.add_edge(source, target, key=relation)
    fields = {"op", "to", "reply", "stream", "time", "payload"}
    for reference, operation in graph.nodes(data=True):
        if operation.get("op") == "send":
            projected.add_node(reference, **{
                key: operation[key] for key in fields if key in operation})
    return projected


def apply_projection_delta(projected, graph, change):
    projected.graph["actors"] = graph.graph.get("actors", [])
    fields = {"op", "to", "reply", "stream", "time", "payload"}
    for node in change["nodes"]:
        if node.get("op") == "send":
            projected.add_node(node["id"], **{
                key: node[key] for key in fields if key in node})
    for edge in change["edges"]:
        relation = edge["key"]
        if relation not in {"spawn", "send", "reply"}:
            continue
        for reference in (edge["source"], edge["target"]):
            operation = graph.nodes.get(reference, {})
            stream = operation.get("stream")
            projected.add_node(reference, **({"stream": stream}
                                              if stream is not None else {}))
        projected.add_edge(edge["source"], edge["target"], key=relation)


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

    def graph_actor(session):
        return (session.ref.key if session.ref.key in descriptors
                else session.ref.session_id)

    actor_sessions = {
        graph_actor(session): session
        for session in sessions
        if session.ref.server.kind == "alan"
    }
    expanded = set(expanded)
    children = {}
    eligible = set()
    session_order = [graph_actor(session) for session in sessions
                     if session.ref.server.kind == "alan"
                     and (show_python or session.agent != "python"
                          or session.state in {"stopped", "failed"})]
    principals = {addr for addr, descriptor in descriptors.items()
                  if descriptor["kind"] == "principal"}
    native_roots = {addr for addr, descriptor in descriptors.items()
                    if descriptor.get("evaluator") == "native"}
    roots = {}
    for actor in session_order:
        ancestors = nx.ancestors(ancestry, actor)
        candidates = ancestors & principals
        if not candidates:
            if (ancestors | {actor}) & native_roots:
                roots[actor] = actor
            continue
        [principal] = candidates
        first = nx.shortest_path(ancestry, principal, actor)[1]
        if descriptors[first].get("preset") == "commander" or (
            descriptors[first]["kind"] == "python" and not show_python
            and descriptors[first]["state"] not in {"stopped", "failed"}
        ):
            continue
        roots[actor] = principal

    visible = [actor for actor in session_order
               if actor in roots
               and descriptors[actor].get("preset") != "commander"
               and (descriptors[actor]["kind"] != "python" or show_python
                    or descriptors[actor]["state"] in {"stopped", "failed"})]
    visible_set = set(visible)
    for actor in visible:
        candidates = nx.ancestors(ancestry, actor) & visible_set
        if candidates:
            parent = min(candidates, key=lambda item: nx.shortest_path_length(
                ancestry, item, actor))
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
    for source, target, request in _outstanding_requests(graph, descriptors):
        candidates = (nx.ancestors(ancestry, source) | {source}) & emitted
        if not candidates:
            continue
        anchor = min(candidates, key=lambda actor: nx.shortest_path_length(
            ancestry, actor, source))
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
        actor = graph_actor(session)
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
        requests.append((operation["stream"], operation["to"],
                         (operation["time"], _timestamp(operation["time"]), preview)))
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


def close(addr):
    loop.control(addr, "close")


def open(addr):
    loop.control(addr, "open")


def stop(addr):
    loop.control(addr, "stop")


def verify_pristine(addr):
    graph = loop.observe(actor=addr)
    for _reference, operation in graph.nodes(data=True):
        if operation.get("stream") == addr and operation.get("op") == "input":
            raise RuntimeError(f"actor has conversational work: {addr}")


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


def preview(addr, columns=0, lines=0, graph=None):
    if graph is None:
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
        observation = loop.observe(stream=True, actors=True)
        try:
            graph = next(observation)
        finally:
            observation.close()
        commanders = [actor["addr"] for actor in graph.graph.get("actors", [])
                      if actor.get("preset") == "commander"
                      and actor.get("state") != "closed"]
        if len(commanders) > 1:
            raise RuntimeError("multiple Commander actors")
        if commanders:
            return commanders[0]
        return loop.spawn({"kind": "llm", "preset": "commander"})


def _label_path(addr):
    return _state_dir() / "labels" / addr


def _state_dir():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-fleet"
