from dataclasses import replace
import asyncio
from unittest import mock

from agent_fleet import actions, alan, authority
from agent_fleet.daemon import Fleet
from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.protocol import decode_message, encode
from agent_fleet.render import column_header, rows_text
from agent_fleet.transcripts import fold_adopted


def session(agent="codex", state="waiting", *, attached=0, last_activity=1):
    server = ServerRef("will@lovelace", "", 0, 0, "alan")
    actor = f"{agent}-identity@lovelace"
    return Session(SessionRef(server, actor), "actor", 1, 0, attached, 1,
                   "alan", "", "/work", agent, state,
                   recency=last_activity, transcript_id="identity",
                   hibernation="exact" if agent == "python" else
                   "transcript" if agent in {"claude", "codex"} else "unsupported")


def fleet_with(item):
    fleet = Fleet()
    fleet.sessions = {item.ref.server.source: [item]}
    fleet.unavailable.clear()
    return fleet


def test_stopped_state_is_visible_and_counted():
    actor = session(state="stopped")
    projected = [alan.Projected(actor, 0, 0, False)]
    assert "1 stopped" in column_header(projected)
    assert "z" in rows_text(projected, [], 120, now=100)


def test_stopped_python_is_visible_when_live_python_is_hidden():
    root = session("codex")
    live = session("python")
    asleep = replace(live, ref=SessionRef(live.ref.server, "python-asleep@lovelace"),
                     reported_state="stopped")
    graph = __import__("networkx").MultiDiGraph()
    graph.graph["actors"] = [
        {"addr": item.ref.session_id, "kind": item.agent, "state": item.state,
         **({"evaluator": "native"} if item is root else {})}
        for item in (root, live, asleep)
    ]
    graph.add_node("codex-identity@lovelace#1", stream=root.ref.session_id)
    graph.add_node("python-identity@lovelace#0", stream=live.ref.session_id)
    graph.add_node("python-asleep@lovelace#0", stream=asleep.ref.session_id)
    graph.add_edge("codex-identity@lovelace#1", "python-identity@lovelace#0", key="spawn")
    graph.add_edge("codex-identity@lovelace#1", "python-asleep@lovelace#0", key="spawn")
    projected = alan.project([root, live, asleep], graph,
                             expanded={root.ref.session_id}, show_python=False)
    assert [item.session.ref for item in projected] == [root.ref, asleep.ref]


def test_fold_combines_actor_and_provider_activity_and_attachment():
    actor = session(last_activity=10)
    provider = Session(
        SessionRef(ServerRef("will@lovelace", "/tmp/tmux", 1, 2), "$1"),
        "native", 1, 20, 1, 1, "codex", "", "/work", "codex", "waiting",
        recency=30, transcript_id="identity")
    [folded] = fold_adopted([actor, provider])
    assert folded.recency == 30
    assert folded.attached == 1


def test_authority_stops_only_the_exact_alan_actor():
    with mock.patch("agent_fleet.authority.alan.stop") as stop:
        assert authority.execute({"operation": "stop-alan",
                                  "actor": "codex-identity@lovelace"}) == {}
    stop.assert_called_once_with("codex-identity@lovelace")


def test_public_stop_action_uses_one_fleet_request():
    with mock.patch("agent_fleet.actions.fleet_action") as action:
        actions.stop("alan:will@lovelace:codex-identity@lovelace")
    action.assert_called_once_with({
        "operation": "stop",
        "source": "alan:will@lovelace:codex-identity@lovelace",
    })


def test_daemon_stop_uses_alan_and_waits_for_same_row():
    actor = replace(session(), evaluator="native", transcript_path="/transcript")
    fleet = fleet_with(actor)

    async def mutate(_source, request):
        assert request == {"operation": "stop-alan",
                           "actor": actor.ref.session_id}
        fleet.sessions[actor.ref.server.source] = [
            replace(actor, reported_state="stopped")]
        return {}

    async def wait(predicate, _description):
        [current] = fleet.sessions[actor.ref.server.source]
        assert predicate(current)
        return current

    with mock.patch.object(fleet, "authority", side_effect=mutate) as execute, \
         mock.patch.object(fleet, "wait_for_source", side_effect=wait) as waiting:
        assert asyncio.run(fleet.action({"operation": "stop",
                                        "source": actor.ref.key})) == {}
    execute.assert_awaited_once()
    waiting.assert_awaited_once()


def test_daemon_stop_refuses_ineligible_rows_before_authority():
    base = replace(session(), evaluator="native", transcript_path="/transcript")
    cases = [replace(base, reported_state="working"),
             replace(base, attached=1),
             session("grok"),
             replace(base, hibernation="unsupported"),
             replace(base, reported_state="failed", transcript_path=""),
             replace(base, reported_state="failed", managed=True)]
    for actor in cases:
        fleet = fleet_with(actor)
        with mock.patch.object(fleet, "authority", mock.AsyncMock()) as execute:
            with __import__("pytest").raises(ValueError):
                asyncio.run(fleet.action({"operation": "stop",
                                          "source": actor.ref.key}))
        execute.assert_not_awaited()


def test_unavailable_recovery_requires_catalogued_full_native_transcript():
    actor = replace(session(state="failed"), evaluator="native",
                    transcript_path="/transcript")
    [recovery] = fold_adopted([actor])
    assert recovery.ref == actor.ref
    assert "stop recovery" in recovery.summary


def test_stopped_open_does_not_start_an_evaluator():
    for agent in ("codex", "claude", "grok", "python", "llm"):
        for managed in (False, True):
            actor = replace(session(agent, state="stopped"), evaluator="native",
                            managed=managed, transcript_path="/transcript")
            graph = __import__("networkx").MultiDiGraph()
            graph.graph["actors"] = [{"addr": actor.ref.key, "evaluator": "native",
                                      "managed": managed}]
            fleet = fleet_with(actor)
            with mock.patch.object(fleet, "composed_graph", return_value=graph), \
                 mock.patch.object(fleet, "authority", mock.AsyncMock()) as execute, \
                 mock.patch.object(fleet, "wait_for_source", mock.AsyncMock()) as waiting:
                assert asyncio.run(fleet.ensure_attachment(actor.ref.key)) == actor
            execute.assert_not_called()
            waiting.assert_not_called()
            assert fleet.restores == {}


def test_history_reopens_alan_actor_without_waiting_for_an_evaluator():
    actor = replace(session(state="closed"), evaluator="native", managed=True)
    graph = __import__("networkx").MultiDiGraph()
    graph.graph["actors"] = [{"addr": actor.ref.key, "evaluator": "native",
                              "managed": True, "state": "closed"}]
    fleet = fleet_with(actor)

    with mock.patch.object(fleet, "composed_graph", return_value=graph), \
            mock.patch.object(fleet, "authority", return_value={}) as execute, \
            mock.patch.object(fleet, "wait_for_source", mock.AsyncMock()) as waiting:
        value = asyncio.run(fleet.action({
            "operation": "restore", "history": actor.ref.key, "name": ""}))

    assert value == {"source": actor.ref.key}
    execute.assert_awaited_once_with(
        actor.ref.server.source,
        {"operation": "open-alan", "actor": actor.ref.session_id})
    waiting.assert_not_called()


def test_hibernation_capability_schema_is_protocol_version_three():
    import json
    import pytest

    message = json.loads(encode([session()]))
    assert message["version"] == 3
    message["version"] = 2
    with pytest.raises(ValueError, match="unsupported Fleet protocol version 2"):
        decode_message(json.dumps(message))
