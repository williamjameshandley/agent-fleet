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
from libtmux.exc import LibTmuxException
from libtmux.session import Session as TmuxSession
from watchfiles import watch

from .model import ServerRef, Session, SessionRef
from .agent import observe
from .config import RUNTIME
from .alan import Watcher as AlanWatcher, inventory as alan_inventory
from . import alan, presentation, transcripts as native_transcripts

PREVIEW = Path("/usr/lib/agent-fleet/fleet-preview")
UNSET = object()
SESSION_FORMAT = (
    "socket=x#{q:socket_path} pid=x#{q:pid} started=x#{q:start_time} "
    "id=x#{q:session_id} name=x#{q:session_name} "
    "created=x#{q:session_created} activity=x#{q:session_activity} "
    "attached=x#{q:session_attached} windows=x#{q:session_windows} "
    "command=x#{q:pane_current_command} title=x#{q:pane_title} "
    "path=x#{q:pane_current_path} human=x#{q:@fleet_human_activity}"
)


def server():
    return Server(tmux_bin=os.environ.get(
        "FLEET_TMUX", "/usr/lib/agent-fleet/fleet-tmux"))


class ControlSlot:
    def __init__(self):
        self._lock = threading.Lock()
        self._control = None

    def set(self, control):
        with self._lock:
            self._control = control

    def clear(self, control):
        with self._lock:
            if self._control is control:
                self._control = None

    def get(self):
        with self._lock:
            control = self._control
        if control is None or control.closed:
            raise RuntimeError("tmux server is unavailable")
        return control


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

    def alan_target(self, actor, descriptor=None):
        name = "fleet@alan-" + alan.runtime_name(actor)
        arguments = [
            "list-sessions", "-f", f"#{{==:#{{session_name}},{name}}}", "-F",
            "#{q:socket_path} #{pid} #{start_time} #{q:session_id}"]
        def locate():
            return [shlex.split(line) for line in self.command(arguments) if line]
        matches = locate()
        if (not matches and descriptor
                and descriptor["kind"] in {"python", "llm", "antigravity"}):
            presentation.target(actor, descriptor)
            matches = locate()
        if len(matches) != 1 or len(matches[0]) != 4:
            raise RuntimeError(f"Alan evaluator terminal is unavailable or ambiguous: {actor}")
        socket, pid, started, session_id = matches[0]
        return socket, int(pid), int(started), session_id


def capture(key, columns=0, lines=0, alan_graph=UNSET):
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
        if alan_graph is None:
            raise RuntimeError("Alan observation is unavailable")
        return alan.preview(addr, columns, lines,
                            None if alan_graph is UNSET else alan_graph)
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


def inventory(host, actor_descriptors):
    tmux = server()
    sessions = []
    names = []
    for line in tmux.cmd("list-sessions", "-F", SESSION_FORMAT).stdout:
        (socket, pid, started, session_id, name, created, activity,
         attached, windows, command, title, path, human_activity) = (
            field.split("=", 1)[1][1:] for field in shlex.split(line))
        names.append(name)
        if (name.startswith("fleet@")
                and not name.startswith("fleet@native-")):
            continue
        source = ServerRef(host, socket, int(pid), int(started))
        sessions.append(Session(
            SessionRef(source, session_id), name, int(created), int(activity),
            int(attached), int(windows), command, title, path,
            human_activity=int(human_activity or 0)))
    actors = [actor for actor in actor_descriptors
              if actor.get("evaluator") == "native"
              or presentation.available(actor["addr"], actor, names)]
    return sessions + alan_inventory(host, actors)


def watched_event(path, transcript_roots, quota_path):
    path = Path(path)
    if path == quota_path:
        return "quota"
    if any(path.is_relative_to(root) for root in transcript_roots):
        return "transcript"
    return None


def default_socket():
    root = Path(os.environ.get("TMUX_TMPDIR", "/tmp"))
    return root / f"tmux-{os.getuid()}" / "default"


def watch_socket(changed, consumer):
    socket = default_socket()
    while not consumer.is_set():
        parent = socket.parent
        while not parent.is_dir():
            parent = parent.parent
        expected = parent / socket.relative_to(parent).parts[0]
        for changes in watch(parent, recursive=False, stop_event=consumer,
                             yield_on_timeout=True):
            if expected.exists() or any(Path(path) == expected for _, path in changes):
                changed.put("socket")
                break


