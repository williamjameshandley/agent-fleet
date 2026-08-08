import os
import shlex
import subprocess
import queue
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from libtmux import Server
from libtmux.session import Session as TmuxSession
from watchfiles import watch

from .model import ServerRef, Session, SessionRef
from .agent import observe
from .config import RUNTIME
from .alan import (Watcher as AlanWatcher, inventory as alan_inventory,
                   projection_graph as alan_projection_graph)
from . import alan, transcripts as native_transcripts

PREVIEW = Path("/usr/lib/agent-fleet/fleet-preview")


def server():
    return Server(tmux_bin=os.environ.get(
        "FLEET_TMUX", "/usr/lib/agent-fleet/fleet-tmux"))


def split_key(key):
    host_socket, pid, started, session_id = key.rsplit(":", 3)
    host, socket = host_socket.split(":", 1)
    return host, socket, int(pid), int(started), session_id


def mutate(key, operation, arguments):
    host, socket, pid, started, session_id = split_key(key)
    if host != os.uname().nodename:
        raise ValueError(f"identity is for {host}, not {os.uname().nodename}")
    if operation == "rename":
        command = ["rename-session", "-t", session_id, arguments[0]]
    elif operation == "archive":
        command = ["kill-session", "-t", session_id]
    else:
        raise ValueError(f"unknown mutation {operation!r}")
    condition = (f"#{{&&:#{{==:#{{socket_path}},{socket}}},"
                 f"#{{&&:#{{==:#{{pid}},{pid}}},"
                 f"#{{&&:#{{==:#{{start_time}},{started}}},"
                 f"#{{==:#{{session_id}},{session_id}}}}}}}}}")
    result = server().cmd("if-shell", "-t", session_id, "-F", condition,
                          shlex.join(command),
                          "display-message -p FLEET_STALE")
    if result.stdout and result.stdout[0] == "FLEET_STALE":
        raise RuntimeError(f"stale source identity: {key}")


def switch_session(socket, pid, started, session_id, client):
    began = time.monotonic()
    condition = ("#{&&:#{==:#{socket_path},%s},"
                 "#{&&:#{==:#{pid},%s},"
                 "#{&&:#{==:#{start_time},%s},"
                 "#{==:#{session_id},%s}}}}" %
                 (socket, pid, started, session_id))
    result = server().cmd("if-shell", "-t", session_id, "-F", condition,
                          shlex.join(["switch-client", "-c", client, "-t", session_id]),
                          "display-message -p FLEET_STALE")
    if result.stdout and result.stdout[0] == "FLEET_STALE":
        raise RuntimeError("source or viewer client identity changed")
    return time.monotonic() - began


def client_ready(target_value, client):
    socket, pid, started, session_id = target_value
    condition = ("#{&&:#{==:#{socket_path},%s},"
                 "#{&&:#{==:#{pid},%s},"
                 "#{&&:#{==:#{start_time},%s},#{==:#{session_id},%s}}}}" %
                 (socket, pid, started, session_id))
    checked = server().cmd("if-shell", "-t", session_id, "-F", condition,
                           "display-message -p FLEET_READY",
                           "display-message -p FLEET_STALE")
    if checked.stdout != ["FLEET_READY"]:
        return False
    attached = server().cmd("list-clients", "-t", session_id, "-F", "#{client_name}")
    return client in attached.stdout


class ControlClient:
    """Serialize commands and notifications on one tmux control client."""
    def __init__(self, process, changed):
        self.process = process
        self.changed = changed
        self.lock = threading.RLock()
        self.pending = None
        self.closed = False
        self.ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        response = None
        response_id = None
        for raw in self.process.stdout:
            line = raw.rstrip("\n")
            if line.startswith("%begin "):
                if self.pending and response is None:
                    response_id = int(line.split()[2])
                    response = []
            elif line.startswith(("%end ", "%error ")):
                terminal_id = int(line.split()[2])
                if (response is not None and self.pending and
                        terminal_id == response_id):
                    self.pending["success"] &= line.startswith("%end ")
                    self.pending["output"].extend(response)
                    self.pending["remaining"] -= 1
                    if not self.pending["remaining"]:
                        self.pending["reply"].put(
                            (self.pending["success"], self.pending["output"]))
                    response = None
                else:
                    self.ready.set()
            elif response is not None and not line.startswith("%"):
                response.append(line)
            elif response is not None and line == "%message FLEET_STALE":
                response.append("FLEET_STALE")
            elif line.startswith(("%sessions-changed", "%session-renamed", "%session-changed",
                                  "%window-add", "%window-close", "%window-renamed",
                                  "%unlinked-window-add", "%unlinked-window-close",
                                  "%layout-change", "%client-session-changed")):
                self.changed.put("tmux")
        self.closed = True
        if self.pending:
            self.pending["reply"].put((False, ["tmux control client closed"]))
        self.changed.put("closed")

    def command(self, arguments, replies=1):
        with self.lock:
            self.ready.wait()
            if self.closed or self.process.poll() is not None:
                raise RuntimeError("tmux control client closed")
            reply = queue.Queue(maxsize=1)
            self.pending = {"reply": reply, "remaining": replies,
                            "success": True, "output": []}
            try:
                self.process.stdin.write(shlex.join(arguments) + "\n")
                self.process.stdin.flush()
                success, output = reply.get()
            finally:
                self.pending = None
            if not success:
                raise RuntimeError("\n".join(output) or "tmux command failed")
            return output

    def switch(self, target, client):
        socket, pid, started, session_id = target
        condition = ("#{&&:#{==:#{socket_path},%s},"
                     "#{&&:#{==:#{pid},%s},"
                     "#{&&:#{==:#{start_time},%s},"
                     "#{==:#{session_id},%s}}}}" % target)
        began = time.monotonic()
        success = shlex.join(["switch-client", "-c", client, "-t", session_id])
        success += " ; display-message -p FLEET_SWITCHED"
        output = self.command([
            "if-shell", "-t", session_id, "-F", condition, success,
            "display-message -p FLEET_STALE"], replies=2)
        if output != ["FLEET_SWITCHED"]:
            raise RuntimeError("source or viewer client identity changed")
        return time.monotonic() - began

    def alan_target(self, actor):
        name = "fleet@alan-" + alan.runtime_name(actor)
        output = self.command([
            "list-sessions", "-f", f"#{{==:#{{session_name}},{name}}}", "-F",
            "#{q:socket_path} #{pid} #{start_time} #{q:session_id}"])
        matches = [shlex.split(line) for line in output if line]
        if len(matches) != 1 or len(matches[0]) != 4:
            raise RuntimeError(f"Alan evaluator terminal is unavailable or ambiguous: {actor}")
        socket, pid, started, session_id = matches[0]
        return socket, int(pid), int(started), session_id


