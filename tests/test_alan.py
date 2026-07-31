import queue
import threading
from unittest import mock

import networkx as nx

from agent_fleet import alan


def graph(*operations, state="live"):
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


def test_actor_state_and_native_identity_are_derived_from_operations():
    current = alan.actors(graph(
        {"op": "create"},
        {"op": "input", "sender": "will"},
        {"op": "evaluation"},
        {"op": "output", "status": "ok", "native": {
            "kind": "codex", "thread_id": "thread-1", "base_dir": "/native",
        }},
        {"op": "input", "sender": "will"},
        {"op": "evaluation"},
    ))[0]

    assert current["state"] == "working"
    assert current["active_evaluation"] == "codex-a@newton#5"
    assert current["evaluation_started"] == 1785412805
    assert current["native"]["id"] == "thread-1"
    assert current["native"]["base_dir"] == "/native"
    assert current["created"] == 1785412800
    assert current["human_activity"] == 1785412804


def test_closed_and_retired_actors_are_reconstructed_without_extra_state():
    waiting = alan.actors(graph(
        {"op": "create"},
        {"op": "input", "sender": "will"},
        {"op": "evaluation"},
        {"op": "output", "status": "error", "error": "failed"},
    ))[0]
    assert waiting["state"] == "waiting"
    assert waiting["last_error"] == "failed"

    retired = alan.actors(graph({"op": "create"}, state="retired"))[0]
    assert retired["state"] == "retired"


def test_foreign_semantic_endpoint_is_not_mistaken_for_an_observed_operation():
    current = graph({"op": "create"}, {"op": "input", "sender": "will"})
    current.add_edge(
        "claude-remote@lovelace#4",
        "codex-a@newton#1",
        key="send",
        relation="send",
    )

    actor = alan.actors(current)[0]

    assert actor["addr"] == "codex-a@newton"
    assert actor["human_activity"] == 1785412801


def test_schedule_operations_do_not_become_fleet_actors():
    current = graph({"op": "create"})
    current.add_node(
        "schedule:550e8400-e29b-41d4-a716-446655440000@newton#0",
        op="send",
        to="codex-a@newton",
        payload="later",
        at="2037-01-02T03:04:05Z",
        time="2026-07-30T12:00:01Z",
    )

    assert [actor["addr"] for actor in alan.actors(current)] == ["codex-a@newton"]


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
    second = graph({"op": "create"}, {"op": "input", "sender": "will"})
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
