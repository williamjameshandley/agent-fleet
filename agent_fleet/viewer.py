import os
import json
import re
import shlex
import socket
import subprocess
import time
import queue
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from .config import HUB, RUNTIME, runtime_sources, ssh_environment
from .daemon import request as daemon_request
from .model import key_actor, key_host, key_source
from . import alan, journal, presentation, proc, workstation
from .tmux import ControlClient, split_key


SLOT = re.compile(r"^[A-Za-z0-9_-]+$")
TMUX_SESSION = re.compile(r"^\$[0-9]+$")
EXPECTED = (LookupError, OSError, RuntimeError, ValueError,
            subprocess.CalledProcessError)


class ViewerFailure(RuntimeError):
    def __init__(self, stage, cause, error):
        super().__init__(str(error))
        self.stage = stage
        self.cause = cause
        self.error_type = type(error).__name__


def source_host(key):
    sources = {source.key for source in runtime_sources()}
    try:
        if key.startswith("alan:"):
            actor = key_actor(key)
            source = key_source(key)
            if not actor or source not in sources:
                raise ValueError("incomplete Alan identity")
            return source
        source, socket_path, pid, started, session = split_key(key)
        if (source not in sources or not socket_path.startswith("/") or
                pid <= 0 or started <= 0 or not TMUX_SESSION.fullmatch(session)):
            raise ValueError("incomplete tmux identity")
        return source
    except (AttributeError, TypeError, ValueError) as error:
        raise ViewerFailure("resolve", "invalid_identity", error) from error


@contextmanager
def boundary(stage, cause):
    try:
        yield
    except ViewerFailure:
        raise
    except EXPECTED as error:
        raise ViewerFailure(stage, cause, error) from error


def check_slot(slot):
    if not SLOT.fullmatch(slot):
        raise ValueError(f"invalid viewer slot {slot!r}")
    return slot


def exchange(slot, message):
    check_slot(slot)
    path = RUNTIME / f"viewer-{slot}.sock"
    with socket.socket(socket.AF_UNIX) as client:
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            raise RuntimeError(f"viewer slot {slot!r} is not running")
        client.sendall((message + "\n").encode())
        reply = client.makefile().readline().strip()
        if message != "STATUS" and reply != "OK":
            raise RuntimeError(reply or f"viewer {slot!r} did not acknowledge")
        return reply


