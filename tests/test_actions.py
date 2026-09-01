import asyncio
import json
import os
import shlex
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

from agent_fleet import authority
from agent_fleet import actions
from agent_fleet import config
from agent_fleet import daemon
from agent_fleet.daemon import Fleet
from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.transcripts import fold_adopted


TARGET = ["/tmp/tmux", 1, 1, "$1"]


def session(host="lovelace", kind="alan", agent="codex", transcript="thread-1"):
    source = f"will@{host}"
    server = (ServerRef(source, "", 0, 0, "alan") if kind == "alan" else
              ServerRef(source, "/tmp/tmux/default", 12, 10))
    identity = f"{agent}-1@{host}" if kind == "alan" else "$1"
    return Session(SessionRef(server, identity), "work", 1, 2, 0, 1,
                   "tmux", "", "/work", agent, "waiting", "", 0,
                   transcript)


def test_authority_create_returns_the_exact_actor_address():
    for agent in ("claude", "codex", "grok", "antigravity", "llm"):
        with mock.patch("agent_fleet.authority.alan.create",
                        return_value=f"{agent}-full@lovelace") as create:
            value = authority.execute({"operation": "create", "agent": agent,
                                       "name": "work", "cwd": "/work"})
        assert value == {"source": f"alan:{agent}-full@lovelace"}
        create.assert_called_once_with(agent, "work", "/work")

    try:
        authority.execute({"operation": "create", "agent": "python",
                           "name": "work", "cwd": "/work"})
    except ValueError as error:
        assert "language-actor kind" in str(error)
    else:
        raise AssertionError("authority created a non-language actor")


def test_authority_create_resolves_an_empty_directory_as_the_source_home():
    with mock.patch("agent_fleet.authority.Path.home",
                    return_value=Path("/home/sophie")), \
         mock.patch("agent_fleet.authority.alan.create",
                    return_value="codex-full@lovelace") as create:
        authority.execute({"operation": "create", "agent": "codex",
                           "name": "work", "cwd": ""})
    create.assert_called_once_with("codex", "work", "/home/sophie")


def test_authority_operations_are_finite_and_direct():
    with mock.patch("agent_fleet.authority.alan.rename") as rename:
        assert authority.execute({"operation": "rename-alan", "actor": "codex-1@lovelace",
                                  "name": "new"}) == {"name": "new"}
    rename.assert_called_once_with("codex-1@lovelace", "new")

    with mock.patch("agent_fleet.authority.tmux.mutate_target") as mutate:
        authority.execute({"operation": "rename-tmux", "target": TARGET,
                           "name": "new"})
    mutate.assert_called_once_with(TARGET, "rename", ["new"])

    with mock.patch("agent_fleet.authority.alan.retire") as retire, \
         mock.patch("agent_fleet.authority.presentation.close") as close:
        assert authority.execute({"operation": "archive-alan",
                                  "actor": "codex-1@lovelace",
                                  "agent": "codex"}) == {}
    retire.assert_called_once_with("codex-1@lovelace")
    close.assert_not_called()

    with mock.patch("agent_fleet.authority.alan.retire"), \
         mock.patch("agent_fleet.authority.presentation.close") as close:
        authority.execute({"operation": "archive-alan",
                           "actor": "llm-1@lovelace", "agent": "llm"})
    close.assert_called_once_with("llm-1@lovelace")

    calls = []
    with mock.patch("agent_fleet.authority.presentation.close",
                    side_effect=lambda actor: calls.append(("close", actor))), \
         mock.patch("agent_fleet.authority.alan.retire",
                    side_effect=lambda actor: calls.append(("retire", actor))):
        authority.execute({"operation": "archive-alan",
                           "actor": "llm-ordered@lovelace", "agent": "llm"})
    assert calls == [("close", "llm-ordered@lovelace"),
                     ("retire", "llm-ordered@lovelace")]

    with mock.patch("agent_fleet.authority.presentation.close",
                    side_effect=RuntimeError("tmux failed")), \
         mock.patch("agent_fleet.authority.alan.retire") as retire:
        with pytest.raises(RuntimeError, match="tmux failed"):
            authority.execute({"operation": "archive-alan",
                               "actor": "llm-retry@lovelace", "agent": "llm"})
    retire.assert_not_called()

    with mock.patch("agent_fleet.authority.presentation.close") as close, \
         mock.patch("agent_fleet.authority.alan.retire",
                    side_effect=RuntimeError("retire failed")):
        with pytest.raises(RuntimeError, match="retire failed"):
            authority.execute({"operation": "archive-alan",
                               "actor": "llm-rebuild@lovelace", "agent": "llm"})
    close.assert_called_once_with("llm-rebuild@lovelace")

    with mock.patch("agent_fleet.authority.alan.resume",
                    return_value="codex-1@lovelace") as resume:
        assert authority.execute({"operation": "restore-alan",
                                  "actor": "codex-1@lovelace"}) == {
                                      "source": "alan:codex-1@lovelace"}
    resume.assert_called_once_with("codex-1@lovelace")


