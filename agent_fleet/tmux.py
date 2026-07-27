import os
import shlex
import subprocess
import queue
import sys
import threading
from dataclasses import replace
from pathlib import Path

from libtmux import Server
from libtmux.session import Session as TmuxSession
from watchfiles import watch

from .model import ServerRef, Session, SessionRef
from .agent import observe
from .config import RUNTIME
from .alan import Watcher as AlanWatcher, inventory as alan_inventory

PREVIEW = Path("/usr/lib/agent-fleet/fleet-preview")
_alan_attachments = {}


def server():
    return Server()


def split_key(key):
    host_socket, pid, started, session_id = key.rsplit(":", 3)
    host, socket = host_socket.split(":", 1)
    return host, socket, int(pid), int(started), session_id


def mutate(key, operation, arguments):
    host, socket, pid, started, session_id = split_key(key)
    if host != os.uname().nodename:
        raise SystemExit(f"identity is for {host}, not {os.uname().nodename}")
    if operation == "rename":
        command = ["rename-session", "-t", session_id, arguments[0]]
    elif operation == "archive":
        command = ["kill-session", "-t", session_id]
    else:
        raise SystemExit(f"unknown mutation {operation!r}")
    condition = (f"#{{&&:#{{==:#{{socket_path}},{socket}}},"
                 f"#{{&&:#{{==:#{{pid}},{pid}}},"
                 f"#{{&&:#{{==:#{{start_time}},{started}}},"
                 f"#{{==:#{{session_id}},{session_id}}}}}}}}}")
    result = server().cmd("if-shell", "-t", session_id, "-F", condition,
                          shlex.join(command),
                          "display-message -p FLEET_STALE")
    if result.stdout and result.stdout[0] == "FLEET_STALE":
        raise SystemExit(f"stale source identity: {key}")


def capture(key, columns=0, lines=0):
    if key.startswith("alan:"):
        _, host, addr = key.split(":", 2)
        if host != os.uname().nodename:
            raise RuntimeError(f"identity is for {host}, not {os.uname().nodename}")
        attachment = _alan_attachments.get(addr)
        if attachment is None:
            raise RuntimeError(f"Alan actor disappeared: {addr}")
        if attachment.get("kind") != "tmux":
            raise RuntimeError(f"Alan actor {addr} has no tmux presentation")
        tmux = server()
        name = attachment["session"]
        generation = attachment.get("generation", "")
        if tmux.cmd("show-options", "-v", "-t", name,
                    "@fleet_generation").stdout != [generation]:
            raise RuntimeError(f"stale Alan presentation: {addr}")
        session = next((item for item in tmux.sessions if item.session_name == name), None)
        if session is None:
            raise RuntimeError(f"Alan presentation disappeared: {addr}")
        return capture_pane(session, columns, lines)
    host, socket, pid, started, session_id = split_key(key)
    if host != os.uname().nodename:
        raise RuntimeError(f"identity is for {host}, not {os.uname().nodename}")
    tmux = server()
    session = TmuxSession.from_session_id(tmux, session_id)
    if (session.socket_path, int(session.pid), int(session.start_time)) != (socket, pid, started):
        raise RuntimeError(f"stale source identity: {key}")
    return capture_pane(session, columns, lines)


def capture_pane(session, columns=0, lines=0):
    pane = session.active_pane
    content = pane.capture_pane(start=0, end="-", escape_sequences=True,
                                preserve_trailing=True) or []
    if not columns or not lines:
        return "\n".join(content)
    result = subprocess.run(
        [PREVIEW, pane.pane_width, pane.pane_height, pane.cursor_x, pane.cursor_y,
         str(columns), str(lines)], input="\n".join(content) + "\n", text=True,
        capture_output=True, check=True)
    return result.stdout


def inventory(host):
    tmux = server()
    metadata = {sid: int(activity or 0)
                for sid, activity in (line.split("\t") for line in tmux.cmd(
                    "list-sessions", "-F",
                    "#{session_id}\t#{@fleet_human_activity}").stdout)}
    sessions = []
    for item in tmux.sessions:
        if item.session_name.startswith("fleet@"):
            continue
        source = ServerRef(host, item.socket_path, int(item.pid), int(item.start_time))
        sessions.append(Session(
            SessionRef(source, item.session_id), item.session_name,
            int(item.session_created), int(item.session_activity),
            int(item.session_attached), int(item.session_windows),
            item.pane_current_command, item.pane_title, item.pane_current_path,
            human_activity=metadata[item.session_id]))
    return sessions


def event_stream(host, consumer=None):
    changed = queue.Queue()
    alan = AlanWatcher(changed, consumer)
    if consumer:
        def disconnected():
            consumer.wait()
            changed.put("consumer")
        threading.Thread(target=disconnected, daemon=True).start()
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [path for path in (Path.home() / ".claude/projects",
                               Path.home() / ".codex/sessions", RUNTIME) if path.exists()]
    if paths:
        def transcripts():
            quota_path = RUNTIME / "quota.changed"
            for changes in watch(*paths):
                changed.put("quota" if any(Path(path) == quota_path for _, path in changes)
                            else "transcript")
        threading.Thread(target=transcripts, daemon=True).start()
    tmux = server()
    if not tmux.has_session("fleet@events"):
        tmux.new_session("fleet@events", attach=False,
                         window_command="sleep infinity")
    process = subprocess.Popen(["tmux", "-C", "attach-session", "-f", "ignore-size",
                                "-t", "fleet@events"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)
    assert process.stdout and process.stdin
    process.stdin.write("refresh-client -f no-output\n")
    process.stdin.flush()

    def topology():
        assert process.stdout
        for line in process.stdout:
            if line.startswith(("%sessions-changed", "%session-renamed", "%session-changed",
                                "%window-add", "%window-close", "%window-renamed",
                                "%unlinked-window-add", "%unlinked-window-close",
                                "%layout-change", "%client-session-changed")):
                changed.put("tmux")
        changed.put("closed")
    threading.Thread(target=topology, daemon=True).start()
    previous = None
    force = False
    agent_cache = {}
    alan_error = None
    try:
        while True:
            if alan.error and alan.error != alan_error:
                print(alan.error, file=sys.stderr, flush=True)
            alan_error = alan.error
            global _alan_attachments
            _alan_attachments = {
                actor["addr"]: actor.get("attachment") or {"kind": "none"}
                for actor in alan.actors if actor.get("type") in {"claude", "codex"}}
            current = inventory(host) + alan_inventory(host, alan.actors)
            try:
                current = observe(current)
                agent_cache = {session.ref: session for session in current}
            except RuntimeError as error:
                print(f"agent adapter: {error}", file=sys.stderr, flush=True)
                current = [replace(session, agent_name=cached.agent_name,
                                   reported_state=cached.reported_state,
                                   summary=cached.summary, recency=cached.recency,
                                   transcript_id=cached.transcript_id)
                           if (cached := agent_cache.get(session.ref)) else session
                           for session in current]
            serial = tuple(current)
            if serial != previous or force:
                yield current
                previous = serial
                force = False
            if consumer and consumer.is_set():
                return
            events = {changed.get()}
            while not changed.empty():
                events.add(changed.get_nowait())
            if consumer and consumer.is_set():
                return
            force = "quota" in events
            if "closed" in events or process.poll() is not None:
                error = process.stderr.read().strip() if process.stderr else ""
                raise RuntimeError(error or "tmux control client closed")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