def attached_workstation():
    result = subprocess.run(
        ["/usr/bin/tmux", "-N", "show-options", "-qv", "-t", "fleet@muster",
         "@fleet_workstation"], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def focus(slot, name):
    with boundary("focus", "workstation"):
        if not name:
            raise RuntimeError("Muster has no attached workstation")
        workstation.request(name, {"operation": "focus", "slot": slot})


def viewer_error(value):
    command = (["/usr/bin/tmux", "-N", "set-option", "-t", "=fleet@muster:",
                "@fleet_viewer_error", value] if value else
               ["/usr/bin/tmux", "-N", "set-option", "-u", "-t", "=fleet@muster:",
                "@fleet_viewer_error"])
    stored = subprocess.run(command, text=True, capture_output=True)
    if stored.returncode:
        journal.record("viewer_error_unpublished", surface="tmux",
                       error=stored.stderr.strip() or str(stored.returncode))
    path = RUNTIME / "muster.sock"
    if path.exists():
        posted = subprocess.run(
            ["curl", "-fsS", "--max-time", "2", "--unix-socket", str(path),
             "-XPOST", "-d", "transform-header(/usr/lib/agent-fleet/ui header)",
             "http://localhost"], text=True, capture_output=True)
        if posted.returncode:
            journal.record("viewer_error_unpublished", surface="muster",
                           error=posted.stderr.strip() or str(posted.returncode))


def request(slot, key):
    exchange(slot, f"OPEN {key}" if key else "CLEAR")


def slots():
    sessions = subprocess.run(
        ["/usr/bin/tmux", "-L", "agent-fleet-ui", "list-sessions",
         "-F", "#{session_name}"], text=True, capture_output=True)
    names = sessions.stdout.splitlines() if sessions.returncode == 0 else []
    found = []
    for name in sorted(names):
        slot = name.removeprefix("fleet@")
        if slot == name or not SLOT.fullmatch(slot):
            continue
        try:
            found.append((slot, exchange(slot, "STATUS")))
        except RuntimeError as error:
            journal.record("viewer_slot_unavailable", slot=slot, error=str(error))
            found.append((slot, None))
    return found


def open_main(key):
    request("main", key)
    from .ui import select
    select(key)


def show(key, slot=None):
    available = slots()
    selected = next((name for name, source in available if source == key), None)
    if selected:
        open_main(key) if selected == "main" else request(selected, key)
        return
    if slot:
        open_main(key) if slot == "main" else request(slot, key)
        return
    free = next((name for name, source in available if source == ""), None)
    if free:
        open_main(key) if free == "main" else request(free, key)
        return
    if len(available) == 1 and available[0][0] == "main":
        open_main(key)
        return
    unresponsive = [name for name, source in available if source is None]
    message = "All viewer slots are occupied; choose a slot explicitly"
    if unresponsive:
        message += " (unresponsive: " + " ".join(unresponsive) + ")"
    subprocess.run(["/usr/bin/tmux", "-N", "display-message", "-t", "fleet@muster",
                    message])


def process_identity(pid):
    return proc.start_time(pid)


def process_alive(identity):
    if not identity:
        return True
    pid, started = identity
    try:
        return process_identity(pid) == started
    except (FileNotFoundError, ProcessLookupError):
        return False


class Attachment:
    def __init__(self, slot, tty, ui=None):
        self.slot = slot
        self.tty = tty
        self.source = ""
        self.host = ""
        self.daemon_socket = None
        self.daemon_master = None
        self.switch_duration = 0.0
        self.attachments = {}
        self.ui_process = None
        self.workstation = ""
        if ui is None:
            self.ui_process = subprocess.Popen(
                ["/usr/bin/tmux", "-L", "agent-fleet-ui", "-C", "attach-session",
                 "-t", f"fleet@{slot}"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1)
            ui = ControlClient(self.ui_process, queue.Queue())
            ui.command(["refresh-client", "-f", "no-output,ignore-size"])
            self.workstation = attached_workstation()
        self.ui = ui

    def ssh(self, host, *arguments, capture=False, check=True):
        command = ["ssh", "-o", "BatchMode=yes", host, *arguments]
        with boundary("ssh", "command"):
            return subprocess.run(command, text=True, capture_output=capture, check=check,
                                  env=ssh_environment())

    def master_identity(self, host):
        with boundary("ssh", "unavailable"):
            checked = subprocess.run(
                ["ssh", "-O", "check", host],
                text=True, capture_output=True, check=True, env=ssh_environment())
            match = re.search(r"pid=(\d+)", checked.stdout + checked.stderr)
            if not match:
                raise RuntimeError("SSH did not report its master identity")
            pid = int(match.group(1))
            return pid, process_identity(pid)

    def master_policy(self, host):
        with boundary("ssh", "policy"):
            result = subprocess.run(["ssh", "-G", host], text=True, capture_output=True,
                                    check=True, env=ssh_environment())
            policy = dict(line.split(None, 1) for line in result.stdout.splitlines()
                          if " " in line)
            if policy.get("controlmaster") not in {"yes", "auto"}:
                raise RuntimeError(f"SSH ControlMaster is not configured for {host}")
            if not policy.get("controlpath") or policy["controlpath"] == "none":
                raise RuntimeError(f"SSH ControlPath is not configured for {host}")
            if policy.get("controlpersist") in {None, "no", "0"}:
                raise RuntimeError(f"persistent SSH ControlMaster is not configured for {host}")

    def ensure_master(self, host):
        with boundary("ssh", "unavailable"):
            checked = subprocess.run(["ssh", "-O", "check", host], env=ssh_environment(),
                                     text=True, capture_output=True)
            if checked.returncode:
                self.master_policy(host)
                subprocess.run(["ssh", "-MNf", "-o", "BatchMode=yes", host],
                               check=True, env=ssh_environment())
            return self.master_identity(host)

    def daemon_forward(self):
        local = RUNTIME / f"viewer-{self.slot}-fleet.sock"
        remote = f"/run/user/{os.getuid()}/agent-fleet/fleet.sock"
        return local, f"{local}:{remote}"

    def cancel_daemon_forward(self):
        local, specification = self.daemon_forward()
        subprocess.run(["ssh", "-O", "cancel", "-L", specification, HUB],
                       env=ssh_environment(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        local.unlink(missing_ok=True)

    @staticmethod
    def socket_request(path, message):
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(path))
            client.sendall((message + "\n").encode())
            chunks = []
            while chunk := client.recv(65536):
                chunks.append(chunk)
        return b"".join(chunks).decode()

    def daemon(self, message):
        with boundary("daemon", "unavailable"):
            local = os.uname().nodename.split(".", 1)[0]
            if local == HUB:
                return daemon_request(message)
            if self.daemon_socket and not process_alive(self.daemon_master):
                self.daemon_socket.unlink(missing_ok=True)
                self.daemon_socket = self.daemon_master = None
            if not self.daemon_socket:
                forwarded, specification = self.daemon_forward()
                self.daemon_master = self.ensure_master(HUB)
                self.cancel_daemon_forward()
                try:
                    with boundary("ssh", "command"):
                        subprocess.run(
                            ["ssh", "-O", "forward", "-o", "StreamLocalBindUnlink=yes",
                             "-L", specification, HUB], check=True,
                            env=ssh_environment())
                    value = self.socket_request(forwarded, message)
                except Exception:
                    self.cancel_daemon_forward()
                    raise
                self.daemon_socket = forwarded
                return value
            return self.socket_request(self.daemon_socket, message)

    def resident_switch(self, key, client):
        try:
            value = json.loads(self.daemon("switch " + json.dumps(
                {"key": key, "client": client}, separators=(",", ":"))))
        except ViewerFailure:
            raise
        except (TypeError, ValueError) as error:
            raise ViewerFailure("daemon", "invalid_reply", error) from error
        if set(value) == {"error"}:
            error = RuntimeError(value["error"])
            raise ViewerFailure("switch", "identity_or_client", error) from error
        if set(value) != {"target", "duration", "name", "host"}:
            error = RuntimeError("invalid Fleet switch response")
            raise ViewerFailure("daemon", "invalid_reply", error) from error
        self.switch_duration = value["duration"]
        if self.slot == "main":
            self.set_header(value["name"], value["host"])
        return tuple(value["target"])

    def set_header(self, name, host):
        label = f" {name} [{host}]"
        literal = label.replace("#", "##").replace("}", "#}")
        with boundary("select", "header"):
            self.ui.command(["set-option", "-t", "=fleet@main:",
                             "status-left", f"#[fg=color3]#{{l:{literal}}}"])

    def reclaim_marker(self, host, owner):
        try:
            value = json.loads(self.daemon("cleanup " + json.dumps(
                {"host": host, "owner": owner, "slot": self.slot},
                separators=(",", ":"))))
        except ViewerFailure:
            raise
        except (TypeError, ValueError) as error:
            raise ViewerFailure("daemon", "invalid_reply", error) from error
        if set(value) == {"error"}:
            error = RuntimeError(value["error"])
            raise ViewerFailure("daemon", "refused", error) from error
        if value != {"ok": True}:
            error = RuntimeError("invalid Fleet cleanup response")
            raise ViewerFailure("daemon", "invalid_reply", error) from error

    def prove_switch(self, key, client, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.resident_switch(key, client)
            except RuntimeError as error:
                if "can't find client" not in str(error) or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)

    def ui_value(self, window, value):
        with boundary("attach", "client_registration"):
            output = self.ui.command(["display-message", "-p", "-t", window, value])
            if len(output) != 1:
                raise RuntimeError(f"UI server did not report {value}")
            return output[0]

    def ui_windows(self):
        with boundary("attach", "window"):
            return set(self.ui.command(
                ["list-windows", "-t", f"=fleet@{self.slot}",
                 "-F", "#{window_id}"]))

    def create_host(self, host, key):
        local = os.uname().nodename.split(".", 1)[0]
        remote_file = (f"${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}/agent-fleet/"
                       f"viewer-{local}-{self.slot}-{host}.tty")
        owner = local
        master = self.ensure_master(host)
        expected = self.resolve(key, remote=True)
        self.reclaim_marker(host, owner)
        body = (f"mkdir -p \"$(dirname {remote_file})\"; "
                f"chmod 700 \"$(dirname {remote_file})\"; tty > {remote_file}; "
                f"exec /usr/lib/agent-fleet/fleet-tmux -S {shlex.quote(str(expected[0]))} attach-session "
                f"-t {shlex.quote(expected[3])}")
        command = shlex.join(["ssh", "-tt", "-o", "BatchMode=yes", host, body])
        with boundary("attach", "window"):
            output = self.ui.command(["new-window", "-d", "-P", "-F", "#{window_id}",
                                      "-t", f"fleet@{self.slot}", "-n", host, command])
            if len(output) != 1:
                raise RuntimeError("UI server did not create one presentation window")
        window = output[0]
        try:
            wait = (f"for _ in $(seq 150); do [ -s {remote_file} ] && "
                    f"exec cat {remote_file}; sleep .02; done; exit 1")
            result = self.ssh(host, wait, capture=True, check=False)
            if result.returncode not in (0, 1):
                error = RuntimeError(result.stderr.strip()
                                     or f"ssh exited {result.returncode}")
                raise ViewerFailure("ssh", "command", error) from error
            client = result.stdout.strip() if result.returncode == 0 else ""
            if not client:
                error = RuntimeError("remote tmux did not report the viewer attachment")
                raise ViewerFailure("attach", "client_registration", error) from error
            self.prove_switch(key, client)
            entry = SimpleNamespace(host=host, window=window, client=client,
                                    source=key, remote_file=remote_file,
                                    owner=owner, master=master)
            journal.record("attachment_created", slot=self.slot, host=host,
                           route="remote",
                           window=window, client=client)
            return entry
        except Exception:
            try:
                self.remove_window(window)
            except EXPECTED as cleanup_error:
                journal.record("attachment_cleanup_failed", slot=self.slot,
                               host=host, window=window, step="window",
                               error=str(cleanup_error))
            else:
                journal.record("attachment_removed", slot=self.slot, host=host,
                               window=window, reason="create_failed")
            if remote_file:
                try:
                    self.reclaim_marker(host, owner)
                except RuntimeError as cleanup_error:
                    journal.record("attachment_cleanup_failed", slot=self.slot,
                                   host=host, window=window, step="marker",
                                   error=str(cleanup_error))
            raise

    def select_host(self, entry):
        with boundary("select", "window"):
            self.ui.command(["select-window", "-t", entry.window])

    def find(self, key):
        try:
            value = json.loads(self.daemon(f"resolve {key}"))
        except ViewerFailure:
            raise
        except (TypeError, ValueError) as error:
            raise ViewerFailure("daemon", "invalid_reply", error) from error
        if set(value) == {"error"}:
            error = RuntimeError(value["error"])
            raise ViewerFailure("resolve", "refused", error) from error
        if set(value) not in ({"agent", "state", "cwd", "attachment"},
                              {"agent", "state", "cwd", "attachment",
                               "tmux_socket"}):
            error = RuntimeError("invalid Fleet resolver response")
            raise ViewerFailure("daemon", "invalid_reply", error) from error
        return SimpleNamespace(**{"tmux_socket": "", **value})

    def resolve(self, key, remote=False):
        if not key.startswith("alan:"):
            try:
                _, socket_path, pid, started, sid = split_key(key)
            except (TypeError, ValueError) as error:
                raise ViewerFailure("resolve", "invalid_identity", error) from error
            return socket_path, pid, started, sid
        session = self.find(key)
        actor = key_actor(key)
        if session.state in {"retired", "unavailable"}:
            error = RuntimeError(f"Alan actor is {session.state}: {actor}")
            raise ViewerFailure("resolve", "unavailable", error) from error
        if session.attachment:
            try:
                return split_key(session.attachment)[1:]
            except (AttributeError, TypeError, ValueError) as error:
                raise ViewerFailure("resolve", "invalid_identity", error) from error
        name = "fleet@alan-" + alan.runtime_name(actor)
        fmt = "#{q:socket_path} #{pid} #{start_time} #{q:session_id}"
        tmux_socket = getattr(session, "tmux_socket", "")
        tmux_socket = tmux_socket if isinstance(tmux_socket, str) else ""
        socket_arguments = (["-S", tmux_socket] if tmux_socket else [])
        command = ["/usr/bin/tmux", "-N", *socket_arguments, "list-sessions", "-f",
                   f"#{{==:#{{session_name}},{name}}}", "-F", fmt]
        def locate():
            if remote:
                return self.ssh(source_host(key), shlex.join(command), capture=True)
            return subprocess.run(command, text=True, capture_output=True, check=True)
        with boundary("resolve", "unavailable"):
            result = locate()
            values = shlex.split(result.stdout.strip())
            if len(values) != 4 and session.agent not in {"claude", "codex", "grok"}:
                if tmux_socket:
                    bootstrap = ["/usr/bin/tmux", "-N", *socket_arguments,
                                 "list-sessions", "-f",
                                 "#{==:#{session_name},fleet@events}", "-F", fmt]
                    result = (self.ssh(source_host(key), shlex.join(bootstrap), capture=True)
                              if remote else subprocess.run(
                                  bootstrap, text=True, capture_output=True, check=True))
                    values = shlex.split(result.stdout.strip())
                elif remote:
                    self.ssh(source_host(key), shlex.join([
                        "/usr/lib/agent-fleet/fleet-present", actor, session.agent,
                        session.cwd]))
                else:
                    presentation.target(actor, {"kind": session.agent, "cwd": session.cwd})
                if not tmux_socket:
                    values = shlex.split(locate().stdout.strip())
            if len(values) != 4:
                raise RuntimeError(
                    f"{session.agent.capitalize()} evaluator terminal is unavailable: {actor}")
            return values[0], int(values[1]), int(values[2]), values[3]

    def open(self, key, selected=None, host=None):
        started = time.monotonic()
        new_host = source_host(key) if host is None else host
        entry = self.attachments.get(new_host)
        if entry is not None and entry.window not in self.ui_windows():
            self.remove_host(new_host, "missing")
            if new_host == self.host:
                self.source = self.host = ""
            entry = None
        if entry is None:
            path = "cold"
            entry = self.create_host(new_host, key)
            self.attachments[new_host] = entry
            try:
                self.select_host(entry)
            except Exception:
                self.remove_host(new_host, "select_failed")
                raise
        elif new_host == self.host:
            path = "same_host"
            self.resident_switch(key, entry.client)
            entry.source = key
        else:
            path = "cross_host"
            previous = entry.source
            self.resident_switch(key, entry.client)
            try:
                self.select_host(entry)
            except Exception:
                try:
                    self.resident_switch(previous, entry.client)
                except Exception:
                    self.remove_host(new_host, "rollback_failed")
                raise
            entry.source = key
        self.source, self.host = key, new_host
        duration = time.monotonic() - started
        acknowledged = time.clock_gettime(time.CLOCK_BOOTTIME)
        selection_duration = acknowledged - selected if selected is not None else duration
        journal.record(
            "projection_completed", slot=self.slot, host=new_host, source=key, path=path,
            selection_ack_seconds=selection_duration, transport_reply_seconds=duration,
            revalidate_switch_seconds=self.switch_duration)

    def clear(self, reason="clear"):
        for host in list(self.attachments):
            self.remove_host(host, reason)
        self.source = self.host = ""

    def release(self, key):
        if self.source != key:
            return
        self.remove_host(self.host, "clear")
        self.source = self.host = ""

    def remove_window(self, window):
        try:
            self.ui.command(["kill-window", "-t", window])
        except EXPECTED as error:
            try:
                windows = self.ui.command(
                    ["list-windows", "-a", "-F", "#{window_id}"])
            except EXPECTED:
                raise error
            if window in windows:
                raise error

    def remove_host(self, host, reason, exit_status=None):
        entry = self.attachments[host]
        if entry.remote_file:
            try:
                self.reclaim_marker(host, entry.owner)
            except RuntimeError as error:
                if "disconnected; refusing cleanup" not in str(error):
                    raise
        self.remove_window(entry.window)
        if exit_status is not None:
            status, signal = exit_status
            journal.record("attachment_exited", slot=self.slot, host=host,
                           window=entry.window, status=status, signal=signal)
        self.attachments.pop(host)
        journal.record("attachment_removed", slot=self.slot, host=host,
                       window=entry.window, reason=reason)

    def check(self):
        if not self.attachments:
            return ""
        windows = self.ui_windows()
        for host, entry in list(self.attachments.items()):
            master_dead = entry.master is not None and not process_alive(entry.master)
            dead = entry.window not in windows
            if not dead:
                dead = self.ui_value(entry.window, "#{pane_dead}") == "1"
            if master_dead or dead:
                status = signal = ""
                if dead:
                    try:
                        status, signal = self.ui_value(
                            entry.window, "#{pane_dead_status}\t#{pane_dead_signal}").split("\t")
                    except (RuntimeError, ValueError):
                        pass
                self.remove_host(host, "exited", (status, signal))
                if host == self.host:
                    self.source = self.host = ""
                    return "Viewer attachment exited unexpectedly"
        return ""

    def shutdown(self):
        self.clear("shutdown")
        if self.ui_process and self.ui_process.poll() is None:
            self.ui_process.terminate()
            self.ui_process.wait()
        if self.daemon_socket:
            self.cancel_daemon_forward()


def focus_projected(state, slot, key):
    if state.source != key:
        raise RuntimeError(f"Main projects {state.source or 'nothing'}, not {key}")
    focus(slot, state.workstation)


def project(state, key, selected=None, host=None):
    if state.source != key:
        if host is None:
            state.open(key, selected)
        else:
            state.open(key, selected, host)


def activate(state, slot, key, selected=None, host=None):
    if host is None:
        state.open(key, selected)
    else:
        state.open(key, selected, host)
    try:
        focus(slot, state.workstation)
    except ViewerFailure as error:
        return error
    except EXPECTED as error:
        return ViewerFailure("focus", "workstation", error)
    return None


class ViewerWorker:
    """Own one Attachment and collapse cursor movement to its latest intent."""

    INTENTS = {"PROJECT", "FOCUS"}
    CHECK_INTERVAL = .25

    def __init__(self, state, slot):
        self.state = state
        self.slot = slot
        self.jobs = []
        self.condition = threading.Condition()
        self.stopped = False
        self.failure = None
        self.reported = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @staticmethod
    def job(kind, key="", selected=None):
        return SimpleNamespace(kind=kind, key=key, selected=selected,
                               host="", source="", event=threading.Event(),
                               value=None, error=None)

    def intent(self, kind, key, selected=None):
        job = self.job(kind, key, selected)
        with self.condition:
            if self.failure:
                raise self.failure
            if self.stopped:
                raise RuntimeError("viewer worker has stopped")
            self.jobs = [pending for pending in self.jobs
                         if pending.kind not in self.INTENTS]
            self.jobs.append(job)
            self.condition.notify()

    def barrier(self, kind, key="", selected=None):
        job = self.job(kind, key, selected)
        with self.condition:
            if self.failure:
                raise self.failure
            if self.stopped:
                raise RuntimeError("viewer worker has stopped")
            if kind in {"OPEN", "SHUTDOWN"}:
                self.jobs = [pending for pending in self.jobs
                             if pending.kind not in self.INTENTS]
            elif kind == "CLEAR":
                self.jobs = [pending for pending in self.jobs
                             if not (pending.kind in self.INTENTS
                                     and (not key or pending.key == key))]
            self.jobs.append(job)
            self.condition.notify()
        job.event.wait()
        if job.error:
            raise job.error
        return job.value

    def perform(self, job):
        if job.kind in {"PROJECT", "FOCUS", "OPEN"}:
            job.host = source_host(job.key)
            job.source = job.key
        if job.kind == "PROJECT":
            project(self.state, job.key, job.selected, job.host)
        elif job.kind == "FOCUS":
            if self.state.source != job.key:
                self.state.open(job.key, job.selected, job.host)
            focus(self.slot, self.state.workstation)
        elif job.kind == "OPEN":
            error = activate(
                self.state, self.slot, job.key, job.selected, job.host)
            if error:
                self.record_failure("viewer_operation_failed", job, error)
                viewer_error(f"Focus failed: {error}")
                self.reported = True
        elif job.kind == "CLEAR":
            if job.key:
                self.state.release(job.key)
            else:
                self.state.clear()
        elif job.kind == "SOURCE":
            job.value = self.state.source
        elif job.kind == "STATUS":
            if error := self.state.check():
                viewer_error(error)
                self.reported = True
            job.value = self.state.source
        elif job.kind == "WORKSTATION":
            if not SLOT.fullmatch(job.key):
                raise ValueError(f"invalid workstation {job.key!r}")
            self.state.workstation = job.key
        elif job.kind == "SHUTDOWN":
            self.state.shutdown()
            self.stopped = True
        elif job.kind == "CHECK":
            if error := self.state.check():
                viewer_error(error)
                self.reported = True
        else:
            raise ValueError(f"unknown viewer operation {job.kind!r}")

    def record_failure(self, event, job, error):
        stage = error.stage if isinstance(error, ViewerFailure) else "worker"
        cause = error.cause if isinstance(error, ViewerFailure) else "unexpected"
        error_type = (error.error_type if isinstance(error, ViewerFailure)
                      else type(error).__name__)
        source = job.source or self.state.source
        source_key = job.host if job.source else self.state.host
        journal.record(event, slot=self.slot, operation=job.kind,
                       host=key_host(source_key), source=source,
                       stage=stage, cause=cause, error_type=error_type)

    def execute(self, job):
        if self.reported and job.kind not in {"CHECK", "STATUS"}:
            viewer_error("")
            self.reported = False
        try:
            self.perform(job)
        except EXPECTED as error:
            self.record_failure("viewer_operation_failed", job, error)
            viewer_error(f"Open failed: {error}")
            self.reported = True
            job.error = error
        except BaseException as error:
            job.error = RuntimeError(f"viewer worker failed: {error}")
            raise
        finally:
            job.event.set()

    def run(self):
        job = self.job("CHECK")
        next_check = time.monotonic()
        try:
            while not self.stopped:
                with self.condition:
                    remaining = next_check - time.monotonic()
                    if not self.jobs and remaining > 0:
                        self.condition.wait(remaining)
                    if time.monotonic() >= next_check:
                        job = self.job("CHECK")
                    elif self.jobs:
                        job = self.jobs.pop(0)
                    else:
                        continue
                self.execute(job)
                if job.kind == "CHECK":
                    next_check = time.monotonic() + self.CHECK_INTERVAL
        except BaseException as error:
            self.record_failure("viewer_controller_failed", job, error)
            failure = RuntimeError(f"viewer worker failed: {error}")
            with self.condition:
                self.failure = failure
                self.stopped = True
                pending, self.jobs = self.jobs, []
            for job in pending:
                job.error = failure
                job.event.set()
            viewer_error(str(failure))

    def close(self):
        if not self.stopped and not self.failure:
            try:
                self.barrier("SHUTDOWN")
            except EXPECTED:
                with self.condition:
                    self.stopped = True
                    self.condition.notify()
        self.thread.join()


def serve(slot):
    check_slot(slot)
    tty = os.ttyname(0)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = RUNTIME / f"viewer-{slot}.sock"
    path.unlink(missing_ok=True)
    state = Attachment(slot, tty)
    worker = ViewerWorker(state, slot)
    viewer_error("")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path)); os.chmod(path, 0o600); server.listen(); server.settimeout(.25)
        journal.record("viewer_ready", slot=slot, tty=tty)
        try:
            while True:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    def respond(value):
                        try:
                            connection.sendall(value)
                        except (BrokenPipeError, ConnectionResetError):
                            pass

                    message = connection.makefile().readline().strip()
                    if message == "SOURCE":
                        try:
                            value = worker.barrier("SOURCE")
                        except EXPECTED as error:
                            respond((f"ERROR {error}\n").encode())
                        else:
                            respond((value + "\n").encode())
                        continue
                    if message == "STATUS":
                        try:
                            value = worker.barrier("STATUS")
                        except EXPECTED as error:
                            respond((f"ERROR {error}\n").encode())
                        else:
                            respond((value + "\n").encode())
                        continue
                    try:
                        if message == "CLEAR":
                            worker.barrier("CLEAR")
                        elif message.startswith("CLEAR "):
                            worker.barrier("CLEAR", message.removeprefix("CLEAR "))
                        elif message.startswith("WORKSTATION "):
                            worker.barrier(
                                "WORKSTATION", message.removeprefix("WORKSTATION "))
                        elif message == "SHUTDOWN":
                            try:
                                worker.barrier("SHUTDOWN")
                            except EXPECTED as error:
                                viewer_error(f"Open failed: {error}")
                                respond((f"ERROR {error}\n").encode())
                                continue
                            path.unlink(missing_ok=True)
                            respond(b"OK\n")
                            return
                        elif message.startswith("FOCUS "):
                            worker.intent("FOCUS", message.removeprefix("FOCUS "))
                        elif message.startswith("PROJECT "):
                            values = message.removeprefix("PROJECT ").split(" ", 1)
                            key = values[0]
                            selected = values[1] if len(values) == 2 else ""
                            worker.intent(
                                "PROJECT", key, float(selected) if selected else None)
                        elif message.startswith("OPEN "):
                            values = message.removeprefix("OPEN ").split(" ", 1)
                            key = values[0]
                            selected = values[1] if len(values) == 2 else ""
                            worker.barrier(
                                "OPEN", key, float(selected) if selected else None)
                        else:
                            raise ValueError(f"unknown viewer request {message!r}")
                    except EXPECTED as error:
                        viewer_error(f"Open failed: {error}")
                        respond((f"ERROR {error}\n").encode())
                    else:
                        respond(b"OK\n")
        finally:
            journal.record("viewer_stopping", slot=slot, tty=tty)
            worker.close()
            path.unlink(missing_ok=True)