def test_authority_archive_verifies_recovery_before_exact_tmux_kill():
    calls = mock.Mock()
    with mock.patch("agent_fleet.authority.transcripts.verify") as verify, \
         mock.patch("agent_fleet.authority.tmux.mutate_target") as mutate:
        calls.attach_mock(verify, "verify")
        calls.attach_mock(mutate, "mutate")
        authority.execute({"operation": "archive-tmux", "target": TARGET,
                           "agent": "codex", "transcript": "thread-1"})
    assert calls.mock_calls == [
        mock.call.verify("codex", "thread-1"),
        mock.call.mutate(TARGET, "archive", []),
    ]


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_authority_composite_archive_orders_each_authoritative_component(agent):
    calls = mock.Mock()
    with mock.patch("agent_fleet.authority.transcripts.verify") as verify, \
         mock.patch("agent_fleet.authority.tmux.mutate_target") as mutate, \
         mock.patch("agent_fleet.authority.alan.retire") as retire:
        calls.attach_mock(verify, "verify")
        calls.attach_mock(mutate, "mutate")
        calls.attach_mock(retire, "retire")
        authority.execute({"operation": "archive-composite",
                           "actor": f"{agent}-1@lovelace", "agent": agent,
                           "target": TARGET, "transcript": "thread-1"})
    assert calls.mock_calls == [
        mock.call.verify(agent, "thread-1"),
        mock.call.mutate(TARGET, "archive", []),
        mock.call.retire(f"{agent}-1@lovelace"),
    ]


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_pristine_archive_verifies_emptiness_then_closes_and_retires(agent):
    calls = mock.Mock()
    with mock.patch("agent_fleet.authority.alan.verify_pristine") as pristine, \
         mock.patch("agent_fleet.authority.tmux.mutate_target") as mutate, \
         mock.patch("agent_fleet.authority.alan.retire") as retire:
        calls.attach_mock(pristine, "pristine")
        calls.attach_mock(mutate, "mutate")
        calls.attach_mock(retire, "retire")
        authority.execute({"operation": "archive-pristine",
                           "actor": f"{agent}-1@lovelace", "agent": agent,
                           "target": TARGET})
    assert calls.mock_calls == [
        mock.call.pristine(f"{agent}-1@lovelace"),
        mock.call.mutate(TARGET, "archive", []),
        mock.call.retire(f"{agent}-1@lovelace"),
    ]


def test_pristine_archive_stops_when_the_actor_has_work():
    with mock.patch("agent_fleet.authority.alan.verify_pristine",
                    side_effect=RuntimeError("actor has conversational work")), \
         mock.patch("agent_fleet.authority.tmux.mutate_target") as mutate, \
         mock.patch("agent_fleet.authority.alan.retire") as retire, \
         pytest.raises(RuntimeError, match="conversational work"):
        authority.execute({"operation": "archive-pristine",
                           "actor": "codex-1@lovelace", "agent": "codex",
                           "target": TARGET})
    mutate.assert_not_called()
    retire.assert_not_called()


def test_composite_archive_stops_when_transcript_verification_fails():
    with mock.patch("agent_fleet.authority.transcripts.verify",
                    side_effect=LookupError("missing")), \
         mock.patch("agent_fleet.authority.tmux.mutate_target") as mutate, \
         mock.patch("agent_fleet.authority.alan.retire") as retire, \
         pytest.raises(LookupError, match="missing"):
        authority.execute({"operation": "archive-composite",
                           "actor": "codex-1@lovelace", "agent": "codex",
                           "target": TARGET, "transcript": "thread-1"})
    mutate.assert_not_called()
    retire.assert_not_called()


def test_composite_archive_stops_before_retire_when_tmux_archive_fails():
    with mock.patch("agent_fleet.authority.transcripts.verify"), \
         mock.patch("agent_fleet.authority.tmux.mutate_target",
                    side_effect=RuntimeError("tmux failed")), \
         mock.patch("agent_fleet.authority.alan.retire") as retire, \
         pytest.raises(RuntimeError, match="tmux failed"):
        authority.execute({"operation": "archive-composite",
                           "actor": "codex-1@lovelace", "agent": "codex",
                           "target": TARGET, "transcript": "thread-1"})
    retire.assert_not_called()


def test_composite_archive_removes_provider_before_retire_failure():
    with mock.patch("agent_fleet.authority.transcripts.verify"), \
         mock.patch("agent_fleet.authority.tmux.mutate_target") as mutate, \
         mock.patch("agent_fleet.authority.alan.retire",
                    side_effect=RuntimeError("retire failed")), \
         pytest.raises(RuntimeError, match="retire failed"):
        authority.execute({"operation": "archive-composite",
                           "actor": "codex-1@lovelace", "agent": "codex",
                           "target": TARGET, "transcript": "thread-1"})
    mutate.assert_called_once_with(TARGET, "archive", [])


@pytest.mark.parametrize("agent", ["claude", "codex", "grok"])
def test_authority_transcript_restore_returns_native_identity(agent):
    with mock.patch("agent_fleet.authority.transcripts.resume_native") as resume:
        value = authority.execute({"operation": "restore-transcript",
                                   "agent": agent, "transcript": "full-id",
                                   "name": "work"})
    assert value == {"agent": agent, "transcript": "full-id"}
    resume.assert_called_once_with(agent, "full-id")


