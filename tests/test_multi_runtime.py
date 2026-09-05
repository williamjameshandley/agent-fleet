import asyncio
import json
import os
import queue
import shlex
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from agent_fleet import alan, config, presentation, viewer
from agent_fleet.daemon import Fleet
from agent_fleet.model import ServerRef, Session, SessionRef
from agent_fleet.tmux import ControlClient


def runtime(principal, root):
    return config.RuntimeSource(
        "lovelace", principal, str(root / "loop.sock"), str(root / "tmux.sock"))


def actor_session(principal):
    actor = "claude-collision@lovelace"
    return Session(
        SessionRef(ServerRef(f"{principal}@lovelace", "", 0, 0, "alan"), actor),
        principal, 1, 0, 0, 1, "alan", "", f"/home/{principal}", "claude",
    )


def actor_catalogue(principal):
    human = f"{principal}@lovelace"
    actor = "claude-collision@lovelace"
    return [
        {"addr": human, "kind": "principal", "state": "waiting"},
        {"addr": actor, "kind": "claude", "state": "waiting"},
    ]


def test_runtime_source_configuration_is_closed_absolute_and_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG", tmp_path)
    entries = [
        {"host": "lovelace", "principal": principal,
         "public_socket": f"/{principal}/loop.sock",
         "tmux_socket": f"/{principal}/tmux.sock"}
        for principal in ("will", "sophie")
    ]
    (tmp_path / "runtime-sources.json").write_text(json.dumps(entries))

    assert [source.key for source in config.runtime_sources()] == [
        "will@lovelace", "sophie@lovelace"]

    entries[1]["principal"] = "will"
    (tmp_path / "runtime-sources.json").write_text(json.dumps(entries))
    with pytest.raises(ValueError, match="duplicate runtime source"):
        config.runtime_sources()


def test_source_tmux_command_selects_the_exact_configured_socket(monkeypatch):
    monkeypatch.setenv("FLEET_TMUX_SOCKET", "/run/user/1004/tmux.sock")

    assert config.tmux_command("list-sessions") == [
        "/usr/bin/tmux", "-N", "-S", "/run/user/1004/tmux.sock", "list-sessions"]


def test_colliding_same_host_actor_addresses_remain_runtime_qualified(tmp_path, monkeypatch):
    sources = {principal: runtime(principal, tmp_path / principal)
               for principal in ("will", "sophie")}
    monkeypatch.setattr("agent_fleet.daemon.runtime_sources", lambda: list(sources.values()))
    fleet = Fleet()
    fleet.sessions = {source.key: [actor_session(principal)]
                      for principal, source in sources.items()}
    fleet.catalogues = {source.key: actor_catalogue(principal)
                    for principal, source in sources.items()}
    fleet.unavailable.clear()
    fleet.observed += 1

    assert set(fleet.composed_catalogue()) == {
        f"alan:{source.key}:{actor['addr']}"
        for source in sources.values()
        for actor in fleet.catalogues[source.key]
    }


def test_preview_and_authority_route_only_to_selected_runtime(tmp_path, monkeypatch):
    sources = {principal: runtime(principal, tmp_path / principal)
               for principal in ("will", "sophie")}
    monkeypatch.setattr("agent_fleet.daemon.runtime_sources", lambda: list(sources.values()))
    fleet = Fleet()
    sessions = {principal: actor_session(principal) for principal in sources}
    fleet.sessions = {sources[p].key: [session] for p, session in sessions.items()}
    fleet.unavailable.clear()
    inputs = {source.key: mock.Mock() for source in sources.values()}
    for value in inputs.values():
        value.drain = mock.AsyncMock()
    fleet.processes = {key: mock.Mock(stdin=value) for key, value in inputs.items()}

    async def exercise():
        pending = asyncio.create_task(fleet.preview(sessions["sophie"].ref.key, 80, 20))
        await asyncio.sleep(0)
        request = json.loads(inputs["sophie@lovelace"].write.call_args.args[0])
        assert not inputs["will@lovelace"].write.called
        fleet.source_reply(
            "sophie@lovelace", {"preview": request["preview"], "text": "sophie"})
        assert await pending == "sophie"
        pending = asyncio.create_task(
            fleet.authority("sophie@lovelace", {"operation": "archive-alan"}))
        await asyncio.sleep(0)
        request = json.loads(inputs["sophie@lovelace"].write.call_args.args[0])
        assert not inputs["will@lovelace"].write.called
        fleet.source_reply("sophie@lovelace", {
            "authority": request["authority"], "value": {}})
        assert await pending == {}

    asyncio.run(exercise())


