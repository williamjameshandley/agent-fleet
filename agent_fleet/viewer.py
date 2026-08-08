import os
import json
import re
import shlex
import socket
import subprocess
import syslog
import time
import queue
from pathlib import Path
from types import SimpleNamespace

from .config import HUB, RUNTIME, ssh_environment
from .daemon import request as daemon_request
from .model import key_host
from . import alan, presentation, workstation
from .tmux import ControlClient, split_key


SLOT = re.compile(r"^[A-Za-z0-9_-]+$")
EXPECTED = (LookupError, OSError, RuntimeError, ValueError,
            subprocess.CalledProcessError)


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
    if not name:
        raise RuntimeError("Muster has no attached workstation")
    workstation.request(name, {"operation": "focus", "slot": slot})


def viewer_error(value):
    command = (["/usr/bin/tmux", "-N", "set-option", "-t", "=fleet@muster:",
                "@fleet_viewer_error", value] if value else
               ["/usr/bin/tmux", "-N", "set-option", "-u", "-t", "=fleet@muster:",
                "@fleet_viewer_error"])
    subprocess.run(command, check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path = RUNTIME / "muster.sock"
    if path.exists():
        subprocess.run(["curl", "-fsS", "--max-time", "2", "--unix-socket", str(path),
                        "-XPOST", "-d", "transform-header(/usr/lib/agent-fleet/ui header)",
                        "http://localhost"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


def request(slot, key):
    exchange(slot, f"OPEN {key}" if key else "CLEAR")


def slots():
    found = []
    for path in sorted(RUNTIME.glob("viewer-*.sock")):
        slot = path.name.removeprefix("viewer-").removesuffix(".sock")
        try:
            found.append((slot, exchange(slot, "STATUS")))
        except RuntimeError:
            continue
    return found


def open_main(key):
    request("main", key)
    from .ui import select
    select()


def show(key, slot=None):
    available = slots()
    selected = next((name for name, source in available if source == key), None)
    if selected:
        open_main(key) if selected == "main" else request(selected, key)
        return
    if slot:
        open_main(key) if slot == "main" else request(slot, key)
        return
    free = next((name for name, source in available if not source), None)
    if free:
        open_main(key) if free == "main" else request(free, key)
        return
    if len(available) == 1 and available[0][0] == "main":
        open_main(key)
        return
    subprocess.run(["/usr/bin/tmux", "-N", "display-message", "-t", "fleet@muster",
                    "All viewer slots are occupied; choose a slot explicitly"])


def process_identity(pid):
    fields = (Path(f"/proc/{pid}/stat")).read_text().split()
    return int(fields[21])


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
            ui.command(["refresh-client", "-f", "no-output"])
            self.workstation = attached_workstation()
        self.ui = ui

    def ssh(self, host, *arguments, capture=False, check=True):
        command = ["ssh", "-o", "BatchMode=yes", host, *arguments]
        return subprocess.run(command, text=True, capture_output=capture, check=check,
                              env=ssh_environment())

    def master_identity(self, host):
        checked = subprocess.run(
            ["ssh", "-O", "check", host],
            text=True, capture_output=True, check=True, env=ssh_environment())
        match = re.search(r"pid=(\d+)", checked.stdout + checked.stderr)
        if not match:
            raise RuntimeError("SSH did not report its master identity")
        pid = int(match.group(1))
        return pid, process_identity(pid)

    def master_policy(self, host):
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
                subprocess.run(
                    ["ssh", "-O", "forward", "-o", "StreamLocalBindUnlink=yes",
                     "-L", specification, HUB], check=True, env=ssh_environment())
                value = self.socket_request(forwarded, message)
            except Exception:
                self.cancel_daemon_forward()
                raise
            self.daemon_socket = forwarded
            return value
        return self.socket_request(self.daemon_socket, message)

    def resident_switch(self, key, client):
        value = json.loads(self.daemon("switch " + json.dumps(
            {"key": key, "client": client}, separators=(",", ":"))))
        if set(value) == {"error"}:
            raise RuntimeError(value["error"])
        if set(value) != {"target", "duration"}:
            raise RuntimeError("invalid Fleet switch response")
        self.switch_duration = value["duration"]
        return tuple(value["target"])

    def reclaim_marker(self, host, owner):
        value = json.loads(self.daemon("cleanup " + json.dumps(
            {"host": host, "owner": owner, "slot": self.slot},
            separators=(",", ":"))))
        if set(value) == {"error"}:
            raise RuntimeError(value["error"])
        if value != {"ok": True}:
            raise RuntimeError("invalid Fleet cleanup response")

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
        output = self.ui.command(["display-message", "-p", "-t", window, value])
        if len(output) != 1:
            raise RuntimeError(f"UI server did not report {value}")
        return output[0]

    def create_host(self, host, key):
        local = os.uname().nodename.split(".", 1)[0]
        remote_file = None
        owner = ""
        master = None
        if host == local:
            expected = self.resolve(key)
            command = shlex.join(["env", "-u", "TMUX", "-u", "TMUX_PANE",
                                  "/usr/lib/agent-fleet/fleet-tmux", "attach-session",
                                  "-t", expected[3]])
        else:
            master = self.ensure_master(host)
            expected = self.resolve(key, remote=True)
            owner = local
            remote_file = (f"${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}/agent-fleet/"
                           f"viewer-{owner}-{self.slot}-{host}.tty")
            self.reclaim_marker(host, owner)
            body = (f"mkdir -p \"$(dirname {remote_file})\"; "
                    f"chmod 700 \"$(dirname {remote_file})\"; tty > {remote_file}; "
                    f"exec /usr/lib/agent-fleet/fleet-tmux attach-session "
                    f"-t {shlex.quote(expected[3])}")
            command = shlex.join(["ssh", "-tt", "-o", "BatchMode=yes", host, body])
        output = self.ui.command(["new-window", "-d", "-P", "-F", "#{window_id}",
                                  "-t", f"fleet@{self.slot}", "-n", host, command])
        if len(output) != 1:
            raise RuntimeError("UI server did not create one presentation window")
        window = output[0]
        try:
            if host == local:
                client = self.ui_value(window, "#{pane_tty}")
            else:
                deadline = time.monotonic() + 3
                client = ""
                while time.monotonic() < deadline:
                    result = self.ssh(host, f"cat {remote_file} 2>/dev/null || :",
                                      capture=True)
                    client = result.stdout.strip()
                    if client:
                        break
                    time.sleep(.02)
                if not client:
                    raise RuntimeError("remote tmux did not report the viewer attachment")
            self.prove_switch(key, client)
            return SimpleNamespace(host=host, window=window, client=client,
                                   source=key, remote_file=remote_file,
                                   owner=owner, master=master)
        except Exception:
            self.ui.command(["kill-window", "-t", window])
            if remote_file:
                try:
                    self.reclaim_marker(host, owner)
                except RuntimeError:
                    pass
            raise

    def select_host(self, entry):
        self.ui.command(["select-window", "-t", entry.window])

    def find(self, key):
        value = json.loads(self.daemon(f"resolve {key}"))
        if set(value) == {"error"}:
            raise RuntimeError(value["error"])
        if set(value) != {"agent", "state", "cwd"}:
            raise RuntimeError("invalid Fleet resolver response")
        return SimpleNamespace(**value)

    def resolve(self, key, remote=False):
        if not key.startswith("alan:"):
            _, socket_path, pid, started, sid = split_key(key)
            return socket_path, pid, started, sid
        session = self.find(key)
        actor = key.removeprefix("alan:")
        if session.state in {"retired", "unavailable"}:
            raise RuntimeError(f"Alan actor is {session.state}: {actor}")
        name = "fleet@alan-" + alan.runtime_name(actor)
        fmt = "#{q:socket_path} #{pid} #{start_time} #{q:session_id}"
        command = ["/usr/bin/tmux", "-N", "list-sessions", "-f",
                   f"#{{==:#{{session_name}},{name}}}", "-F", fmt]
        def locate():
            if remote:
                return self.ssh(key_host(key), shlex.join(command), capture=True)
            return subprocess.run(command, text=True, capture_output=True, check=True)
        result = locate()
        values = shlex.split(result.stdout.strip())
        if len(values) != 4 and session.agent not in {"claude", "codex"}:
            if remote:
                self.ssh(key_host(key), shlex.join([
                    "/usr/lib/agent-fleet/fleet-present", actor, session.agent, session.cwd]))
            else:
                presentation.target(actor, {"kind": session.agent, "cwd": session.cwd})
            values = shlex.split(locate().stdout.strip())
        if len(values) != 4:
            raise RuntimeError(f"{session.agent.capitalize()} evaluator terminal is unavailable: {actor}")
        return values[0], int(values[1]), int(values[2]), values[3]

    def open(self, key, selected=None):
        started = time.monotonic()
        new_host = key_host(key) if key else ""
        local = os.uname().nodename.split(".", 1)[0]
        entry = self.attachments.get(new_host)
        if entry is None:
            entry = self.create_host(new_host, key)
            self.attachments[new_host] = entry
            try:
                self.select_host(entry)
            except Exception:
                self.remove_host(new_host)
                raise
        elif new_host == self.host:
            self.resident_switch(key, entry.client)
            entry.source = key
        else:
            previous = entry.source
            self.resident_switch(key, entry.client)
            try:
                self.select_host(entry)
            except Exception:
                try:
                    self.resident_switch(previous, entry.client)
                except Exception:
                    self.remove_host(new_host)
                raise
            entry.source = key
        self.source, self.host = key, new_host
        duration = time.monotonic() - started
        acknowledged = time.clock_gettime(time.CLOCK_BOOTTIME)
        selection_duration = acknowledged - selected if selected is not None else duration
        syslog.syslog(syslog.LOG_INFO, f"fleet_viewer_switch slot={self.slot} source={key} "
                      f"route={'local' if new_host == local else 'remote'} "
                      f"selection_ack_duration={selection_duration:.6f} "
                      f"transport_reply_duration={duration:.6f} "
                      f"revalidate_switch_duration={self.switch_duration:.6f}")

    def clear(self):
        for host in list(self.attachments):
            self.remove_host(host)
        self.source = self.host = ""

    def remove_host(self, host, missing_ok=False):
        entry = self.attachments[host]
        if entry.remote_file:
            try:
                self.reclaim_marker(host, entry.owner)
            except RuntimeError as error:
                if "disconnected; refusing cleanup" not in str(error):
                    raise
        self.attachments.pop(host)
        try:
            self.ui.command(["kill-window", "-t", entry.window])
        except EXPECTED:
            if not missing_ok:
                raise

    def check(self):
        for host, entry in list(self.attachments.items()):
            master_dead = entry.master is not None and not process_alive(entry.master)
            try:
                dead = self.ui_value(entry.window, "#{pane_dead}") == "1"
            except RuntimeError:
                dead = True
            if master_dead or dead:
                self.remove_host(host, missing_ok=True)
                if host == self.host:
                    self.source = self.host = ""
                    return "Viewer attachment exited unexpectedly"
        return ""

    def shutdown(self):
        self.clear()
        if self.ui_process and self.ui_process.poll() is None:
            self.ui_process.terminate()
            self.ui_process.wait()
        if self.daemon_socket:
            self.cancel_daemon_forward()


def focus_projected(state, slot, key):
    if state.source != key:
        raise RuntimeError(f"Main projects {state.source or 'nothing'}, not {key}")
    focus(slot, state.workstation)


def activate(state, slot, key, selected=None):
    state.open(key, selected)
    try:
        focus(slot, state.workstation)
    except EXPECTED as error:
        return f"Focus failed: {error}"
    return ""


def serve(slot):
    check_slot(slot)
    tty = os.ttyname(0)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = RUNTIME / f"viewer-{slot}.sock"
    path.unlink(missing_ok=True)
    state = Attachment(slot, tty)
    viewer_error("")
    reported = False
    shut_down = False
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path)); os.chmod(path, 0o600); server.listen(); server.settimeout(.25)
        try:
            while True:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    error = state.check()
                    if error:
                        viewer_error(error)
                        reported = True
                    continue
                with connection:
                    message = connection.makefile().readline().strip()
                    if message == "STATUS":
                        error = state.check()
                        if error:
                            viewer_error(error)
                            reported = True
                        connection.sendall((state.source + "\n").encode()); continue
                    if reported:
                        viewer_error("")
                        reported = False
                    try:
                        if message == "CLEAR":
                            state.clear()
                        elif message.startswith("WORKSTATION "):
                            name = message.removeprefix("WORKSTATION ")
                            if not SLOT.fullmatch(name):
                                raise ValueError(f"invalid workstation {name!r}")
                            state.workstation = name
                        elif message == "SHUTDOWN":
                            state.shutdown()
                            shut_down = True
                            path.unlink(missing_ok=True)
                            connection.sendall(b"OK\n")
                            return
                        elif message.startswith("FOCUS "):
                            focus_projected(state, slot, message.removeprefix("FOCUS "))
                        elif message.startswith("PROJECT "):
                            values = message.removeprefix("PROJECT ").split(" ", 1)
                            key = values[0]
                            selected = values[1] if len(values) == 2 else ""
                            state.open(key, float(selected) if selected else None)
                        elif message.startswith("OPEN "):
                            values = message.removeprefix("OPEN ").split(" ", 1)
                            key = values[0]
                            selected = values[1] if len(values) == 2 else ""
                            error = activate(
                                state, slot, key, float(selected) if selected else None)
                            if error:
                                viewer_error(error)
                                reported = True
                        else:
                            raise ValueError(f"unknown viewer request {message!r}")
                    except EXPECTED as error:
                        viewer_error(f"Open failed: {error}")
                        reported = True
                        connection.sendall((f"ERROR {error}\n").encode())
                    else:
                        connection.sendall(b"OK\n")
        finally:
            if not shut_down:
                state.shutdown()
            path.unlink(missing_ok=True)