def test_authority_antigravity_transcript_restore_remains_standalone():
    with mock.patch("agent_fleet.authority.transcripts.resume") as resume:
        value = authority.execute({"operation": "restore-transcript",
                                   "agent": "antigravity", "transcript": "full-id",
                                   "name": "work"})
    assert value == {"agent": "antigravity", "transcript": "full-id"}
    resume.assert_called_once_with("antigravity", "full-id", "work")


def test_authority_restores_the_exact_adopted_native_identity():
    with mock.patch("agent_fleet.authority.transcripts.resume_native") as resume:
        value = authority.execute({"operation": "restore-native",
                                   "actor": "codex-full-id@lovelace",
                                   "agent": "codex", "transcript": "full-id"})
    assert value == {"source": "alan:codex-full-id@lovelace"}
    resume.assert_called_once_with("codex", "full-id")


def test_authority_rejects_a_native_restore_for_another_actor():
    with mock.patch("agent_fleet.authority.transcripts.resume_native") as resume, \
         pytest.raises(ValueError, match="actor and transcript identity differ"):
        authority.execute({"operation": "restore-native",
                           "actor": "codex-other-id@lovelace",
                           "agent": "codex", "transcript": "full-id"})
    resume.assert_not_called()


def test_authority_rejects_generic_or_extra_operations():
    for request in ({"operation": "exec", "command": "sh"},
                    {"operation": "refresh", "actor": "codex-a@lovelace"},
                    {"operation": "archive-alan", "actor": "a", "fallback": True}):
        with mock.patch("agent_fleet.authority.alan.retire") as retire, \
             mock.patch("agent_fleet.authority.alan.resume") as resume:
            with pytest.raises(ValueError, match="invalid authority action"):
                authority.execute(request)
        retire.assert_not_called()
        resume.assert_not_called()
    with pytest.raises(ValueError, match="language actor"):
        authority.execute({"operation": "archive-alan", "actor": "python-a",
                           "agent": "python"})


def test_daemon_rename_revalidates_its_projection():
    item = session()
    fleet = Fleet()
    fleet.sessions["will@lovelace"] = [item]
    fleet.unavailable.clear()

    async def exercise():
        with mock.patch.object(fleet, "authority", return_value={"name": "new"}) as execute:
            assert await fleet.action({"operation": "rename", "source": item.ref.key,
                                       "name": "new."}) == {"name": "new"}
        execute.assert_awaited_once_with("will@lovelace", {
            "operation": "rename-alan", "actor": "codex-1@lovelace",
            "name": "new"})
    asyncio.run(exercise())


def test_removed_refresh_is_rejected_without_authority_or_viewer_effects():
    item = session()
    fleet = Fleet()
    fleet.sessions["will@lovelace"] = [item]
    fleet.unavailable.clear()

    async def exercise():
        with mock.patch.object(fleet, "authority") as execute, \
             mock.patch.object(fleet, "viewers") as viewers, \
             mock.patch.object(fleet, "update_viewers") as update:
            with pytest.raises(ValueError, match="invalid Fleet action"):
                await fleet.action({"operation": "refresh",
                                    "source": item.ref.key})
        execute.assert_not_awaited()
        viewers.assert_not_awaited()
        update.assert_not_awaited()

    asyncio.run(exercise())


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_daemon_composite_archive_payload_contains_both_exact_authorities(agent):
    actor = session(agent=agent)
    attachment = SessionRef(ServerRef(
        "will@lovelace", "/tmp/tmux/native", 44, 12), "$9")
    composite = Session(**{**actor.__dict__, "attachment": attachment})
    fleet = Fleet()
    fleet.sessions = {"lovelace": [composite]}
    fleet.unavailable.clear()

    assert fleet.archive_authority(composite.ref.key)[2] == {
        "operation": "archive-composite", "actor": f"{agent}-1@lovelace",
        "agent": agent, "target": ["/tmp/tmux/native", 44, 12, "$9"],
        "transcript": "thread-1"}


def test_composite_archive_keeps_bare_actor_and_raw_provider_operations():
    actor = session(agent="codex")
    provider = session(kind="tmux", agent="codex")
    fleet = Fleet()
    fleet.sessions = {"lovelace": [actor, provider]}
    fleet.unavailable.clear()

    assert fleet.archive_authority(actor.ref.key)[2] == {
        "operation": "archive-alan", "actor": "codex-1@lovelace",
        "agent": "codex"}
    assert fleet.archive_authority(provider.ref.key)[2] == {
        "operation": "archive-tmux",
        "target": ["/tmp/tmux/default", 12, 10, "$1"],
        "agent": "codex", "transcript": "thread-1"}


def test_retired_native_actor_leaves_a_provider_only_archive_boundary():
    actor = replace(session(agent="codex"), reported_state="retired",
                    name="historic name")
    provider = session(kind="tmux", agent="codex")
    [historic] = fold_adopted([actor, provider])
    fleet = Fleet()
    fleet.sessions = {"lovelace": [historic]}
    fleet.unavailable.clear()

    assert historic.ref == provider.ref
    assert historic.name == "historic name"
    assert fleet.archive_authority(historic.ref.key)[2] == {
        "operation": "archive-tmux",
        "target": ["/tmp/tmux/default", 12, 10, "$1"],
        "agent": "codex", "transcript": "thread-1"}


