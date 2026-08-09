import asyncio
import json
import os
import shlex
import socket
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from agent_fleet import authority
from agent_fleet import actions
from agent_fleet import daemon
from agent_fleet.daemon import Fleet
from agent_fleet.model import ServerRef, Session, SessionRef


def session(host="lovelace", kind="alan", agent="codex", transcript="thread-1"):
    server = (ServerRef(host, "", 0, 0, "alan") if kind == "alan" else
              ServerRef(host, "/tmp/tmux/default", 12, 10))
    identity = f"{agent}-1@{host}" if kind == "alan" else "$1"
    return Session(SessionRef(server, identity), "work", 1, 2, 0, 1,
                   "tmux", "", "/work", agent, "waiting", "", 0,
                   transcript)


def test_authority_create_returns_the_exact_actor_address():
    with mock.patch("agent_fleet.authority.alan.create",
                    return_value="codex-full@lovelace") as create:
        value = authority.execute({"operation": "create", "agent": "codex",
                                   "name": "work", "cwd": "/work"})
    assert value == {"source": "alan:codex-full@lovelace"}
    create.assert_called_once_with("codex", "work", "/work")


def test_authority_operations_are_finite_and_direct():
    with mock.patch("agent_fleet.authority.alan.rename") as rename:
        assert authority.execute({"operation": "rename-alan", "actor": "codex-1@lovelace",
                                  "name": "new"}) == {"name": "new"}
    rename.assert_called_once_with("codex-1@lovelace", "new")

    with mock.patch("agent_fleet.authority.tmux.mutate") as mutate:
        authority.execute({"operation": "rename-tmux", "source": "source",
                           "name": "new"})
    mutate.assert_called_once_with("source", "rename", ["new"])

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

    with mock.patch("agent_fleet.authority.presentation.refresh") as refresh:
        authority.execute({"operation": "refresh", "actor": "codex-1@lovelace"})
    refresh.assert_called_once_with("codex-1@lovelace")

    with mock.patch("agent_fleet.authority.alan.resume",
                    return_value="codex-1@lovelace") as resume:
        assert authority.execute({"operation": "restore-alan",
                                  "actor": "codex-1@lovelace"}) == {
                                      "source": "alan:codex-1@lovelace"}
    resume.assert_called_once_with("codex-1@lovelace")


def test_authority_archive_verifies_recovery_before_exact_tmux_kill():
    calls = mock.Mock()
    with mock.patch("agent_fleet.authority.transcripts.verify") as verify, \
         mock.patch("agent_fleet.authority.tmux.mutate") as mutate:
        calls.attach_mock(verify, "verify")
        calls.attach_mock(mutate, "mutate")
        authority.execute({"operation": "archive-tmux", "source": "source",
                           "agent": "codex", "transcript": "thread-1"})
    assert calls.mock_calls == [
        mock.call.verify("codex", "thread-1"),
        mock.call.mutate("source", "archive", []),
    ]


def test_authority_transcript_restore_returns_native_identity():
    with mock.patch("agent_fleet.authority.transcripts.resume") as resume:
        value = authority.execute({"operation": "restore-transcript",
                                   "agent": "claude", "transcript": "full-id",
                                   "name": "work"})
    assert value == {"agent": "claude", "transcript": "full-id"}
    resume.assert_called_once_with("claude", "full-id", "work")


def test_authority_rejects_generic_or_extra_operations():
    for request in ({"operation": "exec", "command": "sh"},
                    {"operation": "archive-alan", "actor": "a", "fallback": True}):
        with pytest.raises(ValueError, match="invalid authority action"):
            authority.execute(request)
    with pytest.raises(ValueError, match="language actor"):
        authority.execute({"operation": "archive-alan", "actor": "python-a",
                           "agent": "python"})


def test_daemon_rename_and_refresh_revalidate_its_projection():
    item = session()
    fleet = Fleet()
    fleet.sessions["lovelace"] = [item]
    fleet.unavailable.clear()

    async def exercise():
        with mock.patch.object(fleet, "authority", return_value={"name": "new"}) as execute:
            assert await fleet.action({"operation": "rename", "source": item.ref.key,
                                       "name": "new."}) == {"name": "new"}
        execute.assert_awaited_once_with("lovelace", {
            "operation": "rename-alan", "actor": "codex-1@lovelace",
            "name": "new"})
        with mock.patch.object(fleet, "authority",
                               return_value={"source": item.ref.key}) as execute:
            assert await fleet.action({"operation": "refresh",
                                       "source": item.ref.key}) == {
                                           "source": item.ref.key}
        execute.assert_awaited_once_with("lovelace", {
            "operation": "refresh", "actor": "codex-1@lovelace"})

    asyncio.run(exercise())