def capture(key, columns=0, lines=0):
    if key.startswith("alan:"):
        addr = key.removeprefix("alan:")
        kind = addr.split("-", 1)[0]
        if kind in {"claude", "codex"}:
            name = "fleet@alan-" + alan.runtime_name(addr)
            session = next((item for item in server().sessions
                            if item.session_name == name), None)
            if session is None:
                raise RuntimeError(
                    f"{kind.capitalize()} evaluator terminal is unavailable: {addr}"
                )
            return capture_pane(session, columns, lines)
        return alan.preview(addr, columns, lines)
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


def watched_event(path, transcript_roots, quota_path):
    path = Path(path)
    if path == quota_path:
        return "quota"
    if any(path.is_relative_to(root) for root in transcript_roots):
        return "transcript"
    return None


def event_stream(host, consumer=None, controls=None, changed=None):
    changed = changed or queue.Queue()
    alan = AlanWatcher(changed, consumer)
    if consumer:
        def disconnected():
            consumer.wait()
            changed.put("consumer")
        threading.Thread(target=disconnected, daemon=True).start()
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    transcript_roots = [path for path in (Path.home() / ".claude/projects",
                                          Path.home() / ".codex/sessions")
                        if path.exists()]
    paths = transcript_roots + ([RUNTIME] if RUNTIME.exists() else [])
    if paths:
        def transcripts():
            quota_path = RUNTIME / "quota.changed"
            # One transcript event publishes a full host inventory. Group the
            # short pauses between streamed token writes instead of promoting
            # each burst into a Fleet-wide update.
            for changes in watch(*paths, step=200):
                events = {watched_event(path, transcript_roots, quota_path)
                          for _, path in changes}
                for event in events - {None}:
                    changed.put(event)
        threading.Thread(target=transcripts, daemon=True).start()
    probe = subprocess.run(["/usr/bin/tmux", "-N", "list-sessions"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode:
        raise RuntimeError("tmux server is not running")
    tmux = server()
    if not tmux.has_session("fleet@events"):
        created = subprocess.run(
            ["/usr/bin/tmux", "-N", "new-session", "-d", "-s", "fleet@events", "sleep infinity"],
            text=True, capture_output=True)
        if created.returncode:
            raise RuntimeError(created.stderr.strip())
    process = subprocess.Popen(["/usr/bin/tmux", "-N", "-C", "attach-session",
                                "-f", "ignore-size", "-t", "fleet@events"],
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1)
    assert process.stdout and process.stdin
    control = ControlClient(process, changed)
    control.command(["refresh-client", "-f", "no-output"])
    if controls is not None:
        controls.put(control)
    previous = None
    force = False
    barriers = []
    authority_refresh = False
    agent_cache = {}
    alan_error = None
    try:
        while True:
            if alan.error and alan.error != alan_error:
                print(alan.error, file=sys.stderr, flush=True)
            alan_error = alan.error
            actors, graph = (alan.refresh() if authority_refresh
                             else alan.snapshot())
            current = inventory(host) + alan_inventory(host, actors)
            try:
                current = observe(current, native_transcripts.catalog())
                agent_cache = {session.ref: session for session in current}
            except RuntimeError as error:
                print(f"agent adapter: {error}", file=sys.stderr, flush=True)
                current = [replace(session, agent_name=cached.agent_name,
                                   reported_state=cached.reported_state,
                                   summary=cached.summary, recency=cached.recency,
                                   transcript_id=cached.transcript_id,
                                   transcript_path=cached.transcript_path)
                           if (cached := agent_cache.get(session.ref)) else session
                           for session in current]
            serial = tuple(current)
            if serial != previous or force:
                yield current, alan_projection_graph(graph)
                previous = serial
                force = False
                for barrier in barriers:
                    barrier.set()
                barriers.clear()
                authority_refresh = False
            if consumer and consumer.is_set():
                return
            events = [changed.get()]
            while not changed.empty():
                events.append(changed.get_nowait())
            if consumer and consumer.is_set():
                return
            authorities = [event for event in events
                           if isinstance(event, tuple) and event[0] == "authority"]
            barriers.extend(event[1] for event in authorities)
            authority_refresh = any(event[2] for event in authorities)
            force = "quota" in events or bool(barriers)
            if "closed" in events or process.poll() is not None:
                error = process.stderr.read().strip() if process.stderr else ""
                raise RuntimeError(error or "tmux control client closed")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