def test_composite_archive_projection_leaves_no_actor_or_raw_provider():
    actor = session(agent="codex")
    provider = session(kind="tmux", agent="codex")
    live = [provider, actor]
    [composite] = fold_adopted(live)
    fleet = Fleet()
    fleet.sessions = {"lovelace": [composite]}
    fleet.unavailable.clear()
    graph = daemon.nx.MultiDiGraph()
    graph.graph["actors"] = [{"addr": actor.ref.key, "kind": "codex",
                              "evaluator": "native"}]
    fleet._composed = (fleet.observed, graph)
    assert [item.session for item in fleet.projected()] == [composite]

    async def remove_components(_host, request):
        if request["operation"] in {"archive-tmux", "archive-composite"}:
            assert request["target"] == ["/tmp/tmux/default", 12, 10, "$1"]
            live.remove(provider)
        if request["operation"] in {"archive-alan", "archive-composite"}:
            assert request["actor"] == actor.ref.session_id
            live.remove(actor)
        fleet.sessions = {"lovelace": fold_adopted(live)}
        return {}

    with mock.patch.object(fleet, "viewers", return_value=[]), \
         mock.patch.object(fleet, "update_viewers"), \
         mock.patch.object(fleet, "authority", side_effect=remove_components):
        asyncio.run(fleet.action({"operation": "archive", "source": composite.ref.key}))

    assert fleet.projected() == []


def test_composite_retire_failure_leaves_the_bare_actor_visible():
    actor = session(agent="codex")
    provider = session(kind="tmux", agent="codex")
    live = [provider, actor]
    [composite] = fold_adopted(live)
    fleet = Fleet()
    fleet.sessions = {"lovelace": [composite]}
    fleet.unavailable.clear()
    graph = daemon.nx.MultiDiGraph()
    graph.graph["actors"] = [{"addr": actor.ref.key, "kind": "codex",
                              "evaluator": "native"}]
    fleet._composed = (fleet.observed, graph)

    async def fail_after_provider_removal(_host, request):
        if request["operation"] == "archive-composite":
            assert request["target"] == ["/tmp/tmux/default", 12, 10, "$1"]
            live.remove(provider)
        fleet.sessions = {"lovelace": fold_adopted(live)}
        raise RuntimeError("retire failed")

    with mock.patch.object(fleet, "viewers", return_value=[]), \
         mock.patch.object(fleet, "update_viewers"), \
         mock.patch.object(fleet, "authority", side_effect=fail_after_provider_removal), \
         pytest.raises(RuntimeError, match="retire failed"):
        asyncio.run(fleet.action({"operation": "archive", "source": composite.ref.key}))

    assert [item.session for item in fleet.projected()] == [actor]


def test_workstation_rename_sends_raw_input_and_returns_boundary_normalization():
    with mock.patch("agent_fleet.actions.fleet_action",
                    return_value={"name": "docs-v2-1"}) as action:
        assert actions.rename("source", "docs:v2.1") == "docs-v2-1"
    action.assert_called_once_with({"operation": "rename", "source": "source",
                                   "name": "docs:v2.1"})


def test_daemon_restores_transcript_then_reconciles_exact_native_identity():
    fleet = Fleet()
    fleet.unavailable.clear()
    restored = session(agent="codex", transcript="full-id")
    restored = replace(
        restored,
        ref=SessionRef(restored.ref.server, "codex-full-id@lovelace"),
        attachment=session(kind="tmux", agent="codex", transcript="full-id").ref,
    )
    standalone = session(kind="tmux", agent="codex", transcript="full-id")
    detached = replace(restored, attachment=None)
    unnamed = replace(restored, name="codex-full-id")

    async def exercise():
        renamed = asyncio.Event()

        async def execute(_host, request):
            if request["operation"] == "rename-alan":
                renamed.set()
            return {}

        async def project(value):
            async with fleet.changed:
                fleet.sessions = {"will@lovelace": [value]}
                fleet.observed += 1
                fleet.changed.notify_all()
            await asyncio.sleep(0)

        with mock.patch.object(fleet, "authority", side_effect=execute) as authority:
            pending = asyncio.create_task(fleet.action({
                "operation": "restore", "history": "will@lovelace:codex:full-id",
                "name": "work"}))
            await asyncio.sleep(0)
            await project(standalone)
            assert not pending.done()
            await project(detached)
            assert not pending.done()
            await project(unnamed)
            await renamed.wait()
            await asyncio.sleep(0)
            assert not pending.done()
            await project(restored)
            value = await pending

        source = "alan:will@lovelace:codex-full-id@lovelace"
        assert value == {"source": source}
        assert authority.await_args_list == [mock.call("will@lovelace", {
            "operation": "restore-transcript", "agent": "codex",
            "transcript": "full-id", "name": "work"}), mock.call("will@lovelace", {
                "operation": "rename-alan", "actor": "codex-full-id@lovelace",
                "name": "work"})]

    asyncio.run(exercise())