def event_stream(host, consumer=None, controls=None, changed=None, alan_watcher=None):
    changed = changed or queue.Queue()
    alan = alan_watcher or AlanWatcher(changed, consumer)
    if consumer:
        def disconnected():
            consumer.wait()
            changed.put("consumer")
        threading.Thread(target=disconnected, daemon=True).start()
    socket_consumer = consumer or threading.Event()
    threading.Thread(target=watch_socket, args=(changed, socket_consumer),
                     daemon=True).start()
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    transcript_roots = [path for path in (Path.home() / ".claude/projects",
                                          Path.home() / ".codex/sessions",
                                          Path.home() / ".gemini/antigravity-cli/brain")
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
    previous = None
    force = False
    agent_cache = {}
    alan_error = None
    process = control = None
    available = None
    def discard_control():
        nonlocal process, control
        if controls is not None and control is not None:
            controls.clear(control)
        if process is not None and process.poll() is None:
            process.terminate()
        if process is not None:
            process.wait()
        process = control = None
    try:
        while True:
            if control is not None and (control.closed or process.poll() is not None):
                discard_control()
                force = True
            if control is None:
                probe = subprocess.run(
                    ["/usr/bin/tmux", "-N", "list-sessions"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if not probe.returncode:
                    tmux = server()
                    if not tmux.has_session("fleet@events"):
                        created = subprocess.run(
                            ["/usr/bin/tmux", "-N", "new-session", "-d", "-s",
                             "fleet@events", "sleep infinity"],
                            text=True, capture_output=True)
                        if created.returncode:
                            raise RuntimeError(created.stderr.strip())
                    process = subprocess.Popen(
                        ["/usr/bin/tmux", "-N", "-C", "attach-session", "-f",
                         "ignore-size", "-t", "fleet@events"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, bufsize=1)
                    assert process.stdout and process.stdin
                    control = ControlClient(process, changed)
                    control.command(["refresh-client", "-f", "no-output"])
                    if controls is not None:
                        controls.set(control)
            if alan.error and alan.error != alan_error:
                print(alan.error, file=sys.stderr, flush=True)
            alan_error = alan.error
            with alan.snapshot() as (actors, graph):
                try:
                    current = inventory(host, actors) if control is not None else []
                except (subprocess.CalledProcessError, LibTmuxException) as error:
                    if control is None:
                        raise
                    probe = subprocess.run(
                        ["/usr/bin/tmux", "-N", "list-sessions"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if not probe.returncode:
                        raise error
                    discard_control()
                    force = True
                    continue
                if control is not None:
                    try:
                        current = observe(current, native_transcripts.catalog())
                        agent_cache = {session.ref: session for session in current}
                    except subprocess.CalledProcessError as error:
                        probe = subprocess.run(
                            ["/usr/bin/tmux", "-N", "list-sessions"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if probe.returncode:
                            discard_control()
                            force = True
                            continue
                        print(f"agent adapter: {error}", file=sys.stderr, flush=True)
                        current = [replace(session, agent_name=cached.agent_name,
                                           reported_state=cached.reported_state,
                                           summary=cached.summary, recency=cached.recency,
                                           transcript_id=cached.transcript_id,
                                           transcript_path=cached.transcript_path)
                                   if (cached := agent_cache.get(session.ref)) else session
                                   for session in current]
                serial = tuple(current)
                current_available = control is not None
                if serial != previous or force or current_available != available:
                    yield current, graph, current_available
                    previous = serial
                    force = False
                    available = current_available
            if consumer and consumer.is_set():
                return
            events = [changed.get()]
            while not changed.empty():
                events.append(changed.get_nowait())
            if consumer and consumer.is_set():
                return
            force = bool({"alan", "quota"} & set(events))
            if control is not None and ("closed" in events or process.poll() is not None):
                discard_control()
                force = True
    finally:
        socket_consumer.set()
        if controls is not None and control is not None:
            controls.clear(control)
        if process is not None and process.poll() is None:
            process.terminate()
        if process is not None:
            process.wait()
