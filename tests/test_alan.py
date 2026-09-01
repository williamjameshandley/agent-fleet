import queue
import ast
import threading
from pathlib import Path
from unittest import mock

import networkx as nx
import pytest

from agent_fleet import alan
from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.protocol import decode_graph, encode


def graph(*operations, state="waiting"):
    actor = "codex-a@newton"
    result = nx.MultiDiGraph()
    result.graph["actors"] = [{
        "addr": actor,
        "kind": "codex",
        "host": "newton",
        "cwd": "/work",
        "state": state,
    }]
    for position, operation in enumerate(operations):
        result.add_node(
            f"{actor}#{position}",
            stream=actor,
            time=f"2026-07-30T12:00:0{position}Z",
            **operation,
        )
    return result


def test_live_state_is_authoritative_and_identity_comes_from_address():
    current = alan.actors(graph(
        {"op": "create"},
        {"op": "input"},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "native": {
            "kind": "codex", "session_id": "app-session", "turn_id": "turn-1",
            "base_dir": "/native", "path": "/native/rollout-a.jsonl",
        }},
        {"op": "input"},
        {"op": "evaluation"},
        state="working",
    ))[0]

    assert current["state"] == "working"
    assert current["active_evaluation"] == "codex-a@newton#5"
    assert current["evaluation_started"] == 1785412805
    assert current["native"]["session_id"] == "app-session"
    assert current["native"]["turn_id"] == "turn-1"
    assert current["native"]["path"] == "/native/rollout-a.jsonl"
    assert "id" not in current["native"]
    assert current["native"]["base_dir"] == "/native"
    assert current["native_id"] == "a"
    assert current["created"] == 1785412800
    assert current["human_activity"] == 0


def test_unmatched_historical_evaluation_does_not_make_a_waiting_actor_working():
    current = alan.actors(graph(
        {"op": "create"},
        {"op": "input"},
        {"op": "evaluation"},
        state="waiting",
    ))[0]

    assert current["state"] == "waiting"
    assert current["active_evaluation"] is None
    assert current["evaluation_started"] == 0


def test_principals_are_not_sessions_and_their_sends_are_human_activity():
    current = graph({"op": "create"}, {"op": "input", "send": "will@newton#1"})
    current.graph["actors"].append({
        "addr": "will@newton", "kind": "principal", "host": "newton"
    })
    current.add_node("will@newton#0", stream="will@newton", op="create",
                     time="2026-07-30T12:00:00Z")
    current.add_node("will@newton#1", stream="will@newton", op="send",
                     to="codex-a@newton", payload="hello",
                     time="2026-07-30T12:00:01Z")
    current.add_edge("will@newton#1", "codex-a@newton#1", key="send")

    [actor] = alan.actors(current)

    assert actor["addr"] == "codex-a@newton"
    assert actor["human_activity"] == 1785412801


def test_closed_and_retired_actors_are_reconstructed_without_extra_state():
    waiting = alan.actors(graph(
        {"op": "create"},
        {"op": "input"},
        {"op": "evaluation"},
        {"op": "output", "status": "error", "error": "failed"},
    ))[0]
    assert waiting["state"] == "waiting"
    assert waiting["last_error"] == "failed"

    retired = alan.actors(graph({"op": "create"}, state="retired"))[0]
    assert retired["state"] == "retired"


def test_conversational_work_is_derived_from_input_operations():
    pristine = alan.actors(graph({"op": "create"}))[0]
    assert pristine["worked"] is False

    worked = alan.actors(graph({"op": "create"}, {"op": "input"}))[0]
    assert worked["worked"] is True


def test_latest_successful_output_is_the_actor_summary():
    current = alan.actors(graph(
        {"op": "create"},
        {"op": "output", "status": "ok", "value": "latest\nmodel reply"},
    ))[0]

    assert current["summary"] == "latest model reply"


def test_interruption_preserves_the_latest_meaningful_output():
    current = alan.actors(graph(
        {"op": "create"},
        {"op": "output", "status": "ok", "value": "latest model reply"},
        {"op": "output", "status": "interrupted", "error": "interrupted"},
    ))[0]

    assert current["summary"] == "latest model reply"


def test_foreign_semantic_endpoint_is_not_mistaken_for_an_observed_operation():
    current = graph({"op": "create"}, {"op": "input"})
    current.add_edge(
        "claude-remote@lovelace#4",
        "codex-a@newton#1",
        key="send",
        relation="send",
    )

    actor = alan.actors(current)[0]

    assert actor["addr"] == "codex-a@newton"
    assert actor["human_activity"] == 0