def test_daemon_native_actor_restore_waits_for_its_provider_attachment():
    fleet = Fleet()
    fleet.unavailable.clear()
    actor = session(agent="codex", transcript="1")
    provider = session(kind="tmux", agent="codex", transcript="1")
    graph = daemon.nx.MultiDiGraph()
    graph.graph["actors"] = [{"addr": actor.ref.key, "actor": actor.ref.session_id,
                               "runtime": "will", "kind": "codex",
                               "evaluator": "native"}]
    fleet._composed = (fleet.observed, graph)

    async def exercise():
        with mock.patch.object(fleet, "authority", return_value={"source": actor.ref.key}):
            pending = asyncio.create_task(fleet.action({
                "operation": "restore", "history": actor.ref.key, "name": ""}))
            await asyncio.sleep(0)
            fleet.sessions = {"will@lovelace": [actor]}
            fleet.observed += 1
            async with fleet.changed:
                fleet.changed.notify_all()
            await asyncio.sleep(0)
            assert not pending.done()
            fleet.sessions = {"will@lovelace": fold_adopted([actor, provider])}
            fleet.observed += 1
            async with fleet.changed:
                fleet.changed.notify_all()
            value = await pending
            assert value == {"source": actor.ref.key}
            assert fleet.source(value["source"]).attachment == provider.ref

    asyncio.run(exercise())


def test_daemon_bare_llm_restore_does_not_wait_for_provider_attachment():
    fleet = Fleet()
    fleet.unavailable.clear()
    actor = session(agent="llm", transcript="")
    graph = daemon.nx.MultiDiGraph()
    graph.graph["actors"] = [{"addr": actor.ref.key, "actor": actor.ref.session_id,
                               "runtime": "will", "kind": "llm"}]
    fleet._composed = (fleet.observed, graph)

    async def exercise():
        with mock.patch.object(fleet, "authority", return_value={"source": actor.ref.key}):
            pending = asyncio.create_task(fleet.action({
                "operation": "restore", "history": actor.ref.key, "name": ""}))
            await asyncio.sleep(0)
            fleet.sessions = {"will@lovelace": [actor]}
            fleet.observed += 1
            async with fleet.changed:
                fleet.changed.notify_all()
            assert await pending == {"source": actor.ref.key}

    asyncio.run(exercise())


def test_daemon_managed_codex_restore_does_not_wait_for_provider_attachment():
    fleet = Fleet()
    fleet.unavailable.clear()
    actor = session(agent="codex", transcript="1")
    graph = daemon.nx.MultiDiGraph()
    graph.graph["actors"] = [{"addr": actor.ref.key, "actor": actor.ref.session_id,
                               "runtime": "will", "kind": "codex", "capabilities": "read",
                               "evaluator": "native", "managed": True}]
    fleet._composed = (fleet.observed, graph)

    async def exercise():
        with mock.patch.object(fleet, "authority", return_value={"source": actor.ref.key}):
            pending = asyncio.create_task(fleet.action({
                "operation": "restore", "history": actor.ref.key, "name": ""}))
            await asyncio.sleep(0)
            fleet.sessions = {"will@lovelace": [actor]}
            fleet.observed += 1
            async with fleet.changed:
                fleet.changed.notify_all()
            assert await pending == {"source": actor.ref.key}

    asyncio.run(exercise())


def test_daemon_refuses_stale_disconnected_and_unrecoverable_sources():
    fleet = Fleet()
    with pytest.raises(LookupError, match="not in the current projection"):
        asyncio.run(fleet.action({"operation": "archive", "source": "gone"}))

    item = session(host="newton")
    fleet.sessions["will@newton"] = [item]
    fleet.unavailable = {"will@newton"}
    with pytest.raises(RuntimeError, match="disconnected"):
        asyncio.run(fleet.action({"operation": "rename", "source": item.ref.key,
                                  "name": "new"}))

    item = session(kind="tmux", transcript="")
    fleet.sessions = {"will@lovelace": [item]}
    fleet.unavailable.clear()
    with pytest.raises(ValueError, match="durable"):
        asyncio.run(fleet.action({"operation": "archive", "source": item.ref.key}))


def test_authority_uses_the_bound_source_pipe():
    async def exercise():
        fleet = Fleet()
        source = "will@lovelace"
        fleet.unavailable.clear()
        request = {"operation": "rename-alan", "actor": "codex-1@lovelace",
                   "name": "new"}
        input = mock.Mock()
        input.drain = mock.AsyncMock()
        fleet.processes[source] = mock.Mock(stdin=input)
        pending = asyncio.create_task(fleet.authority(source, request))
        await asyncio.sleep(0)
        envelope = json.loads(input.write.call_args.args[0])
        assert envelope["request"] == request
        fleet.source_reply(source, {"authority": envelope["authority"],
                                    "value": {"name": "new"}})
        assert await pending == {"name": "new"}

    asyncio.run(exercise())


def test_archive_clears_every_shown_viewer():
    async def exercise():
        fleet = Fleet()
        item = session(kind="tmux")
        fleet.sessions = {"lovelace": [item]}
        fleet.unavailable.clear()
        paths = [Path("/run/viewer-main.sock"), Path("/run/viewer-right.sock")]
        with mock.patch.object(fleet, "viewers",
                               return_value=paths) as showing, \
             mock.patch.object(fleet, "authority", return_value={}), \
             mock.patch.object(fleet, "wait_for_absence"), \
             mock.patch.object(fleet, "update_viewers") as update:
            await fleet.action({"operation": "archive", "source": item.ref.key})
        showing.assert_awaited_once_with()
        update.assert_awaited_once_with(paths, f"CLEAR {item.ref.key}")

    asyncio.run(exercise())