def test_workstation_rename_sends_raw_input_and_returns_boundary_normalization():
    with mock.patch("agent_fleet.actions.fleet_action",
                    return_value={"name": "docs-v2-1"}) as action:
        assert actions.rename("source", "docs:v2.1") == "docs-v2-1"
    action.assert_called_once_with({"operation": "rename", "source": "source",
                                   "name": "docs:v2.1"})


def test_daemon_restores_transcript_then_reconciles_exact_native_identity():
    fleet = Fleet()
    fleet.unavailable.clear()
    restored = session(kind="tmux", agent="codex", transcript="full-id")

    async def exercise():
        with mock.patch.object(fleet, "authority", return_value={
                "agent": "codex", "transcript": "full-id"}) as execute, \
             mock.patch.object(fleet, "wait_for_source",
                               return_value=restored.ref.key) as wait:
            value = await fleet.action({"operation": "restore",
                                        "history": "lovelace:codex:full-id",
                                        "name": "work"})
        assert value == {"source": restored.ref.key}
        execute.assert_awaited_once_with("lovelace", {
            "operation": "restore-transcript", "agent": "codex",
            "transcript": "full-id", "name": "work"})
        wait.assert_awaited_once()

    asyncio.run(exercise())


def test_daemon_refuses_stale_disconnected_and_unrecoverable_sources():
    fleet = Fleet()
    with pytest.raises(LookupError, match="session disappeared"):
        asyncio.run(fleet.action({"operation": "archive", "source": "gone"}))

    item = session(host="newton")
    fleet.sessions["newton"] = [item]
    fleet.unavailable = {"newton"}
    with pytest.raises(RuntimeError, match="disconnected"):
        asyncio.run(fleet.action({"operation": "rename", "source": item.ref.key,
                                  "name": "new"}))

    item = session(kind="tmux", transcript="")
    fleet.sessions = {"lovelace": [item]}
    fleet.unavailable.clear()
    with pytest.raises(ValueError, match="durable"):
        asyncio.run(fleet.action({"operation": "archive", "source": item.ref.key}))


def test_authority_uses_one_finite_command_on_the_target_host():
    async def exercise():
        fleet = Fleet()
        host = os.uname().nodename.split(".", 1)[0]
        fleet.unavailable.clear()
        request = {"operation": "rename-alan", "actor": "codex-1@lovelace",
                   "name": "new"}
        with mock.patch.object(fleet, "remote_json",
                               return_value={"name": "new"}) as execute:
            value = await fleet.authority(host, request)
        assert value == {"name": "new"}
        command = execute.await_args.args
        assert command[0] == host
        assert "execute_json" in command[3]
        assert json.loads(command[4]) == request

    asyncio.run(exercise())


def test_archive_clears_and_refresh_reopens_every_shown_viewer():
    async def exercise(operation):
        fleet = Fleet()
        item = session(kind="tmux" if operation == "archive" else "alan")
        fleet.sessions = {"lovelace": [item]}
        fleet.unavailable.clear()
        paths = [Path("/run/viewer-main.sock"), Path("/run/viewer-right.sock")]
        with mock.patch.object(fleet, "viewers_showing",
                               return_value=paths) as showing, \
             mock.patch.object(fleet, "authority", return_value={}), \
             mock.patch.object(fleet, "wait_for_absence"), \
             mock.patch.object(fleet, "update_viewers") as update:
            await fleet.action({"operation": operation, "source": item.ref.key})
        showing.assert_awaited_once_with(item.ref.key)
        message = (f"CLEAR {item.ref.key}" if operation == "archive" else
                   f"OPEN {item.ref.key}")
        update.assert_awaited_once_with(paths, message)

    asyncio.run(exercise("archive"))
    asyncio.run(exercise("refresh"))


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


