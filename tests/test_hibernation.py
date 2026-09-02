from dataclasses import replace
import asyncio
from unittest import mock

import pytest

from agent_fleet import actions, alan, authority
from agent_fleet.daemon import Fleet
from agent_fleet.hibernate_idle import candidates, duration, reason
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


def test_hibernated_state_is_visible_and_counted():
    actor = session(state="hibernated")
    projected = [alan.Projected(actor, 0, 0, False)]
    assert "1 hibernated" in column_header(projected)
    assert "z" in rows_text(projected, [], 120, now=100)


def test_hibernated_python_is_visible_when_live_python_is_hidden():
    root = session("codex")
    live = session("python")
    asleep = replace(live, ref=SessionRef(live.ref.server, "python-asleep@lovelace"),
                     reported_state="hibernated")
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


def test_idle_policy_selects_only_old_supported_waiting_unattached_actors():
    old = session(last_activity=10)
    values = [old, replace(old, ref=SessionRef(old.ref.server, "codex-new@lovelace"),
                           recency=90),
              replace(old, ref=SessionRef(old.ref.server, "codex-busy@lovelace"),
                      reported_state="working"),
              replace(old, ref=SessionRef(old.ref.server, "codex-viewed@lovelace"),
                      attached=1),
              session("grok", last_activity=10)]
    assert list(candidates(values, 50, now=100)) == [old]
    assert duration("48h") == 172800
    assert reason(values[1], 50, 100) == "recent"
    assert reason(values[2], 50, 100) == "working"
    assert reason(values[3], 50, 100) == "attached"
    assert reason(values[4], 50, 100) == "unsupported-evaluator"


def test_fold_combines_actor_and_provider_activity_and_attachment():
    actor = session(last_activity=10)
    provider = Session(
        SessionRef(ServerRef("will@lovelace", "/tmp/tmux", 1, 2), "$1"),
        "native", 1, 20, 1, 1, "codex", "", "/work", "codex", "waiting",
        recency=30, transcript_id="identity")
    [folded] = fold_adopted([actor, provider])
    assert folded.recency == 30
    assert folded.attached == 1


def test_authority_hibernates_only_the_exact_alan_actor():
    with mock.patch("agent_fleet.authority.alan.hibernate") as hibernate:
        assert authority.execute({"operation": "hibernate-alan",
                                  "actor": "codex-identity@lovelace"}) == {}
    hibernate.assert_called_once_with("codex-identity@lovelace")


def test_public_hibernate_action_uses_one_fleet_request():
    with mock.patch("agent_fleet.actions.fleet_action") as action:
        actions.hibernate("alan:will@lovelace:codex-identity@lovelace")
    action.assert_called_once_with({
        "operation": "hibernate",
        "source": "alan:will@lovelace:codex-identity@lovelace",
    })


def test_daemon_hibernate_uses_alan_and_waits_for_same_row():
    actor = replace(session(), evaluator="native", transcript_path="/transcript")
    fleet = fleet_with(actor)

    async def mutate(_source, request):
        assert request == {"operation": "hibernate-alan",
                           "actor": actor.ref.session_id}
        fleet.sessions[actor.ref.server.source] = [
            replace(actor, reported_state="hibernated")]
        return {}

    async def wait(predicate, _description):
        [current] = fleet.sessions[actor.ref.server.source]
        assert predicate(current)
        return current

    with mock.patch.object(fleet, "authority", side_effect=mutate) as execute, \
         mock.patch.object(fleet, "wait_for_source", side_effect=wait) as waiting:
        assert asyncio.run(fleet.action({"operation": "hibernate",
                                        "source": actor.ref.key})) == {}
    execute.assert_awaited_once()
    waiting.assert_awaited_once()


def test_daemon_hibernate_refuses_ineligible_rows_before_authority():
    base = replace(session(), evaluator="native", transcript_path="/transcript")
    cases = [replace(base, reported_state="working"),
             replace(base, attached=1),
             session("grok"),
             replace(base, hibernation="unsupported"),
             replace(base, reported_state="unavailable", transcript_path=""),
             replace(base, reported_state="unavailable", managed=True)]
    for actor in cases:
        fleet = fleet_with(actor)
        with mock.patch.object(fleet, "authority", mock.AsyncMock()) as execute:
            with __import__("pytest").raises(ValueError):
                asyncio.run(fleet.action({"operation": "hibernate",
                                          "source": actor.ref.key}))
        execute.assert_not_awaited()


def test_unavailable_recovery_requires_catalogued_full_native_transcript():
    actor = replace(session(state="unavailable"), evaluator="native",
                    transcript_path="/transcript")
    [recovery] = fold_adopted([actor])
    assert recovery.ref == actor.ref
    assert "hibernation recovery" in recovery.summary