def test_background_archive_releases_viewers_before_source_authority(tmp_path,
                                                                      monkeypatch):
    async def exercise():
        fleet = Fleet()
        key = "lovelace:/tmp/tmux/default:12:10:$1"
        paths = [Path("/run/viewer-main.sock")]
        order = []

        async def update(viewers, message):
            order.append(("viewers", viewers, message))

        async def authority(host, request):
            order.append(("authority", host, request))
            return {}

        with mock.patch.object(fleet, "viewers", return_value=paths), \
             mock.patch.object(fleet, "update_viewers", side_effect=update), \
             mock.patch.object(fleet, "authority", side_effect=authority), \
             mock.patch.object(fleet, "wait_for_absence"):
            await fleet.complete_archive(
                key, "will@lovelace", {"operation": "archive"}, [])

        assert order == [
            ("viewers", paths, f"CLEAR {key}"),
            ("authority", "will@lovelace", {"operation": "archive"}),
        ]

    monkeypatch.setattr(daemon, "RUNTIME", tmp_path)
    asyncio.run(exercise())


def test_background_archive_refuses_reattachment_after_viewer_release(tmp_path,
                                                                      monkeypatch):
    async def exercise():
        fleet = Fleet()
        item = session(kind="tmux")
        host = item.ref.server.host
        key = item.ref.key
        fleet.sessions = {host: [item]}
        fleet.unavailable.clear()
        fleet.pending_archives.add(key)
        released = asyncio.Event()
        finish = asyncio.Event()

        async def update(_viewers, _message):
            released.set()

        async def authority(_host, _request):
            await finish.wait()
            return {}

        with mock.patch.object(fleet, "viewers", return_value=[]), \
             mock.patch.object(fleet, "update_viewers", side_effect=update), \
             mock.patch.object(fleet, "authority", side_effect=authority), \
             mock.patch.object(fleet, "wait_for_absence"):
            archive = asyncio.create_task(fleet.complete_archive(
                key, host, {"operation": "archive"}, []))
            await released.wait()
            with pytest.raises(LookupError, match="being archived"):
                await fleet.switch(key, "/dev/pts/9")
            finish.set()
            await archive

    monkeypatch.setattr(daemon, "RUNTIME", tmp_path)
    asyncio.run(exercise())


def test_direct_archive_refuses_reattachment_until_authority_finishes():
    async def exercise():
        fleet = Fleet()
        item = session(kind="tmux")
        host = item.ref.server.host
        key = item.ref.key
        fleet.sessions = {host: [item]}
        fleet.unavailable.clear()
        started = asyncio.Event()
        finish = asyncio.Event()

        async def authority(_host, _request):
            started.set()
            await finish.wait()
            return {}

        with mock.patch.object(fleet, "viewers", return_value=[]), \
             mock.patch.object(fleet, "update_viewers"), \
             mock.patch.object(fleet, "authority", side_effect=authority), \
             mock.patch.object(fleet, "wait_for_absence"):
            archive = asyncio.create_task(fleet.action({
                "operation": "archive", "source": key}))
            await started.wait()
            assert key in fleet.pending_archives
            with pytest.raises(LookupError, match="being archived"):
                await fleet.switch(key, "/dev/pts/9")
            with pytest.raises(LookupError, match="being archived"):
                fleet.source(key)
            finish.set()
            assert await archive == {}
            assert key not in fleet.pending_archives

    asyncio.run(exercise())


def test_viewer_updates_attempt_every_recorded_slot_before_reporting_failure():
    async def exercise():
        fleet = Fleet()
        paths = [Path("/run/viewer-left.sock"), Path("/run/viewer-right.sock")]
        with mock.patch.object(fleet, "update_viewer",
                               side_effect=[RuntimeError("gone"), None]) as update:
            with pytest.raises(RuntimeError, match="viewer-left.sock: gone"):
                await fleet.update_viewers(paths, "CLEAR")
        assert update.await_args_list == [mock.call(paths[0], "CLEAR"),
                                          mock.call(paths[1], "CLEAR")]

    asyncio.run(exercise())


def test_optimistic_archive_refuses_to_destroy_source_before_viewer_cleanup(
        tmp_path, monkeypatch):
    async def exercise():
        fleet = Fleet()
        item = session(kind="tmux")
        fleet.sessions = {"lovelace": [item]}
        fleet.unavailable.clear()
        fleet.muster_generation = ("registered",)
        paths = [Path("/run/viewer-left.sock"), Path("/run/viewer-right.sock")]

        async def absent(_key):
            fleet.sessions = {}

        with mock.patch.object(fleet, "viewers",
                               return_value=paths) as showing, \
             mock.patch.object(fleet, "authority", return_value={}) as authority, \
             mock.patch.object(fleet, "wait_for_absence", side_effect=absent), \
             mock.patch.object(fleet, "update_viewers",
                               side_effect=RuntimeError("viewer-left.sock: gone")) as update, \
             mock.patch.object(fleet, "publish_current_view") as publish:
            result = await fleet.mutate_action(
                f"archive\t{item.ref.key}\t0\t100")
            showing.assert_not_awaited()
            assert item.ref.key in fleet.pending_archives
            assert "Archiving" in next(tmp_path.glob("*.header")).read_text()
            await asyncio.sleep(0)
            publish.assert_not_awaited()
            for artifact in tmp_path.glob("muster-view-*.*"):
                artifact.unlink()
            await asyncio.gather(*fleet.background_tasks)

        assert "reload-sync" in result
        assert item.ref.key not in fleet.pending_archives
        assert [value.session.ref.key for value in fleet.projected()] == [item.ref.key]
        authority.assert_not_awaited()
        update.assert_awaited_once_with(paths, f"CLEAR {item.ref.key}")
        publish.assert_awaited_once()
        assert "viewer-left.sock: gone" in publish.await_args.args[0]

    monkeypatch.setattr(daemon, "RUNTIME", tmp_path)
    asyncio.run(exercise())