def test_explicit_empty_graph_is_not_replaced_by_a_live_observation():
    with mock.patch.object(alan.loop, "observe") as observe:
        assert alan.actors(nx.MultiDiGraph()) == []
    observe.assert_not_called()


def test_output_is_selected_by_the_input_its_result_reports_as_delivered():
    current = graph(
        {"op": "create"},
        {"op": "input"},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "value": "first"},
        {"op": "input"},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "value": "second"},
    )
    current.add_node("will@newton#2", stream="will@newton", op="result",
                     input="codex-a@newton#4")
    with mock.patch.object(
            alan.loop, "observe", return_value=ObservationStream((current,))):
        assert alan.wait_output(
            "codex-a@newton", "will@newton#2")["value"] == "second"


def test_delivery_without_an_input_reference_is_a_visible_error():
    current = graph({"op": "create"})
    current.add_node("will@newton#2", stream="will@newton", op="result")
    with mock.patch.object(
            alan.loop, "observe", return_value=ObservationStream((current,))), \
         pytest.raises(RuntimeError, match="delivery reported no input: will@newton#2"):
        alan.wait_output("codex-a@newton", "will@newton#2")


def test_output_waits_while_its_input_has_not_been_delivered():
    delivered = graph(
        {"op": "create"},
        {"op": "input"},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "value": "first"},
    )
    delivered.add_node("will@newton#2", stream="will@newton", op="result",
                       input="codex-a@newton#1")
    with mock.patch.object(
            alan.loop, "observe",
            return_value=ObservationStream((graph({"op": "create"}), delivered))):
        assert alan.wait_output(
            "codex-a@newton", "will@newton#2")["value"] == "first"


def test_preview_is_a_projection_of_input_and_output_operations():
    current = graph(
        {"op": "create"},
        {"op": "input", "payload": {"kind": "exec", "code": "1 + 1"}},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "value": "2"},
    )
    with mock.patch.object(alan.loop, "observe", return_value=current):
        assert alan.preview("codex-a@newton") == "Input\n1 + 1\n\nOk\n2\n"


class ObservationStream:
    def __init__(self, values, stopped=None):
        self.values = iter(values)
        self.stopped = stopped
        self.change = None

    def __iter__(self):
        return self

    def __next__(self):
        try:
            value = next(self.values)
        except StopIteration:
            if self.stopped:
                self.stopped.set()
            raise
        if isinstance(value, Exception):
            if self.stopped:
                self.stopped.set()
            raise value
        self.change = {"kind": "replace", "graph": {}}
        return value

    def next(self, update=None, lock=None):
        if lock is None:
            graph = next(self)
            if update:
                update(graph, self.change)
            return graph
        with lock:
            graph = next(self)
            if update:
                update(graph, self.change)
            return graph

    def close(self):
        pass


def test_watcher_consumes_streamed_observations_without_polling():
    stopped = threading.Event()
    changed = queue.Queue()
    first = graph({"op": "create"})
    second = graph({"op": "create"}, {"op": "input"})
    observations = ObservationStream((first, second), stopped)

    def observe(*, stream):
        assert stream
        return observations

    with mock.patch.object(alan.loop, "observe", side_effect=observe), \
         mock.patch.object(alan.time, "sleep"):
        watcher = alan.Watcher(changed, stopped)
        watcher._thread.join(1)

    assert watcher.available
    assert watcher.graph is second
    assert changed.qsize() == 2


def test_watcher_emits_actor_metadata_only_changes():
    stopped = threading.Event()
    changed = queue.Queue()
    live = graph({"op": "create"})
    unavailable = graph({"op": "create"}, state="unavailable")
    observations = ObservationStream((live, unavailable), stopped)

    def observe(*, stream):
        assert stream
        return observations

    with mock.patch.object(alan.loop, "observe", side_effect=observe), \
         mock.patch.object(alan.time, "sleep"):
        watcher = alan.Watcher(changed, stopped)
        watcher._thread.join(1)

    assert watcher.actors[0]["state"] == "unavailable"
    assert changed.qsize() == 2