def test_one_runtime_disconnect_does_not_hide_same_host_sibling(tmp_path, monkeypatch):
    sources = [runtime(principal, tmp_path / principal) for principal in ("will", "sophie")]
    monkeypatch.setattr("agent_fleet.daemon.runtime_sources", lambda: sources)
    fleet = Fleet()
    fleet.sessions = {source.key: [actor_session(source.principal)] for source in sources}
    fleet.tmux_sessions = dict(fleet.sessions)
    fleet.catalogues = {source.key: [] for source in sources}
    fleet.unavailable.clear()

    asyncio.run(fleet.source_disconnected("sophie@lovelace", 12, 1))

    assert "will@lovelace" in fleet.sessions
    assert "sophie@lovelace" not in fleet.sessions
    assert fleet.unavailable == {"sophie@lovelace"}


def test_real_control_switches_remain_confined_to_two_tmux_sockets(tmp_path):
    controls = []
    try:
        for principal in ("will", "sophie"):
            socket = tmp_path / f"{principal}.sock"
            for name in ("from", "to"):
                subprocess.run(["tmux", "-S", str(socket), "new-session", "-d",
                                "-s", name, "sleep", "infinity"], check=True)
            process = subprocess.Popen(
                ["tmux", "-N", "-S", str(socket), "-C", "attach-session", "-f",
                 "ignore-size", "-t", "from"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            control = ControlClient(process, queue.Queue())
            control.command(["refresh-client", "-f", "no-output"])
            [client] = control.command(["display-message", "-p", "#{client_name}"])
            [identity] = control.command([
                "list-sessions", "-f", "#{==:#{session_name},to}", "-F",
                "#{q:socket_path} #{pid} #{start_time} #{q:session_id}"])
            path, pid, started, session_id = shlex.split(identity)
            control.switch((path, int(pid), int(started), session_id), client)
            assert control.command(["display-message", "-p", "#{session_name}"]) == ["to"]
            controls.append((socket, process))
    finally:
        for socket, process in controls:
            if process.poll() is None:
                process.terminate()
                process.wait()
            subprocess.run(["tmux", "-N", "-S", str(socket), "kill-server"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_viewer_keeps_one_attachment_per_same_host_runtime(tmp_path, monkeypatch):
    host = os.uname().nodename.split(".", 1)[0]
    ui_socket = tmp_path / "ui.sock"
    source_sockets = {}
    ui_process = None
    try:
        keys = {}
        for principal in ("will", "sophie"):
            socket = tmp_path / f"{principal}-source.sock"
            source_sockets[principal] = socket
            subprocess.run(["tmux", "-S", str(socket), "new-session", "-d", "-s",
                            principal, "sleep", "infinity"], check=True)
            identity = subprocess.run(
                ["tmux", "-N", "-S", str(socket), "list-sessions", "-F",
                 "#{pid} #{start_time} #{q:session_id}"], check=True, text=True,
                capture_output=True).stdout.strip()
            pid, started, session_id = shlex.split(identity)
            keys[principal] = (
                f"{principal}@{host}:{socket}:{pid}:{started}:{session_id}")
        subprocess.run(["tmux", "-S", str(ui_socket), "new-session", "-d", "-s",
                        "fleet@test", "sleep", "infinity"], check=True)
        ui_process = subprocess.Popen(
            ["tmux", "-N", "-S", str(ui_socket), "-C", "attach-session", "-t",
             "fleet@test"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        control = ControlClient(ui_process, queue.Queue())
        control.command(["refresh-client", "-f", "no-output"])
        state = viewer.Attachment("test", "/dev/pts/9", control)
        monkeypatch.setattr(viewer, "runtime_sources", lambda: [
            config.RuntimeSource(host, principal, f"/{principal}/loop.sock",
                                 str(source_sockets[principal]))
            for principal in ("will", "sophie")])
        monkeypatch.setattr(state, "prove_switch", mock.Mock())
        monkeypatch.setattr(state, "resident_switch", mock.Mock())
        attachments = {
            f"{principal}@{host}": mock.Mock(
                host=f"{principal}@{host}", source=keys[principal],
                window=f"@{index}", client=f"/dev/pts/{index}")
            for index, principal in enumerate(("will", "sophie"), 1)
        }
        monkeypatch.setattr(state, "create_host",
                            lambda source, key: attachments[source])
        monkeypatch.setattr(state, "select_host", lambda entry: None)
        monkeypatch.setattr(state, "ui_windows", lambda: {"@1", "@2"})

        state.open(keys["will"])
        state.open(keys["sophie"])
        state.open(keys["will"])

        assert set(state.attachments) == {f"will@{host}", f"sophie@{host}"}
        assert state.host == f"will@{host}"
    finally:
        if ui_process is not None and ui_process.poll() is None:
            ui_process.terminate()
            ui_process.wait()
        subprocess.run(["tmux", "-N", "-S", str(ui_socket), "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for socket in source_sockets.values():
            subprocess.run(["tmux", "-N", "-S", str(socket), "kill-server"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_sophie_bare_actor_bootstraps_and_creates_only_on_her_tmux_socket(
        tmp_path, monkeypatch):
    host = os.uname().nodename.split(".", 1)[0]
    sockets = {principal: tmp_path / f"{principal}.sock"
               for principal in ("will", "sophie")}
    process = None
    try:
        for socket in sockets.values():
            subprocess.run(["tmux", "-S", str(socket), "new-session", "-d", "-s",
                            "fleet@events", "sleep", "infinity"], check=True)
        actor = f"llm-collision@{host}"
        key = f"alan:sophie@{host}:{actor}"
        reply = json.dumps({
            "agent": "llm", "state": "waiting", "cwd": "/home/sophie",
            "attachment": "", "tmux_socket": str(sockets["sophie"]),
        })
        state = viewer.Attachment("test", "/dev/pts/9", mock.Mock())
        monkeypatch.setattr(viewer, "runtime_sources", lambda: [
            config.RuntimeSource(host, principal, f"/{principal}/loop.sock",
                                 str(sockets[principal]))
            for principal in ("will", "sophie")])
        monkeypatch.setattr(state, "daemon", lambda _request: reply)
        monkeypatch.setattr(presentation, "target", mock.Mock())

        resolved = state.resolve(key)

        assert resolved[0] == str(sockets["sophie"])
        presentation.target.assert_not_called()

        process = subprocess.Popen(
            ["tmux", "-N", "-S", str(sockets["sophie"]), "-C", "attach-session",
             "-f", "ignore-size", "-t", "fleet@events"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        control = ControlClient(process, queue.Queue())
        control.command(["refresh-client", "-f", "no-output"])
        name = "fleet@alan-" + alan.runtime_name(actor)

        def create(_actor, _descriptor):
            subprocess.run(config.tmux_command(
                "new-session", "-d", "-s", name, "sleep", "infinity"), check=True)

        monkeypatch.setattr(presentation, "target", create)
        monkeypatch.setenv("FLEET_TMUX_SOCKET", str(sockets["sophie"]))
        target = control.alan_target(actor, {"kind": "llm", "cwd": "/home/sophie"})

        assert target[0] == str(sockets["sophie"])
        sophie_names = subprocess.run(
            ["tmux", "-N", "-S", str(sockets["sophie"]), "list-sessions", "-F",
             "#{session_name}"], check=True, text=True, capture_output=True).stdout.splitlines()
        will_names = subprocess.run(
            ["tmux", "-N", "-S", str(sockets["will"]), "list-sessions", "-F",
             "#{session_name}"], check=True, text=True, capture_output=True).stdout.splitlines()
        assert name in sophie_names
        assert name not in will_names
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()
        for socket in sockets.values():
            subprocess.run(["tmux", "-N", "-S", str(socket), "kill-server"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