def test_optimistic_archive_clears_viewers_before_authority_failure(
        tmp_path, monkeypatch):
    async def exercise():
        fleet = Fleet()
        item = session(kind="tmux")
        fleet.sessions = {"lovelace": [item]}
        fleet.unavailable.clear()
        fleet.muster_generation = ("registered",)
        with mock.patch.object(fleet, "viewers", return_value=[]), \
             mock.patch.object(fleet, "authority", side_effect=RuntimeError("refused")), \
             mock.patch.object(fleet, "update_viewers") as update, \
             mock.patch.object(fleet, "publish_current_view") as publish:
            await fleet.mutate_action(f"archive\t{item.ref.key}\t0\t100")
            await asyncio.sleep(0)
            publish.assert_not_awaited()
            for artifact in tmp_path.glob("muster-view-*.*"):
                artifact.unlink()
            await asyncio.gather(*fleet.background_tasks)
        assert [value.session.ref.key for value in fleet.projected()] == [item.ref.key]
        update.assert_awaited_once_with([], f"CLEAR {item.ref.key}")
        publish.assert_awaited_once_with("refused")

    monkeypatch.setattr(daemon, "RUNTIME", tmp_path)
    asyncio.run(exercise())


def test_authority_command_error_is_preserved():
    async def exercise():
        fleet = Fleet()
        host = "lovelace"
        source = "will@lovelace"
        fleet.unavailable.clear()
        input = mock.Mock()
        input.drain = mock.AsyncMock()
        fleet.processes[source] = mock.Mock(stdin=input)
        pending = asyncio.create_task(fleet.authority(source, {
            "operation": "archive-alan", "actor": f"codex-1@{host}",
            "agent": "codex"}))
        await asyncio.sleep(0)
        envelope = json.loads(input.write.call_args.args[0])
        fleet.source_reply(source, {"authority": envelope["authority"],
                                    "error": "refused"})
        with pytest.raises(RuntimeError, match="refused"):
            await pending

    asyncio.run(exercise())


def test_authority_json_boundary_round_trips_one_value():
    request = {"operation": "rename-alan", "actor": "codex-1@lovelace",
               "name": "new"}
    with mock.patch("agent_fleet.authority.alan.rename"):
        assert json.loads(authority.execute_json(json.dumps(request))) == {"name": "new"}


def test_finite_host_command_cannot_inherit_an_actor_socket(monkeypatch):
    async def exercise():
        fleet = Fleet()
        process = mock.Mock(returncode=0)
        process.communicate = mock.AsyncMock(return_value=(b"{}\n", b""))
        monkeypatch.setenv("LOOP_SOCKET", "/actor/private.sock")
        monkeypatch.setenv("LOOP_CAPABILITIES", '"full"')
        with mock.patch("agent_fleet.daemon.asyncio.create_subprocess_exec",
                        return_value=process) as execute:
            assert await fleet.remote_json("will@lovelace",
                                           "/usr/bin/python", "-c", "pass") == {}
        assert execute.await_args.args[:5] == (
            "ssh", "-T", "-o", "BatchMode=yes", "will@lovelace")
        assert "LOOP_SOCKET=/home/will/.local/state/alan/loop.sock" in \
            execute.await_args.args[5]
        environment = execute.await_args.kwargs["env"]
        assert "LOOP_SOCKET" not in environment
        assert "LOOP_CAPABILITIES" not in environment

    asyncio.run(exercise())


def test_remote_command_uses_the_exact_source_login_and_socket():
    async def exercise():
        fleet = Fleet()
        source = config.RuntimeSource("newton", "will",
                                      "/home/will/.local/state/alan/loop.sock",
                                      "/tmp/tmux-1000/default")
        fleet.sources = {source.key: source}
        process = mock.Mock(returncode=0)
        process.communicate = mock.AsyncMock(return_value=(b"{}\n", b""))
        envelope = ('{"operation":"archive-alan","actor":"codex-1@newton",'
                    '"agent":"codex"}')
        with mock.patch("agent_fleet.daemon.asyncio.create_subprocess_exec",
                        return_value=process) as execute:
            assert await fleet.remote_json("will@newton", "/usr/bin/python", "-c",
                                           "print('ok')", envelope) == {}
        remote = shlex.split(execute.await_args.args[-1])
        assert execute.await_args.args[4] == "will@newton"
        assert remote[:3] == ["/usr/bin/env",
                              "LOOP_SOCKET=/home/will/.local/state/alan/loop.sock",
                              "FLEET_TMUX_SOCKET=/tmp/tmux-1000/default"]
        assert remote[-1] == envelope

    asyncio.run(exercise())