def test_observation_failure_is_visible_and_clears_projection():
    stopped = threading.Event()
    changed = queue.Queue()
    observations = ObservationStream(
        (graph({"op": "create"}), OSError("closed")), stopped)

    def observe(*, stream):
        assert stream
        return observations

    with mock.patch.object(alan.loop, "observe", side_effect=observe), \
         mock.patch.object(alan.time, "sleep"):
        watcher = alan.Watcher(changed, stopped)
        watcher._thread.join(1)

    assert not watcher.available
    assert watcher.actors == []
    assert watcher.graph is None
    assert watcher.error == "Alan unavailable: closed"


def test_watcher_incremental_refresh_updates_under_one_lock():
    watcher = object.__new__(alan.Watcher)
    watcher.actors = ["stale"]
    watcher.graph = "stale graph"
    watcher.projected = None
    watcher._operations = {}
    watcher._descriptors = {}
    watcher.available = True
    watcher.error = None
    watcher.initialized = threading.Event()
    watcher._changed = queue.Queue()
    watcher._lock = threading.Lock()
    fresh = graph({"op": "create"})

    watcher.refresh(fresh, {"kind": "replace", "graph": {}})

    with watcher.snapshot() as (actors, observed):
        actors = list(actors)
    assert observed is watcher.projected
    assert actors == watcher.actors
    assert watcher.available
    assert watcher.error is None


def test_incremental_refresh_does_not_reenter_whole_graph_derivation():
    watcher = object.__new__(alan.Watcher)
    watcher.actors = []
    watcher.graph = None
    watcher.projected = None
    watcher._descriptors = {}
    watcher.available = False
    watcher.error = None
    watcher.initialized = threading.Event()
    watcher._changed = queue.Queue()
    watcher._lock = threading.Lock()
    current = graph({"op": "create"})
    for position in range(5_000):
        current.add_node(f"other@newton#{position}", stream="other@newton",
                         op="input", time="2026-01-01T00:00:00Z")
    watcher.refresh(current, {"kind": "replace", "graph": {}})

    reference = "codex-a@newton#1"
    node = {"id": reference, "stream": "codex-a@newton", "op": "evaluation",
            "time": "2026-01-01T00:00:01Z"}
    current.add_node(reference, **{key: value for key, value in node.items()
                                   if key != "id"})
    current.graph["actors"][0]["state"] = "working"
    change = {"kind": "delta", "generation": 1, "revision": 1,
              "actors": [current.graph["actors"][0]], "nodes": [node], "edges": []}

    with mock.patch.object(alan, "actors", side_effect=AssertionError("full actors")), \
         mock.patch.object(alan, "projection_graph",
                           side_effect=AssertionError("full projection")):
        watcher.refresh(current, change)

    assert watcher.actors[0]["active_evaluation"] == reference


def test_new_actor_delta_reconstructs_operations_already_in_the_graph():
    watcher = object.__new__(alan.Watcher)
    watcher.actors = []
    watcher.graph = nx.MultiDiGraph()
    watcher.projected = nx.MultiDiGraph()
    watcher._descriptors = {}
    watcher.available = True
    watcher.error = None
    watcher.initialized = threading.Event()
    watcher._changed = queue.Queue()
    watcher._lock = threading.RLock()
    current = graph(
        {"op": "create"},
        {"op": "output", "status": "ok", "value": "restored summary"},
    )

    watcher.refresh(current, {
        "kind": "delta", "generation": 1, "revision": 1,
        "actors": current.graph["actors"], "nodes": [], "edges": [],
    })

    assert watcher.actors == alan.actors(current)
    assert watcher.actors[0]["summary"] == "restored summary"


@pytest.mark.parametrize("prior", [
    {"status": "ok", "value": "stale summary"},
    {"status": "error", "error": "stale error"},
])
def test_incremental_interruption_matches_full_reconstruction(prior):
    watcher = object.__new__(alan.Watcher)
    watcher.actors = []
    watcher.graph = None
    watcher.projected = None
    watcher._descriptors = {}
    watcher.available = False
    watcher.error = None
    watcher.initialized = threading.Event()
    watcher._changed = queue.Queue()
    watcher._lock = threading.RLock()
    current = graph({"op": "create"}, {"op": "output", **prior})
    watcher.refresh(current, {"kind": "replace", "graph": {}})

    reference = "codex-a@newton#2"
    interrupted = {
        "id": reference, "stream": "codex-a@newton", "op": "output",
        "status": "interrupted", "time": "2026-07-30T12:00:02Z",
    }
    current.add_node(reference, **{key: value for key, value in interrupted.items()
                                   if key != "id"})
    watcher.refresh(current, {
        "kind": "delta", "generation": 1, "revision": 1,
        "actors": current.graph["actors"], "nodes": [interrupted], "edges": [],
    })

    assert watcher.actors == alan.actors(current)
    if prior["status"] == "ok":
        assert watcher.actors[0]["summary"] == "stale summary"
    else:
        assert watcher.actors[0]["last_error"] == "stale error"


