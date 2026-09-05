import queue
import json
import socket
import threading
import time
from unittest import mock

import networkx as nx
import pytest

from agent_fleet import alan
from agent_fleet.daemon import Fleet
from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.protocol import decode_observation, encode_observation
from agent_fleet.config import RuntimeSource

FIELDS = alan.ACTOR_FIELDS


def actor(addr, kind="codex", **values):
    item = {field: None for field in FIELDS}
    item.update(addr=addr, kind=kind, state="waiting", hibernation="exact",
                managed=False, worked=False, source_activity={},
                unresolved_requests={}, label=addr)
    item.update(values)
    return item


def session(addr, kind="codex", source="will@newton", state="waiting"):
    return Session(SessionRef(ServerRef(source, "", 0, 0, "alan"), addr),
                   addr, 0, 0, 0, 1, "alan", "", "", kind, state)


def qualified(source, *items):
    return {f"alan:{source}:{item['addr']}": item for item in items}


def test_canonical_actor_is_closed_label_enriched_and_discards_viewport(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    alan.rename("codex-a@newton", "work")
    item = alan.canonical_actor({"addr": "codex-a@newton", "kind": "codex",
                                 "viewport": {"x": 1}, "future": "no"})
    assert set(item) == FIELDS
    assert item["label"] == "work"
    assert "viewport" not in item


def test_watcher_requests_actor_only_observation():
    changed = queue.Queue(); stop = threading.Event()
    observation = [actor("codex-a@newton")]
    class Stream:
        def next(self, callback, lock):
            with lock: callback(observation, {"kind": "replace"})
            stop.set()
        def close(self): pass
    with mock.patch.object(alan.loop, "observe", return_value=Stream()) as observe:
        watcher = alan.Watcher(changed, stop); watcher._thread.join(1)
    observe.assert_called_once_with(stream=True, actors=True)
    with watcher.snapshot() as catalogue:
        assert catalogue == [actor("codex-a@newton")]


def test_viewport_only_stream_delta_does_not_signal_a_catalogue_change():
    watcher = object.__new__(alan.Watcher)
    watcher._lock = threading.RLock(); watcher._descriptors = {}
    watcher.actors = []; watcher.available = False; watcher.error = None
    watcher.initialized = threading.Event()
    watcher._changed = queue.Queue(); watcher._consumer = threading.Event()
    observations = [
        [{**actor("codex-a@newton"), "viewport": {"offset": offset}}]
        for offset in (1, 2)
    ]

    class Stream:
        def __init__(self): self.index = 0
        def next(self, callback, lock):
            if self.index == len(observations): raise StopIteration
            with lock: callback(observations[self.index], {
                "kind": "replace" if self.index == 0 else "delta"})
            self.index += 1
            return observations[self.index - 1]
        def close(self): pass

    with mock.patch.object(alan.loop, "observe", return_value=Stream()):
        watcher._run()
    assert watcher._changed.qsize() == 1


def test_real_actor_stream_viewport_churn_adds_no_host_publication_bytes(
        tmp_path, monkeypatch):
    path = tmp_path / "alan.sock"
    stop = threading.Event()
    served = threading.Event()
    item = actor("codex-a@newton", created="2026-01-01T00:00:00Z",
                 last_operation_activity="2026-01-01T00:00:00Z")

    def serve():
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            listener.listen()
            with listener.accept()[0] as connection:
                reader = connection.makefile("rb")
                request = json.loads(reader.readline())
                assert request == {"op": "observe", "stream": True,
                                   "scope": "actors"}
                graph = {"directed": True, "multigraph": True,
                         "graph": {"actors": [{**item, "viewport": {"text": "one"}}]},
                         "nodes": [], "edges": [], "generation": 1, "revision": 0}
                connection.sendall((json.dumps({"ok": True, "observation": {
                    "kind": "replace", "graph": graph}}) + "\n").encode())
                reader.readline()
                connection.sendall((json.dumps({"ok": True, "observation": {
                    "kind": "delta", "generation": 1, "revision": 1,
                    "actors": [{**item, "viewport": {"text": "two"}}],
                    "nodes": [], "edges": []}}) + "\n").encode())
                reader.readline()
                served.set()
                stop.wait()

    server = threading.Thread(target=serve)
    server.start()
    while not path.exists():
        time.sleep(.001)
    monkeypatch.setenv("LOOP_SOCKET", str(path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    changed = queue.Queue()
    watcher = alan.Watcher(changed, stop)
    assert served.wait(2)
    assert changed.get(timeout=2) == "alan"
    assert changed.empty()
    with watcher.snapshot() as catalogue:
        initial = encode_observation([], True, catalogue)
    assert all(word not in initial for word in ('viewport', 'nodes', 'edges'))

    alan.rename(item["addr"], "renamed remotely")
    assert changed.get(timeout=2) == "alan"
    with watcher.snapshot() as catalogue:
        renamed = encode_observation([], True, catalogue)

    source = RuntimeSource("newton", "will", str(path), "/run/tmux")
    fleet = Fleet()
    fleet.unavailable.discard(source.key)
    fleet.update_source(source, initial)
    assert fleet.sessions[source.key][0].name == item["addr"]
    fleet.update_source(source, renamed)
    assert fleet.sessions[source.key][0].name == "renamed remotely"
    stop.set()
    server.join(2)
    watcher._thread.join(2)


def test_inventory_derives_summary_activity_and_evaluation():
    principal = "will@lovelace"
    item = actor("codex-a@newton", created="2026-01-01T00:00:00Z",
                 last_operation_activity="2026-01-02T00:00:00Z",
                 evaluation_started="2026-01-02T01:00:00Z",
                 active_evaluation="codex-a@newton#4", worked=True,
                 latest_displayable_output={"status": "ok", "value": "a  result"},
                 source_activity={principal: "2026-01-03T00:00:00Z"})
    [result] = alan.inventory("will@newton", [item], {principal})
    assert result.summary == "a result"
    assert result.evaluation == "codex-a@newton#4"
    assert result.human_activity > result.created


def test_inventory_uses_remote_catalogue_label_without_local_label_lookup():
    item = actor("codex-a@newton", label=None,
                 created="2026-01-01T00:00:00Z",
                 last_operation_activity="2026-01-01T00:00:00Z")
    with mock.patch.object(alan, "label", side_effect=AssertionError):
        [result] = alan.inventory("will@newton", [item])
    assert result.name == "codex-a@newton"


def test_cross_runtime_parent_refolds_and_missing_parent_surfaces_as_root():
    parent = actor("codex-parent@newton", evaluator="native")
    child = actor("claude-child@lovelace", spawn="codex-parent@newton#1")
    sessions = [session(parent["addr"]), session(child["addr"], "claude", "will@lovelace")]
    catalogue = {**qualified("will@newton", parent), **qualified("will@lovelace", child)}
    projected = alan.project(sessions, catalogue,
                             expanded={"alan:will@newton:codex-parent@newton"})
    assert [(item.session.agent, item.depth) for item in projected] == [("codex", 0), ("claude", 1)]
    [root] = alan.project(sessions[1:], qualified("will@lovelace", child))
    assert root.depth == 0


def test_truly_unattached_non_native_actor_remains_hidden():
    unattached = actor("claude-unattached@lovelace", "claude", spawn=None)
    assert alan.project([session(unattached["addr"], "claude", "will@lovelace")],
                        qualified("will@lovelace", unattached)) == []


def test_orphaned_known_subtree_surfaces_at_terminal_known_ancestor():
    terminal = actor("python-terminal@lovelace", "python",
                     spawn="codex-absent@newton#1", state="hibernated")
    child = actor("claude-child@lovelace", "claude",
                  spawn="python-terminal@lovelace#2")
    sessions = [session(terminal["addr"], "python", "will@lovelace", "hibernated"),
                session(child["addr"], "claude", "will@lovelace")]
    catalogue = qualified("will@lovelace", terminal, child)
    projected = alan.project(sessions, catalogue,
                             expanded={"alan:will@lovelace:python-terminal@lovelace"})
    assert [(item.session.ref.key, item.depth) for item in projected] == [
        ("alan:will@lovelace:python-terminal@lovelace", 0),
        ("alan:will@lovelace:claude-child@lovelace", 1)]


def test_hidden_python_compresses_to_nearest_visible_creator():
    principal = actor("will@newton", "principal")
    root = actor("codex-root@newton", spawn="will@newton#1")
    hidden = actor("python-hidden@newton", "python", spawn="codex-root@newton#2")
    child = actor("claude-child@newton", "claude", spawn="python-hidden@newton#3")
    catalogue = qualified("will@newton", principal, root, hidden, child)
    projected = alan.project([session(root["addr"]), session(hidden["addr"], "python"),
                              session(child["addr"], "claude")], catalogue,
                             expanded={"alan:will@newton:codex-root@newton"})
    assert [(item.session.agent, item.depth) for item in projected] == [("codex", 0), ("claude", 1)]


def test_principal_request_attention_is_owned_by_target_principal():
    principal = actor("will@newton", "principal", unresolved_requests={
        "codex-a@newton#4": {"time": "2026-01-01T00:00:00Z", "payload": "choose"}})
    requester = actor("codex-a@newton", spawn="will@newton#1")
    [projected] = alan.project([session(requester["addr"])],
                               qualified("will@newton", principal, requester))
    assert projected.session.state == "needs-action"
    assert projected.session.summary == "1 awaiting — choose"


def test_attention_moves_to_expanded_requester_and_clears_by_exact_principal():
    first = actor("will@newton", "principal", unresolved_requests={
        "claude-child@newton#4": {
            "time": "2026-01-03T00:00:00Z", "payload": "first"}})
    second = actor("sophie@newton", "principal", unresolved_requests={
        "claude-child@newton#5": {
            "time": "2026-01-04T00:00:00Z", "payload": "other"}})
    root = actor("codex-root@newton", spawn="will@newton#1")
    child = actor("claude-child@newton", "claude", spawn="codex-root@newton#2")
    catalogue = qualified("will@newton", first, second, root, child)
    sessions = [session(root["addr"]), session(child["addr"], "claude")]

    [collapsed] = alan.project(sessions, catalogue)
    assert collapsed.session.state == "needs-action"
    assert collapsed.session.summary == "1 awaiting — first"

    expanded = alan.project(
        sessions, catalogue, expanded={"alan:will@newton:codex-root@newton"})
    assert [item.session.state for item in expanded] == ["waiting", "needs-action"]
    assert expanded[1].session.summary == "1 awaiting — first"

    first["unresolved_requests"] = {}
    cleared = alan.project(
        sessions, catalogue, expanded={"alan:will@newton:codex-root@newton"})
    assert [item.session.state for item in cleared] == ["waiting", "waiting"]


def test_remote_principal_activity_orders_the_actor_as_most_recent():
    local = actor("will@newton", "principal")
    remote = actor("will@lovelace", "principal")
    active = actor(
        "codex-active@newton", spawn="will@newton#1",
        created="2026-01-01T00:00:00Z",
        last_operation_activity="2026-01-01T00:00:00Z",
        source_activity={"will@lovelace": "2026-01-03T00:00:00Z"})
    newer = actor(
        "codex-newer@newton", spawn="will@newton#2",
        created="2026-01-02T00:00:00Z",
        last_operation_activity="2026-01-02T00:00:00Z")
    fleet = Fleet()
    fleet.catalogues = {
        "will@newton": [local, active, newer],
        "will@lovelace": [remote],
    }
    fleet.tmux_sessions = {"will@newton": [], "will@lovelace": []}
    fleet.rebuild_sessions()
    projected = fleet.projected()
    assert [item.session.ref.session_id for item in projected] == [
        "codex-active@newton", "codex-newer@newton"]


def test_duplicate_raw_address_fails_visibly():
    item = actor("codex-a@newton")
    catalogue = {**qualified("will@newton", item), **qualified("sophie@newton", item)}
    with pytest.raises(RuntimeError, match="multiple Alan runtime sources"):
        alan.project([], catalogue)


def test_host_protocol_rejects_missing_and_extra_actor_fields():
    source = RuntimeSource("newton", "will", "/run/alan", "/run/tmux")
    item = actor("codex-a@newton")
    assert decode_observation(encode_observation([], True, [item]), source)[2] == [item]
    for invalid in ({key: value for key, value in item.items() if key != "worked"},
                    {**item, "viewport": {}}):
        with pytest.raises(ValueError, match="invalid Fleet actor"):
            decode_observation(encode_observation([], True, [invalid]), source)


def test_preview_is_actor_scoped_and_closes_observation():
    graph = nx.MultiDiGraph()
    graph.add_node("python-a@newton#0", stream="python-a@newton", op="input", payload="x")
    graph.add_node("python-a@newton#1", stream="python-a@newton", op="output",
                   status="ok", value="y")
    stream = mock.Mock(); stream.__next__ = mock.Mock(return_value=graph)
    with mock.patch.object(alan.loop, "observe", return_value=stream) as observe:
        assert alan.preview("python-a@newton") == "Input\nx\n\nOk\ny\n"
    observe.assert_called_once_with(stream=True, actor="python-a@newton")
    stream.close.assert_called_once_with()
