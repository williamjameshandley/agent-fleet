import os
import shlex
import subprocess
import queue
import sys
import threading
from dataclasses import replace
from pathlib import Path

from watchfiles import watch

from .model import ServerRef, Session, SessionRef
from .agent import observe
from .config import RUNTIME


FORMAT = " ".join((
    "pid=#{pid}", "started=#{start_time}", "socket=#{q:socket_path}", "id=#{session_id}",
    "name=#{q:session_name}", "created=#{session_created}",
    "activity=#{session_activity}", "attached=#{session_attached}",
    "windows=#{session_windows}", "command=#{q:pane_current_command}",
    "title=#{q:pane_title}", "cwd=#{q:pane_current_path}",
    "attention=#{q:@fleet_attention}",
))


def run(*args, check=True, capture_output=True):
    return subprocess.run(["tmux", *args], text=True, check=check,
                          capture_output=capture_output)


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
    result = run("if-shell", "-t", session_id, "-F", condition,
                 shlex.join(commands[operation]), "display-message -p FLEET_STALE")
    if result.stdout.strip() == "FLEET_STALE":
        raise SystemExit(f"stale source identity: {key}")


def inventory(host):
    result = run("list-sessions", "-F", FORMAT, check=False)
    if result.returncode == 1 and "no server running" in result.stderr:
        return []
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    sessions = []
    for line in result.stdout.splitlines():
        values = dict(field.split("=", 1) for field in shlex.split(line))
        pid, started, socket, sid, name = (values[x] for x in
                                           ("pid", "started", "socket", "id", "name"))
        created, activity, attached, windows = (values[x] for x in
                                                 ("created", "activity", "attached", "windows"))
        command, title, cwd, attention = (values[x] for x in
                                          ("command", "title", "cwd", "attention"))
        if name.startswith("fleet@"):
            continue
        server = ServerRef(host, socket, int(pid), int(started))
        sessions.append(Session(SessionRef(server, sid), name, int(created),
                                int(activity), int(attached), int(windows),
                                command, title, cwd, attention or "tracked"))
    return sessions


def event_stream(host):
    changed = queue.Queue()
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = [path for path in (Path.home() / ".claude/projects",
                               Path.home() / ".codex/sessions", RUNTIME) if path.exists()]
    if paths:
        def transcripts():
            for _ in watch(*paths):
                changed.put("transcript")
        threading.Thread(target=transcripts, daemon=True).start()
    if run("has-session", "-t", "=fleet@events", check=False).returncode:
        run("new-session", "-d", "-s", "fleet@events", "sleep", "infinity")
    process = subprocess.Popen(["tmux", "-C", "attach-session", "-f", "ignore-size",
                                "-t", "=fleet@events"], stdin=subprocess.PIPE,
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
    agent_cache = {}
    try:
        while True:
            current = inventory(host)
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
            if serial != previous:
                yield current
                previous = serial
            event = changed.get()
            while not changed.empty():
                event = changed.get_nowait()
            if event == "closed":
                error = process.stderr.read().strip() if process.stderr else ""
                raise RuntimeError(error or "tmux control client closed")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