def test_watcher_reports_one_loss_and_recovers_with_one_replacement():
    stopped = threading.Event()
    changed = queue.Queue()
    first = ObservationStream((graph({"op": "create"}), OSError("closed")))
    second = ObservationStream((graph({"op": "create"}, state="working"),), stopped)
    streams = iter((first, second))

    with mock.patch.object(alan.loop, "observe",
                           side_effect=lambda **_kwargs: next(streams)), \
         mock.patch.object(stopped, "wait", side_effect=lambda _delay: False), \
         mock.patch.object(alan.time, "sleep"):
        watcher = alan.Watcher(changed, stopped)
        watcher._thread.join(1)

    assert watcher.available
    assert watcher.error is None
    assert watcher.actors[0]["state"] == "working"
    assert list(changed.queue) == ["alan", "alan", "alan"]


def test_actor_lifecycle_delta_updates_without_operation_reconstruction():
    watcher = object.__new__(alan.Watcher)
    watcher.actors = []
    watcher.graph = None
    watcher.projected = None
    watcher._descriptors = {}
    watcher.available = False
    watcher.error = None
    watcher.initialized = threading.Event()
    watcher._changed = queue.Queue()
    watcher._lock = threading.RLock()
    current = graph({"op": "create"}, {"op": "evaluation"}, state="working")
    watcher.refresh(current, {"kind": "replace", "graph": {}})
    assert watcher.actors[0]["active_evaluation"] == "codex-a@newton#1"

    current.graph["actors"][0]["state"] = "unavailable"
    watcher.refresh(current, {
        "kind": "delta", "generation": 1, "revision": 1,
        "actors": current.graph["actors"], "nodes": [], "edges": [],
    })
    assert watcher.actors[0]["state"] == "unavailable"
    assert watcher.actors[0]["active_evaluation"] is None


def test_label_change_updates_only_fleet_projection(tmp_path):
    actor = "codex-a@newton"
    labels = tmp_path / "labels"
    labels.mkdir()
    path = labels / actor
    path.write_text("new label\n")
    watcher = object.__new__(alan.Watcher)
    watcher._consumer = threading.Event()
    watcher._descriptors = {actor: {"addr": actor, "kind": "codex",
                                    "label": "old label"}}
    watcher.actors = list(watcher._descriptors.values())
    watcher.projected = nx.MultiDiGraph()
    watcher.projected.graph["actors"] = watcher.actors
    watcher._changed = queue.Queue()
    watcher._lock = threading.Lock()

    with mock.patch.object(alan, "_state_dir", return_value=tmp_path), \
         mock.patch.object(alan, "watch", return_value=iter((
             {("modified", str(path))},
         ))), \
         mock.patch.object(alan.loop, "observe") as observe:
        watcher._watch_labels()

    assert watcher.actors[0]["label"] == "new label"
    assert watcher._changed.get_nowait() == "alan"
    observe.assert_not_called()