def test_optimistic_archive_commits_absence_despite_viewer_cleanup_failure(
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

        with mock.patch.object(fleet, "viewers_showing",
                               return_value=paths) as showing, \
             mock.patch.object(fleet, "authority", return_value={}), \
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
        assert fleet.projected() == []
        update.assert_awaited_once_with(paths, f"CLEAR {item.ref.key}")
        publish.assert_awaited_once()
        assert "viewer cleanup failed" in publish.await_args.args[0]

    monkeypatch.setattr(daemon, "RUNTIME", tmp_path)
    asyncio.run(exercise())


def test_optimistic_archive_failure_restores_row_and_never_clears_viewers(
        tmp_path, monkeypatch):
    async def exercise():
        fleet = Fleet()
        item = session(kind="tmux")
        fleet.sessions = {"lovelace": [item]}
        fleet.unavailable.clear()
        fleet.muster_generation = ("registered",)
        with mock.patch.object(fleet, "viewers_showing", return_value=[]), \
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
        update.assert_not_awaited()
        publish.assert_awaited_once_with("refused")

    monkeypatch.setattr(daemon, "RUNTIME", tmp_path)
    asyncio.run(exercise())


def test_authority_command_error_is_preserved():
    async def exercise():
        fleet = Fleet()
        host = os.uname().nodename.split(".", 1)[0]
        fleet.unavailable.clear()
        with mock.patch.object(fleet, "remote_json",
                               side_effect=RuntimeError("refused")):
            with pytest.raises(RuntimeError, match="refused"):
                await fleet.authority(host, {
                    "operation": "archive-alan", "actor": f"codex-1@{host}",
                    "agent": "codex"})

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
            assert await fleet.remote_json(os.uname().nodename.split(".", 1)[0],
                                           "/usr/bin/python", "-c", "pass") == {}
        assert execute.await_args.args[:5] == (
            "/usr/bin/env", "-u", "LOOP_SOCKET", "-u", "LOOP_CAPABILITIES")
        environment = execute.await_args.kwargs["env"]
        assert "LOOP_SOCKET" not in environment
        assert "LOOP_CAPABILITIES" not in environment

    asyncio.run(exercise())


def test_remote_authority_strips_actor_socket_on_the_target():
    async def exercise():
        fleet = Fleet()
        process = mock.Mock(returncode=0)
        process.communicate = mock.AsyncMock(return_value=(b"{}\n", b""))
        envelope = ('{"operation":"archive-alan","actor":"codex-1@newton",'
                    '"agent":"codex"}')
        with mock.patch("agent_fleet.daemon.asyncio.create_subprocess_exec",
                        return_value=process) as execute:
            assert await fleet.remote_json("newton", "/usr/bin/python", "-c",
                                           "print('ok')", envelope) == {}
        remote = shlex.split(execute.await_args.args[-1])
        assert remote[:5] == [
            "/usr/bin/env", "-u", "LOOP_SOCKET", "-u", "LOOP_CAPABILITIES"]
        assert remote[-1] == envelope

    asyncio.run(exercise())


def test_local_and_remote_authority_use_the_target_default_alan_socket(
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
        local = os.uname().nodename.split(".", 1)[0]
        assert await fleet.remote_json(local, *command) == {}
        assert await fleet.remote_json("remote-fixture", *command) == {}

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


def test_blocked_authority_does_not_enter_or_delay_the_host_control_lane():
    async def exercise():
        fleet = Fleet()
        item = session(kind="tmux")
        host = item.ref.server.host
        fleet.sessions = {host: [item]}
        fleet.unavailable.clear()
        writes = []

        class Input:
            def write(self, value):
                writes.append(json.loads(value))

            async def drain(self):
                pass

        fleet.processes = {host: mock.Mock(stdin=Input())}
        authority_started = asyncio.Event()
        release_authority = asyncio.Event()

        async def blocked(*_args):
            authority_started.set()
            await release_authority.wait()
            return {}

        with mock.patch.object(fleet, "remote_json", side_effect=blocked):
            authority_task = asyncio.create_task(fleet.authority(host, {
                "operation": "archive-tmux", "source": item.ref.key,
                "agent": "codex", "transcript": "thread-1"}))
            await authority_started.wait()
            switch_task = asyncio.create_task(
                fleet.switch(item.ref.key, "/dev/pts/9"))
            preview_task = asyncio.create_task(fleet.preview(item.ref.key, 80, 20))
            await asyncio.sleep(0)
            assert {next(iter(request)) for request in writes} == {"switch", "preview"}
            switch = next(request for request in writes if "switch" in request)
            preview = next(request for request in writes if "preview" in request)
            target = daemon.split_key(item.ref.key)[1:]
            fleet.host_reply({"switch": switch["switch"],
                              "target": target,
                              "duration": .001})
            fleet.host_reply({"preview": preview["preview"], "text": "screen"})
            assert await asyncio.wait_for(switch_task, 1) == (target, .001)
            assert await asyncio.wait_for(preview_task, 1) == "screen"
            assert not authority_task.done()
            release_authority.set()
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
    envelope = {"operation": "create", "host": "lovelace", "agent": "codex",
                "name": "work", "cwd": "/work"}
    assert daemon.action(envelope) == {"source": "alan:codex-1@lovelace"}
    thread.join()
    assert received == [envelope]


def test_service_removes_actor_identity_at_the_process_boundary():
    source = Path(__file__).parents[1] / "fleet.service"
    assert "UnsetEnvironment=LOOP_SOCKET LOOP_CAPABILITIES" in source.read_text()
