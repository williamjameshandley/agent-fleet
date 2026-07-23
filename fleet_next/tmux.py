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
    commands = {
        "rename": ["rename-session", "-t", session_id, arguments[0]],
        "attention": ["set-option", "-t", session_id, "@fleet_attention", arguments[0]],
    }
    if operation not in commands:
        raise SystemExit(f"unknown mutation {operation!r}")
    condition = (f"#{{&&:#{{==:#{{socket_path}},{socket}}},"
                 f"#{{&&:#{{==:#{{pid}},{pid}}},"
                 f"#{{&&:#{{==:#{{start_time}},{started}}},"
                 f"#{{==:#{{session_id}},{session_id}}}}}}}}}")
    result = server().cmd("if-shell", "-t", session_id, "-F", condition,
                          shlex.join(commands[operation]),
                          "display-message -p FLEET_STALE")
    if result.stdout and result.stdout[0] == "FLEET_STALE":
        raise SystemExit(f"stale source identity: {key}")


def capture(key, columns=0, lines=0):
    host, socket, pid, started, session_id = split_key(key)
    if host != os.uname().nodename:
        raise RuntimeError(f"identity is for {host}, not {os.uname().nodename}")
    tmux = server()
    session = TmuxSession.from_session_id(tmux, session_id)
    if (session.socket_path, int(session.pid), int(session.start_time)) != (socket, pid, started):
        raise RuntimeError(f"stale source identity: {key}")
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
    metadata = {sid: (attention, int(activity or 0))
                for sid, attention, activity in (line.split("\t") for line in tmux.cmd(
                    "list-sessions", "-F",
                    "#{session_id}\t#{@fleet_attention}\t#{@fleet_human_activity}").stdout)}
    human_activity = {sid: activity for sid, (_, activity) in metadata.items()}
    for line in tmux.cmd("list-clients", "-F", "#{session_id}\t#{client_activity}").stdout:
        session_id, activity = line.split("\t", 1)
        human_activity[session_id] = max(human_activity.get(session_id, 0), int(activity))
    for session_id, activity in human_activity.items():
        if activity > metadata[session_id][1]:
            tmux.cmd("set-option", "-t", session_id,
                     "@fleet_human_activity", str(activity))
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
            metadata[item.session_id][0] or "tracked",
            human_activity=human_activity.get(item.session_id, 0)))
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
            current = inventory(host) + alan_inventory(host, alan.actors, alan.attention)
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