def test_source_command_uses_each_configured_public_socket(
        tmp_path, monkeypatch):
    state = tmp_path / "state"
    public = state / "alan" / "loop.sock"
    private = tmp_path / "private.sock"
    public.parent.mkdir(parents=True)
    requests = {"public": [], "private": []}
    stopped = threading.Event()

    def serve(path, name):
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen()
            server.settimeout(.05)
            while not stopped.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    line = connection.makefile("rb").readline()
                    requests[name].append(json.loads(line))
                    connection.sendall(b'{"ok":true}\n')

    servers = [threading.Thread(target=serve, args=(public, "public"), daemon=True),
               threading.Thread(target=serve, args=(private, "private"), daemon=True)]
    for server in servers:
        server.start()
    for _ in range(100):
        if public.exists() and private.exists():
            break
        time.sleep(.01)
    assert public.exists() and private.exists()

    ssh = tmp_path / "ssh"
    ssh.write_text('#!/bin/sh\nexec /bin/sh -c "$5"\n')
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("LOOP_SOCKET", str(private))
    monkeypatch.setenv("LOOP_CAPABILITIES", '"full"')
    command = (sys.executable, "-c",
               "import loop; loop.control('codex-1@target', 'retire'); print('{}')")

    async def exercise():
        fleet = Fleet()
        local = config.RuntimeSource("lovelace", "will", str(public), "/tmp/tmux")
        remote = config.RuntimeSource("newton", "will", str(public), "/tmp/tmux")
        fleet.sources = {source.key: source for source in (local, remote)}
        assert await fleet.remote_json(local.key, *command) == {}
        assert await fleet.remote_json(remote.key, *command) == {}

    try:
        asyncio.run(exercise())
    finally:
        stopped.set()
        for server in servers:
            server.join(1)
    assert requests == {
        "public": [
            {"op": "control", "actor": "codex-1@target", "operation": "retire"},
            {"op": "control", "actor": "codex-1@target", "operation": "retire"},
        ],
        "private": [],
    }


def test_authority_shares_the_source_control_lane():
    async def exercise():
        fleet = Fleet()
        item = session(kind="tmux")
        host = item.ref.server.host
        source = item.ref.server.source
        fleet.sessions = {source: [item]}
        fleet.unavailable.clear()
        writes = []

        class Input:
            def write(self, value):
                writes.append(json.loads(value))

            async def drain(self):
                pass

        fleet.processes = {source: mock.Mock(stdin=Input())}
        authority_task = asyncio.create_task(fleet.authority(source, {
            "operation": "archive-tmux",
            "target": ["/tmp/tmux/default", 12, 10, "$1"],
            "agent": "codex", "transcript": "thread-1"}))
        switch_task = asyncio.create_task(fleet.switch(item.ref.key, "/dev/pts/9"))
        preview_task = asyncio.create_task(fleet.preview(item.ref.key, 80, 20))
        await asyncio.sleep(0)
        assert {next(iter(request)) for request in writes} == {
            "authority", "switch", "preview"}
        authority_request = next(request for request in writes if "authority" in request)
        switch = next(request for request in writes if "switch" in request)
        preview = next(request for request in writes if "preview" in request)
        target = daemon.split_key(item.ref.key)[1:]
        fleet.source_reply(source, {"switch": switch["switch"],
                                  "target": target, "duration": .001})
        fleet.source_reply(source, {"preview": preview["preview"], "text": "screen"})
        fleet.source_reply(source, {"authority": authority_request["authority"],
                                    "value": {}})
        assert await asyncio.wait_for(switch_task, 1) == (target, .001, item.name, host)
        assert await asyncio.wait_for(preview_task, 1) == "screen"
        assert await authority_task == {}

    asyncio.run(exercise())


def test_action_client_cannot_transmit_inherited_actor_identity(tmp_path, monkeypatch):
    runtime = tmp_path / "agent-fleet"
    runtime.mkdir()
    path = runtime / "fleet.sock"
    received = []
    ready = threading.Event()

    def serve():
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen()
            ready.set()
            connection, _ = server.accept()
            with connection:
                received.append(json.loads(connection.makefile().readline()))
                connection.sendall(b'{"ok":true,"value":{"source":"alan:codex-1@lovelace"}}\n')

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(1)
    monkeypatch.setattr(daemon, "RUNTIME", runtime)
    monkeypatch.setenv("LOOP_SOCKET", "/actor/private.sock")
    monkeypatch.setenv("LOOP_CAPABILITIES", '"full"')
    envelope = {"operation": "create", "source": "will@lovelace", "agent": "codex",
                "name": "work", "cwd": "/work"}
    assert daemon.action(envelope) == {"source": "alan:codex-1@lovelace"}
    thread.join()
    assert received == [envelope]


def test_service_removes_actor_identity_at_the_process_boundary():
    source = Path(__file__).parents[1] / "fleet.service"
    assert "UnsetEnvironment=LOOP_SOCKET LOOP_CAPABILITIES" in source.read_text()
