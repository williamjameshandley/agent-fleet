from dataclasses import replace
import asyncio
from unittest import mock

from agent_fleet import actions, alan, authority
from agent_fleet.daemon import Fleet
from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.protocol import (decode_message, encode, decode_observation,
                                  encode_observation)
from agent_fleet.render import column_header, rows_text
from agent_fleet.transcripts import fold_adopted


def session(agent="codex", state="waiting", *, attached=0, last_activity=1):
    server = ServerRef("will@lovelace", "", 0, 0, "alan")
    actor = f"{agent}-identity@lovelace"
    return Session(SessionRef(server, actor), "actor", 1, 0, attached, 1,
                   "alan", "", "/work", agent, state,
                   recency=last_activity, transcript_id="identity",
                   stop="exact" if agent == "python" else
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
             replace(base, stop="unsupported"),
             replace(base, reported_state="failed", transcript_path=""),
             replace(base, reported_state="failed", managed=True)]
    for actor in cases:
        fleet = fleet_with(actor)
        with mock.patch.object(fleet, "authority", mock.AsyncMock()) as execute:
            with __import__("pytest").raises(ValueError):
                asyncio.run(fleet.action({"operation": "stop",
                                          "source": actor.ref.key}))
        execute.assert_not_awaited()


def test_failed_recovery_requires_catalogued_full_native_transcript():
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


def test_stop_capability_schema_is_protocol_version_three():
    import json
    import pytest

    message = json.loads(encode([session()]))
    assert message["version"] == 3
    message["version"] = 2
    with pytest.raises(ValueError, match="unsupported Fleet protocol version 2"):
        decode_message(json.dumps(message))


def test_protocol_accepts_stop_and_rejects_hibernation():
    import json
    import pytest
    from agent_fleet.config import RuntimeSource

    actor = session()
    source = RuntimeSource("lovelace", "will", "/tmp/alan", "/tmp/tmux")
    for encoded, decode in (
        (encode([actor]), decode_message),
        (encode_observation([actor], True, None),
         lambda raw: decode_observation(raw, source)),
    ):
        message = json.loads(encoded)
        item = message["sessions"][0]
        assert item["stop"] == "transcript"
        assert decode(encoded)[0] == [actor]
        item["hibernation"] = item["stop"]
        with pytest.raises(ValueError, match="invalid Fleet session"):
            decode(json.dumps(message))
        del item["stop"]
        with pytest.raises(ValueError, match="invalid Fleet session"):
            decode(json.dumps(message))


def test_lifecycle_rows_preserve_state_without_controlling_actors():
    from agent_fleet import render

    with mock.patch.object(alan.loop, "control") as control, \
         mock.patch.object(authority, "execute") as execute:
        for kind in ("codex", "claude", "grok", "python", "llm"):
            rows = {}
            for state, marker in (("working", "*"), ("waiting", "."),
                                  ("stopped", "z"), ("failed", "!"), ("closed", "")):
                actor = f"{kind}-identity@lovelace"
                descriptor = {
                    "addr": actor, "kind": kind, "state": state,
                    "created": 1, "human_activity": 0, "evaluation_started": 0,
                    "last_operation_activity": "2026-07-30T12:00:01Z",
                    "stop": "exact" if kind == "python" else "transcript",
                    "evaluator": "native" if kind in {"codex", "claude", "grok"}
                    else kind,
                }
                graph = __import__("networkx").MultiDiGraph()
                graph.graph["actors"] = [descriptor, {"addr": "will@lovelace",
                                                       "kind": "principal"}]
                graph.add_node("will@lovelace#0", stream="will@lovelace")
                graph.add_node(actor + "#0", stream=actor)
                graph.add_edge("will@lovelace#0", actor + "#0", key="spawn")
                inventory = alan.inventory("will@lovelace", [descriptor])
                decoded, _, _ = decode_message(encode(inventory))
                projected = render.order(fold_adopted(decoded), [], graph, show_python=True)
                if state == "closed":
                    assert projected == []
                    continue
                assert projected[0].session.state == state
                rows[state] = rows_text(projected, [], 120, now=100)
                assert render.STATE_COLOUR[state] + marker + render.RESET in rows[state]
                if kind == "python" and state in {"stopped", "failed"}:
                    assert render.order(fold_adopted(decoded), [], graph) == projected
            assert rows["failed"] != rows["stopped"]
        control.assert_not_called()
        execute.assert_not_called()


def test_failed_actor_keeps_failure_with_provider_presentations():
    actor = replace(session(state="failed"), evaluator="native")
    provider = Session(
        SessionRef(ServerRef("will@lovelace", "/tmp/tmux", 1, 2), "$1"),
        "native", 1, 20, 1, 1, "codex", "", "/work", "codex", "waiting",
        transcript_id="identity")
    duplicate = replace(provider, ref=replace(provider.ref, session_id="$2"))
    for providers in ([], [provider], [provider, duplicate]):
        projected = fold_adopted([actor, *providers])
        assert projected[0].ref == actor.ref
        assert projected[0].state == "failed"