def test_production_observation_calls_are_confined_to_explicit_scopes():
    root = Path(alan.__file__).parent
    found = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (not isinstance(node.func.value, ast.Name)
                    or node.func.value.id != "loop" or node.func.attr != "observe"):
                continue
            parent = node
            while parent is not None and not isinstance(
                    parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = parents.get(parent)
            found.append((path.name, parent.name if parent else "", tuple(
                sorted(keyword.arg for keyword in node.keywords))))
    assert sorted(found) == [
        ("alan.py", "_run", ("stream",)),
        ("alan.py", "actors", ()),
        ("alan.py", "commander_actor", ("actors", "stream")),
        ("alan.py", "preview", ()),
        ("alan.py", "verify_pristine", ("actor",)),
        ("alan.py", "wait_output", ("actor", "stream")),
        ("commander_client.py", "exchange", ("actor", "stream")),
        ("presentation.py", "run", ("actor", "stream")),
    ]


def actor_session(addr, kind):
    host = addr.rsplit("@", 1)[1]
    return Session(
        SessionRef(ServerRef(host, "", 0, 0, "alan"), addr),
        addr.split("@", 1)[0], 1, 0, 0, 1, "alan", "", "/work", kind, "waiting",
    )


def projected_key(addr):
    return f'alan:{addr.rsplit("@", 1)[1]}:{addr}'


def principal_root(current, root, principal="will@newton"):
    current.graph["actors"].append({"addr": principal, "kind": "principal"})
    current.add_node(f"{principal}#0", stream=principal, op="create")
    current.add_node(f"{principal}#1", stream=principal, op="spawn")
    current.add_edge(f"{principal}#1", f"{root}#0", key="spawn")


def test_projection_includes_adopted_native_root_and_descendants():
    actor = "codex-native@newton"
    python = "python-child@newton"
    llm = "llm-grandchild@newton"
    current = nx.MultiDiGraph()
    current.graph["actors"] = [
        {"addr": actor, "kind": "codex", "evaluator": "native"},
        {"addr": python, "kind": "python"},
        {"addr": llm, "kind": "llm"},
    ]
    for descendant in (actor, python, llm):
        current.add_node(f"{descendant}#0", stream=descendant, op="create")
    for parent, child in ((actor, python), (python, llm)):
        current.add_node(f"{parent}#1", stream=parent, op="spawn")
        current.add_edge(f"{parent}#1", f"{child}#0", key="spawn")

    sessions = [
        actor_session(actor, "codex"),
        actor_session(python, "python"),
        actor_session(llm, "llm"),
    ]

    [projected] = alan.project(sessions, current)

    assert projected.session.ref.key == projected_key(actor)
    assert projected.depth == 0
    assert projected.child_count == 1
    assert [item.session.ref.key for item in alan.project(
        sessions, current, expanded={actor}
    )] == [projected_key(actor), projected_key(llm)]
    assert [item.session.ref.key for item in alan.project(
        sessions, current, expanded={actor, python}, show_python=True
    )] == [projected_key(actor), projected_key(python), projected_key(llm)]


def test_projection_derives_recursive_visible_tree():
    root = "codex-root@newton"
    language = "claude-child@lovelace"
    python = "python-child@newton"
    nested = "codex-nested@lovelace"
    python_nested = "claude-through-python@lovelace"
    commander = "llm-commander@newton"
    commander_child = "claude-commander-child@newton"
    nested_commander = "llm-nested-commander@lovelace"
    direct_python = "python-root@newton"
    current = nx.MultiDiGraph()
    current.graph["actors"] = [
        {"addr": root, "kind": "codex"},
        {"addr": language, "kind": "claude"},
        {"addr": python, "kind": "python"},
        {"addr": nested, "kind": "codex"},
        {"addr": python_nested, "kind": "claude"},
        {"addr": commander, "kind": "llm", "preset": "commander"},
        {"addr": commander_child, "kind": "claude"},
        {"addr": nested_commander, "kind": "llm", "preset": "commander"},
        {"addr": direct_python, "kind": "python"},
    ]
    for actor in (root, language, python, nested, python_nested, commander,
                  commander_child, nested_commander, direct_python):
        current.add_node(f"{actor}#0", stream=actor, op="create")
    for parent, child, position in (
        (root, language, 1),
        (root, python, 2),
        (language, nested, 1),
        (python, python_nested, 1),
        (commander, commander_child, 1),
        (root, nested_commander, 3),
    ):
        source = f"{parent}#{position}"
        current.add_node(source, stream=parent, op="spawn")
        current.add_edge(source, f"{child}#0", key="spawn")
    principal_root(current, root)
    current.add_node("will@newton#2", stream="will@newton", op="spawn")
    current.add_edge("will@newton#2", f"{commander}#0", key="spawn")
    current.add_node("will@newton#3", stream="will@newton", op="spawn")
    current.add_edge("will@newton#3", f"{direct_python}#0", key="spawn")

    standalone = Session(
        SessionRef(ServerRef("newton", "/tmp/tmux", 1, 1), "$1"),
        "standalone", 1, 0, 0, 1, "codex", "", "/work",
    )
    sessions = [standalone] + [
        actor_session(actor, next(item["kind"] for item in current.graph["actors"]
                                  if item["addr"] == actor))
        for actor in (root, language, python, nested, python_nested, commander,
                      commander_child, nested_commander, direct_python)
    ]

    collapsed = alan.project(sessions, current)
    assert [(item.session.ref.key, item.depth, item.child_count, item.expanded)
            for item in collapsed] == [
        (standalone.ref.key, 0, 0, False),
        (projected_key(root), 0, 2, False),
    ]
    assert alan.project(sessions, current, expanded={language}) == collapsed

    root_open = alan.project(sessions, current, expanded={root})
    assert [(item.session.ref.key, item.depth, item.child_count)
            for item in root_open] == [
        (standalone.ref.key, 0, 0),
        (projected_key(root), 0, 2),
        (projected_key(language), 1, 1),
        (projected_key(python_nested), 1, 0),
    ]

    nested_open = alan.project(sessions, current, expanded={root, language})
    assert [(item.session.ref.key, item.depth) for item in nested_open] == [
        (standalone.ref.key, 0),
        (projected_key(root), 0),
        (projected_key(language), 1),
        (projected_key(nested), 2),
        (projected_key(python_nested), 1),
    ]
    assert [item.session.ref.key for item in alan.project(sessions, current)] == [
        standalone.ref.key, projected_key(root),
    ]

    python_open = alan.project(
        sessions, current, expanded={root, python}, show_python=True
    )
    assert [(item.session.ref.key, item.depth, item.child_count)
            for item in python_open] == [
        (standalone.ref.key, 0, 0),
        (projected_key(root), 0, 2),
        (projected_key(language), 1, 1),
        (projected_key(python), 1, 1),
        (projected_key(python_nested), 2, 0),
        (projected_key(direct_python), 0, 0),
    ]
    compact = alan.projection_graph(current)
    assert all(set(operation) <= {
        "op", "to", "reply", "stream", "time", "payload"}
               for _, operation in compact.nodes(data=True))
    assert alan.project(
        sessions, compact,
        expanded={root, python}, show_python=True) == python_open


def test_projection_compresses_an_absent_intermediate_inside_an_eligible_tree():
    root = "codex-root@newton"
    bridge = "llm-bridge@newton"
    child = "claude-child@lovelace"
    current = nx.MultiDiGraph()
    current.graph["actors"] = [
        {"addr": root, "kind": "codex"},
        {"addr": bridge, "kind": "llm"},
        {"addr": child, "kind": "claude"},
    ]
    for actor in (root, bridge, child):
        current.add_node(f"{actor}#0", stream=actor, op="create")
    for parent, descendant in ((root, bridge), (bridge, child)):
        source = f"{parent}#1"
        current.add_node(source, stream=parent, op="spawn")
        current.add_edge(source, f"{descendant}#0", key="spawn")
    principal_root(current, root)
    sessions = [actor_session(root, "codex"), actor_session(child, "claude")]

    projected = alan.project(sessions, current, expanded={root})
    assert [(item.session.ref.key, item.depth, item.child_count)
            for item in projected] == [
        (projected_key(root), 0, 1),
        (projected_key(child), 1, 0),
    ]


def test_projection_folds_an_actor_without_a_principal_root():
    actor = "codex-rootless@newton"
    current = nx.MultiDiGraph()
    current.graph["actors"] = [{"addr": actor, "kind": "codex"}]
    current.add_node(f"{actor}#0", stream=actor, op="create")

    assert alan.project([actor_session(actor, "codex")], current) == []


def test_projection_folds_beneath_the_nearest_creator_across_ancestry_lines():
    chained = "claude-chained@newton"
    adopted = "claude-adopted@newton"
    current = nx.MultiDiGraph()
    current.graph["actors"] = [
        {"addr": chained, "kind": "claude"},
        {"addr": adopted, "kind": "claude", "evaluator": "native"},
    ]
    current.add_node(f"{chained}#0", stream=chained, op="create")
    current.add_node(f"{adopted}#0", stream=adopted, op="create")
    principal_root(current, chained)
    current.add_node(f"{adopted}#1", stream=adopted, op="spawn")
    current.add_edge(f"{adopted}#1", f"{chained}#0", key="spawn")
    sessions = [actor_session(chained, "claude"), actor_session(adopted, "claude")]

    collapsed = alan.project(sessions, current)
    assert [(item.session.ref.key, item.child_count) for item in collapsed] == [
        (projected_key(adopted), 1)]
    assert [item.session.ref.key for item in alan.project(
        sessions, current, expanded={adopted}
    )] == [projected_key(adopted), projected_key(chained)]


def test_projection_composes_spawn_ancestry_from_separate_hosts():
    parent = "codex-parent@newton"
    child = "claude-child@lovelace"
    source = f"{parent}#1"
    target = f"{child}#0"

    newton = nx.MultiDiGraph()
    newton.graph["actors"] = [{"addr": parent, "kind": "codex"}]
    newton.add_node(f"{parent}#0", stream=parent, op="create")
    newton.add_node(source, stream=parent, op="spawn")

    lovelace = nx.MultiDiGraph()
    lovelace.graph["actors"] = [{"addr": child, "kind": "claude"}]
    lovelace.add_node(target, stream=child, op="create", spawn=source)
    lovelace.add_edge(source, target, key="spawn")

    current = nx.compose(newton, lovelace)
    current.graph["actors"] = newton.graph["actors"] + lovelace.graph["actors"]
    principal_root(current, parent)
    sessions = [actor_session(parent, "codex"), actor_session(child, "claude")]

    assert [item.session.ref.key for item in alan.project(sessions, current)] == [
        projected_key(parent)]
    assert [item.session.ref.key for item in alan.project(
        sessions, current, expanded={parent}
    )] == [projected_key(parent), projected_key(child)]


def test_projection_omits_multiple_principals_and_keeps_their_trees_independent():
    first = "codex-first@newton"
    child = "claude-child@lovelace"
    second = "claude-second@boltzmann"
    current = nx.MultiDiGraph()
    current.graph["actors"] = [
        {"addr": first, "kind": "codex"},
        {"addr": child, "kind": "claude"},
        {"addr": second, "kind": "claude"},
    ]
    for actor in (first, child, second):
        current.add_node(f"{actor}#0", stream=actor, op="create")
    principal_root(current, first, "will@newton")
    principal_root(current, second, "will@boltzmann")
    current.add_node(f"{first}#1", stream=first, op="spawn")
    current.add_edge(f"{first}#1", f"{child}#0", key="spawn")
    sessions = [actor_session(actor, kind) for actor, kind in (
        (first, "codex"), (child, "claude"), (second, "claude"))]

    assert [(item.session.ref.key, item.depth) for item in
            alan.project(sessions, current)] == [
        (projected_key(first), 0), (projected_key(second), 0)]
    assert [(item.session.ref.key, item.depth) for item in
            alan.project(sessions, current, expanded={first})] == [
        (projected_key(first), 0), (projected_key(child), 1),
        (projected_key(second), 0)]


def test_outstanding_principal_requests_follow_recursive_folds_and_reply_edges():
    root = "codex-root@newton"
    child = "claude-child@newton"
    nested = "codex-nested@newton"
    principal = "will@newton"
    current = nx.MultiDiGraph()
    current.graph["actors"] = [
        {"addr": root, "kind": "codex"},
        {"addr": child, "kind": "claude"},
        {"addr": nested, "kind": "codex"},
        {"addr": principal, "kind": "principal"},
    ]
    for actor in (root, child, nested, principal):
        current.add_node(f"{actor}#0", stream=actor, op="create",
                         time="2026-07-30T12:00:00Z")
    for parent, descendant in ((root, child), (child, nested)):
        spawn = f"{parent}#1"
        current.add_node(spawn, stream=parent, op="spawn",
                         time="2026-07-30T12:00:01Z")
        current.add_edge(spawn, f"{descendant}#0", key="spawn")
    current.add_node(f"{principal}#10", stream=principal, op="spawn",
                     time="2026-07-30T12:00:00Z")
    current.add_edge(f"{principal}#10", f"{root}#0", key="spawn")
    for position, (source, text) in enumerate(
            ((child, "first question"), (nested, "latest question")), 1):
        send = f"{source}#2"
        accepted = f"{principal}#{position}"
        current.add_node(send, stream=source, op="send", to=principal,
                         payload=text, time=f"2026-07-30T12:00:0{position + 2}Z")
        current.add_node(accepted, stream=principal, op="input", send=send,
                         payload=text, time=f"2026-07-30T12:00:0{position + 2}Z")
        current.add_edge(send, accepted, key="send")
    current.add_node(f"{root}#2", stream=root, op="send", to=principal,
                     payload="not accepted", time="2026-07-30T12:00:05Z")
    current.add_node(f"{principal}#3", stream=principal, op="send", to=nested,
                     payload="human prompt", time="2026-07-30T12:00:05Z")
    current.add_node(f"{nested}#3", stream=nested, op="input",
                     send=f"{principal}#3", payload="human prompt",
                     time="2026-07-30T12:00:05Z")
    current.add_edge(f"{principal}#3", f"{nested}#3", key="send")
    current.add_node(f"{nested}#4", stream=nested, op="send", to=principal,
                     reply=f"{principal}#3", payload="answer to human",
                     time="2026-07-30T12:00:06Z")
    current.add_node(f"{principal}#4", stream=principal, op="input",
                     send=f"{nested}#4", reply=f"{principal}#3",
                     payload="answer to human", time="2026-07-30T12:00:06Z")
    current.add_edge(f"{principal}#3", f"{nested}#4", key="reply")
    current.add_edge(f"{nested}#4", f"{principal}#4", key="send")
    sessions = [actor_session(actor, kind) for actor, kind in (
        (root, "codex"), (child, "claude"), (nested, "codex"))]

    [collapsed] = alan.project(sessions, current)
    assert collapsed.session.state == "needs-action"
    assert collapsed.session.summary == "2 awaiting — latest question"
    compact = decode_graph(encode([], graph=alan.projection_graph(current)))
    [compact_collapsed] = alan.project(sessions, compact)
    assert compact_collapsed == collapsed

    root_open = alan.project(sessions, current, expanded={root})
    assert root_open[0].session.state == "waiting"
    assert root_open[1].session.state == "needs-action"
    assert root_open[1].session.summary == "2 awaiting — latest question"

    nested_open = alan.project(sessions, current, expanded={root, child})
    assert [item.session.summary for item in nested_open] == [
        "", "1 awaiting — first question", "1 awaiting — latest question"
    ]

    reply = f"{principal}#5"
    current.add_node(reply, stream=principal, op="send", to=child,
                     reply=f"{child}#2", payload="answer",
                     time="2026-07-30T12:00:06Z")
    current.add_edge(f"{child}#2", reply, key="reply")

    [collapsed] = alan.project(sessions, current)
    assert collapsed.session.summary == "1 awaiting — latest question"

    reply_evidence = nx.MultiDiGraph()
    reply_evidence.graph["actors"] = []
    reply_evidence.add_node(f"{child}#2")
    reply_evidence.add_node(reply, stream=principal)
    reply_evidence.add_edge(f"{child}#2", reply, key="reply")
    request_graph = compact
    reply_graph = decode_graph(encode(
        [], graph=alan.projection_graph(reply_evidence)))
    composed = nx.compose(request_graph, reply_graph)
    composed.graph["actors"] = request_graph.graph["actors"]
    [composed_collapsed] = alan.project(sessions, composed)
    assert composed_collapsed == collapsed


def test_compact_requests_reconcile_a_principal_descriptor_from_another_host():
    actor = "codex-child@lovelace"
    principal = "will@newton"
    request = nx.MultiDiGraph()
    request.graph["actors"] = [{"addr": actor, "kind": "codex"}]
    request.add_node(f"{actor}#0", stream=actor, op="create")
    request.add_node(f"{actor}#1", stream=actor, op="send", to=principal,
                     payload="question", time="2026-07-30T12:00:01Z")
    request.add_node(f"{principal}#1", stream=principal, op="input")
    request.add_edge(f"{actor}#1", f"{principal}#1", key="send")

    ancestry = nx.MultiDiGraph()
    ancestry.graph["actors"] = [{"addr": principal, "kind": "principal"}]
    ancestry.add_node(f"{principal}#0", stream=principal, op="spawn")
    ancestry.add_node(f"{actor}#0", stream=actor)
    ancestry.add_edge(f"{principal}#0", f"{actor}#0", key="spawn")

    def compose(graphs):
        result = nx.compose_all(graphs)
        result.graph["actors"] = [actor for graph in graphs
                                  for actor in graph.graph["actors"]]
        return result

    full = compose([request, ancestry])
    compact = compose([
        decode_graph(encode([], graph=alan.projection_graph(graph)))
        for graph in (request, ancestry)])
    sessions = [actor_session(actor, "codex")]
    assert alan.project(sessions, compact) == alan.project(sessions, full)
    [projected] = alan.project(sessions, compact)
    assert projected.session.state == "needs-action"
    assert projected.session.summary == "1 awaiting — question"
