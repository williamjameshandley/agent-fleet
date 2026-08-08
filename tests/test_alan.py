import queue
import threading
from unittest import mock

import networkx as nx
import pytest

from agent_fleet import alan
from agent_fleet.model import ServerRef, Session, SessionRef


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


def test_latest_successful_output_is_the_actor_summary():
    current = alan.actors(graph(
        {"op": "create"},
        {"op": "output", "status": "ok", "value": "latest\nmodel reply"},
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


def test_output_is_selected_by_fifo_input_ordinal():
    current = graph(
        {"op": "create"},
        {"op": "input"},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "value": "first"},
        {"op": "input"},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "value": "second"},
    )
    with mock.patch.object(alan.loop, "observe", return_value=current):
        assert alan.wait_output("codex-a@newton#4")["value"] == "second"


def test_preview_is_a_projection_of_input_and_output_operations():
    current = graph(
        {"op": "create"},
        {"op": "input", "payload": {"kind": "exec", "code": "1 + 1"}},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "value": "2"},
    )
    with mock.patch.object(alan.loop, "observe", return_value=current):
        assert alan.preview("codex-a@newton") == "Input\n1 + 1\n\nOk\n2\n"


def test_watcher_polls_observe_and_emits_only_changed_snapshots():
    stopped = threading.Event()
    changed = queue.Queue()
    first = graph({"op": "create"})
    second = graph({"op": "create"}, {"op": "input"})
    observations = iter((first, first, second))

    def observe():
        try:
            return next(observations)
        except StopIteration:
            stopped.set()
            return second

    with mock.patch.object(alan.loop, "observe", side_effect=observe), \
         mock.patch.object(stopped, "wait", side_effect=lambda _delay: False), \
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
    observations = iter((live, unavailable))

    def observe():
        try:
            return next(observations)
        except StopIteration:
            stopped.set()
            return unavailable

    with mock.patch.object(alan.loop, "observe", side_effect=observe), \
         mock.patch.object(stopped, "wait", side_effect=lambda _delay: False), \
         mock.patch.object(alan.time, "sleep"):
        watcher = alan.Watcher(changed, stopped)
        watcher._thread.join(1)

    assert watcher.actors[0]["state"] == "unavailable"
    assert changed.qsize() == 2


def test_observation_failure_is_visible_and_clears_projection():
    stopped = threading.Event()
    changed = queue.Queue()
    observations = iter((graph({"op": "create"}), OSError("closed")))

    def observe():
        value = next(observations)
        if isinstance(value, Exception):
            stopped.set()
            raise value
        return value

    with mock.patch.object(alan.loop, "observe", side_effect=observe), \
         mock.patch.object(stopped, "wait", side_effect=lambda _delay: False), \
         mock.patch.object(alan.time, "sleep"):
        watcher = alan.Watcher(changed, stopped)
        watcher._thread.join(1)

    assert not watcher.available
    assert watcher.actors == []
    assert watcher.graph is None
    assert watcher.error == "Alan unavailable: closed"


def test_watcher_failure_cannot_erase_a_later_successful_refresh():
    watcher = object.__new__(alan.Watcher)
    watcher.actors = ["stale"]
    watcher.graph = "stale graph"
    watcher.available = True
    watcher.error = None
    watcher.initialized = threading.Event()
    watcher._changed = queue.Queue()
    watcher._lock = threading.Lock()
    failed_inside_observe = threading.Event()
    release_failure = threading.Event()
    fresh = graph({"op": "create"})

    def observe():
        if threading.current_thread().name == "failing-observe":
            failed_inside_observe.set()
            release_failure.wait()
            raise OSError("closed")
        return fresh

    def fail():
        try:
            watcher.refresh()
        except OSError as error:
            errors.append(str(error))

    errors = []
    failing = threading.Thread(
        name="failing-observe", target=fail,
    )
    succeeding = threading.Thread(target=watcher.refresh)
    with mock.patch.object(alan.loop, "observe", side_effect=observe):
        failing.start()
        assert failed_inside_observe.wait(1)
        succeeding.start()
        release_failure.set()
        failing.join(1)
        succeeding.join(1)

    actors, observed = watcher.snapshot()
    assert errors == ["closed"]
    assert observed is fresh
    assert actors == watcher.actors
    assert watcher.available
    assert watcher.error is None


def actor_session(addr, kind):
    host = addr.rsplit("@", 1)[1]
    return Session(
        SessionRef(ServerRef(host, "", 0, 0, "alan"), addr),
        addr.split("@", 1)[0], 1, 0, 0, 1, "alan", "", "/work", kind, "waiting",
    )


def principal_root(current, root, principal="will@newton"):
    current.graph["actors"].append({"addr": principal, "kind": "principal"})
    current.add_node(f"{principal}#0", stream=principal, op="create")
    current.add_node(f"{principal}#1", stream=principal, op="spawn")
    current.add_edge(f"{principal}#1", f"{root}#0", key="spawn")


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
        (f"alan:{root}", 0, 2, False),
    ]
    assert alan.project(sessions, current, expanded={language}) == collapsed

    root_open = alan.project(sessions, current, expanded={root})
    assert [(item.session.ref.key, item.depth, item.child_count)
            for item in root_open] == [
        (standalone.ref.key, 0, 0),
        (f"alan:{root}", 0, 2),
        (f"alan:{language}", 1, 1),
        (f"alan:{python_nested}", 1, 0),
    ]

    nested_open = alan.project(sessions, current, expanded={root, language})
    assert [(item.session.ref.key, item.depth) for item in nested_open] == [
        (standalone.ref.key, 0),
        (f"alan:{root}", 0),
        (f"alan:{language}", 1),
        (f"alan:{nested}", 2),
        (f"alan:{python_nested}", 1),
    ]
    assert [item.session.ref.key for item in alan.project(sessions, current)] == [
        standalone.ref.key, f"alan:{root}",
    ]

    python_open = alan.project(
        sessions, current, expanded={root, python}, show_python=True
    )
    assert [(item.session.ref.key, item.depth, item.child_count)
            for item in python_open] == [
        (standalone.ref.key, 0, 0),
        (f"alan:{root}", 0, 2),
        (f"alan:{language}", 1, 1),
        (f"alan:{python}", 1, 1),
        (f"alan:{python_nested}", 2, 0),
        (f"alan:{direct_python}", 0, 0),
    ]


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
        (f"alan:{root}", 0, 1),
        (f"alan:{child}", 1, 0),
    ]


def test_projection_folds_an_actor_without_a_principal_root():
    actor = "codex-rootless@newton"
    current = nx.MultiDiGraph()
    current.graph["actors"] = [{"addr": actor, "kind": "codex"}]
    current.add_node(f"{actor}#0", stream=actor, op="create")

    assert alan.project([actor_session(actor, "codex")], current) == []


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
        f"alan:{parent}"]
    assert [item.session.ref.key for item in alan.project(
        sessions, current, expanded={parent}
    )] == [f"alan:{parent}", f"alan:{child}"]


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
        (f"alan:{first}", 0), (f"alan:{second}", 0)]
    assert [(item.session.ref.key, item.depth) for item in
            alan.project(sessions, current, expanded={first})] == [
        (f"alan:{first}", 0), (f"alan:{child}", 1),
        (f"alan:{second}", 0)]


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