def test_concurrent_hibernated_opens_share_one_alan_resume():
    actor = replace(session(state="hibernated"), evaluator="native",
                    transcript_path="/transcript")
    unavailable = replace(actor, reported_state="unavailable")
    restored = replace(actor, reported_state="waiting",
                       attachment=SessionRef(
                           ServerRef("will@lovelace", "/tmp/tmux", 1, 2), "$1"))
    fleet = fleet_with(actor)
    launched = asyncio.Event()
    release = asyncio.Event()

    async def resume(*_args):
        launched.set()
        await release.wait()
        fleet.sessions[actor.ref.server.source] = [restored]

    async def wait(predicate, _description):
        assert not predicate(unavailable)
        assert predicate(restored)
        return restored

    async def exercise():
        with mock.patch.object(fleet, "authority", side_effect=resume) as execute, \
             mock.patch.object(fleet, "wait_for_source", side_effect=wait) as waiting:
            first = asyncio.create_task(fleet.ensure_attachment(actor.ref.key))
            await launched.wait()
            second = asyncio.create_task(fleet.ensure_attachment(actor.ref.key))
            release.set()
            assert await asyncio.gather(first, second) == [restored, restored]
        execute.assert_awaited_once_with(actor.ref.server.source, {
            "operation": "restore-alan", "actor": actor.ref.session_id})
        waiting.assert_awaited_once()

    asyncio.run(exercise())


def test_restore_waits_for_a_live_alan_actor():
    actor = replace(session(state="hibernated"), evaluator="native", managed=True)
    unavailable = replace(actor, reported_state="unavailable")
    restored = replace(actor, reported_state="waiting")
    graph = __import__("networkx").MultiDiGraph()
    graph.graph["actors"] = [{"addr": actor.ref.key, "evaluator": "native",
                              "managed": True}]
    fleet = fleet_with(actor)

    async def wait(predicate, _description):
        assert not predicate(actor)
        assert not predicate(unavailable)
        assert predicate(restored)
        return restored

    with mock.patch.object(fleet, "composed_graph", return_value=graph), \
            mock.patch.object(fleet, "authority", return_value={}) as execute, \
            mock.patch.object(fleet, "wait_for_source", side_effect=wait):
        value = asyncio.run(fleet.action({
            "operation": "restore", "history": actor.ref.key, "name": ""}))

    assert value == {"source": actor.ref.key}
    execute.assert_awaited_once_with(
        actor.ref.server.source,
        {"operation": "restore-alan", "actor": actor.ref.session_id})


def test_policy_dry_run_reports_reasons_without_mutation(monkeypatch, capsys):
    from agent_fleet import hibernate_idle
    old = session(last_activity=10)
    recent = replace(old, ref=SessionRef(old.ref.server, "codex-new@lovelace"),
                     recency=90)
    monkeypatch.setattr(hibernate_idle, "snapshot", lambda: "snapshot")
    monkeypatch.setattr(hibernate_idle, "decode_message",
                        lambda _raw: ([old, recent], {}, []))
    mutate = mock.Mock()
    monkeypatch.setattr(hibernate_idle, "hibernate", mutate)
    monkeypatch.setattr("sys.argv", ["fleet-hibernate-idle", "--older-than", "50",
                                     "--dry-run"])
    monkeypatch.setattr(hibernate_idle.time, "time", lambda: 100)
    hibernate_idle.main()
    output = capsys.readouterr().out
    assert "\teligible\n" in output
    assert "\trecent\n" in output
    mutate.assert_not_called()


def test_policy_submits_only_eligible_exact_keys(monkeypatch, capsys):
    from agent_fleet import hibernate_idle
    old = session(last_activity=10)
    recent = replace(old, ref=SessionRef(old.ref.server, "codex-new@lovelace"),
                     recency=90)
    monkeypatch.setattr(hibernate_idle, "snapshot", lambda: "snapshot")
    monkeypatch.setattr(hibernate_idle, "decode_message",
                        lambda _raw: ([old, recent], {}, []))
    mutate = mock.Mock()
    monkeypatch.setattr(hibernate_idle, "hibernate", mutate)
    monkeypatch.setattr("sys.argv", ["fleet-hibernate-idle", "--older-than", "50"])
    monkeypatch.setattr(hibernate_idle.time, "time", lambda: 100)
    hibernate_idle.main()
    assert capsys.readouterr().out.count("\n") == 1
    mutate.assert_called_once_with(old.ref.key)


def test_policy_reports_failure_and_continues(monkeypatch, capsys):
    from agent_fleet import hibernate_idle
    first = session(last_activity=10)
    second = replace(first, ref=SessionRef(first.ref.server, "codex-old@lovelace"))
    monkeypatch.setattr(hibernate_idle, "snapshot", lambda: "snapshot")
    monkeypatch.setattr(hibernate_idle, "decode_message",
                        lambda _raw: ([first, second], {}, []))
    mutate = mock.Mock(side_effect=[RuntimeError("actor_not_idle"), None])
    monkeypatch.setattr(hibernate_idle, "hibernate", mutate)
    monkeypatch.setattr("sys.argv", ["fleet-hibernate-idle", "--older-than", "50"])
    monkeypatch.setattr(hibernate_idle.time, "time", lambda: 100)
    with pytest.raises(SystemExit) as error:
        hibernate_idle.main()
    assert error.value.code == 1
    assert capsys.readouterr().err == f"{first.ref.key}: actor_not_idle\n"
    assert mutate.call_args_list == [mock.call(first.ref.key), mock.call(second.ref.key)]


def test_hibernation_capability_schema_is_protocol_version_four():
    import json
    import pytest

    message = json.loads(encode([session()]))
    assert message["version"] == 4
    message["version"] = 3
    with pytest.raises(ValueError, match="unsupported Fleet protocol version 3"):
        decode_message(json.dumps(message))
