import os
import json
import re
import shlex
import signal
import socket
import subprocess
import syslog
import time
from pathlib import Path
from types import SimpleNamespace

from .config import HUB, RUNTIME, ssh_environment
from .daemon import request as daemon_request
from .model import key_host
from . import alan, presentation, workstation
from .tmux import client_ready, split_key, switch_session


SLOT = re.compile(r"^[A-Za-z0-9_-]+$")
SWITCH = "/usr/lib/agent-fleet/fleet-switch"
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


def focus(slot):
    if slot == "main":
        result = subprocess.run(
            ["/usr/bin/tmux", "-N", "show-options", "-qv", "-t", "fleet@muster",
             "@fleet_workstation"], text=True, capture_output=True, check=True)
        name = result.stdout.strip()
        if not name:
            raise RuntimeError("Muster has no attached workstation")
        workstation.request(name, {"operation": "focus", "slot": "main"})
    else:
        subprocess.run(["i3-msg", f'[instance="fleet-{slot}"] focus'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


def stop_child(child, sig=signal.SIGHUP):
    if not child or child.poll() is not None:
        return
    os.killpg(child.pid, sig)
    if sig == signal.SIGSTOP:
        return
    try:
        child.wait(timeout=.1)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


class Attachment:
    def __init__(self, slot, tty):
        self.slot = slot
        self.tty = tty
        self.source = ""
        self.host = ""
        self.child = None
        self.master = None
        self.remote_tty = None
        self.remote_file = None
        self.daemon_socket = None
        self.daemon_master = None
        self.switch_duration = 0.0

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

    def wait_local(self, expected, child, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("tmux attachment exited before becoming ready")
            if client_ready(expected, self.tty):
                return
            time.sleep(.02)
        raise RuntimeError("tmux did not report the viewer attachment")

    def start_local(self, key):
        began = time.monotonic()
        expected = self.resolve(key)
        environment = {name: value for name, value in ssh_environment().items()
                       if name not in {"TMUX", "TMUX_PANE"}}
        child = subprocess.Popen(["/usr/bin/tmux", "-N", "attach-session", "-t", expected[3]],
                                 env=environment, start_new_session=True)
        try:
            self.wait_local(expected, child)
        except Exception:
            stop_child(child)
            raise
        self.switch_duration = time.monotonic() - began
        return child

    def switch_local(self, key):
        expected = self.resolve(key)
        self.switch_duration = switch_session(*expected, self.tty)

    def start_remote(self, host, key):
        environment = ssh_environment()
        child = None
        try:
            master = self.ensure_master(host)
            expected = self.resolve(key, remote=True)
            owner = os.uname().nodename.split(".", 1)[0]
            remote_file = (f"${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}/agent-fleet/"
                           f"viewer-{owner}-{self.slot}.tty")
            self.ssh(host, f"rm -f {remote_file}")
            body = (f"mkdir -p \"$(dirname {remote_file})\"; chmod 700 \"$(dirname {remote_file})\"; "
                    f"tty > {remote_file}; exec /usr/lib/agent-fleet/fleet-tmux attach-session "
                    f"-t {shlex.quote(expected[3])}")
            child = subprocess.Popen(["ssh", "-tt", "-o", "BatchMode=yes", host, body],
                                     env=environment, start_new_session=True)
            deadline = time.monotonic() + 3
            remote_tty = ""
            while time.monotonic() < deadline and child.poll() is None:
                result = self.ssh(host, f"cat {remote_file} 2>/dev/null || :", capture=True)
                remote_tty = result.stdout.strip()
                if remote_tty:
                    check = self.ssh(
                        host, shlex.join([SWITCH, *map(str, expected), remote_tty]),
                        capture=True, check=False)
                    if check.returncode:
                        if "can't find client" in check.stderr:
                            time.sleep(.02)
                            continue
                        check.check_returncode()
                    self.switch_duration = float(check.stdout.strip())
                    break
                time.sleep(.02)
            else:
                raise RuntimeError("remote tmux did not report the viewer attachment")
            self.remote_tty = remote_tty
            return child, master, remote_file
        except Exception:
            stop_child(child)
            if 'remote_file' in locals():
                subprocess.run(["ssh", "-o", "BatchMode=yes", host, f"rm -f {remote_file}"],
                               env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise

    def close_pair(self, child, master_host, remote_file=None):
        stop_child(child)
        if remote_file:
            subprocess.run(["ssh", "-o", "BatchMode=yes", master_host,
                            f"rm -f {remote_file}"], env=ssh_environment(),
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

    def suspend(self):
        local = os.uname().nodename.split(".", 1)[0]
        if self.host == local:
            subprocess.run(["/usr/bin/tmux", "-N", "suspend-client", "-t", self.tty],
                           check=True)
        else:
            self.ssh(self.host, shlex.join(
                ["/usr/bin/tmux", "-N", "suspend-client", "-t", self.remote_tty]))

    def resume(self):
        local = os.uname().nodename.split(".", 1)[0]
        if self.host == local:
            os.killpg(self.child.pid, signal.SIGCONT)
        else:
            result = self.ssh(
                self.host,
                shlex.join(["/usr/bin/tmux", "-N", "display-message", "-p",
                            "-c", self.remote_tty, "#{client_pid}"]), capture=True)
            self.ssh(self.host, shlex.join(["/usr/bin/kill", "-CONT",
                                            str(int(result.stdout.strip()))]))

    def open(self, key, selected=None):
        started = time.monotonic()
        new_host = key_host(key) if key else ""
        local = os.uname().nodename.split(".", 1)[0]
        if self.master and not process_alive(self.master):
            self.abandon()
            self.source = self.host = ""
            raise RuntimeError("current viewer SSH master exited unexpectedly")
        if self.child and self.child.poll() is not None:
            self.close()
            self.source = self.host = ""
            raise RuntimeError("current viewer attachment exited unexpectedly")
        if self.source and new_host == self.host:
            if new_host == local:
                self.switch_local(key)
            else:
                expected = self.resolve(key, remote=True)
                result = self.ssh(new_host,
                                  shlex.join([SWITCH, *map(str, expected), self.remote_tty]),
                                  capture=True)
                self.switch_duration = float(result.stdout.strip())
        elif new_host == local:
            old = (self.child, self.host, self.remote_file)
            if self.child:
                self.suspend()
            try:
                candidate = self.start_local(key)
            except Exception:
                if old[0]:
                    self.resume()
                raise
            self.close_pair(*old)
            self.child, self.master = candidate, None
            self.remote_tty = self.remote_file = None
        else:
            old = (self.child, self.host, self.remote_file)
            if self.child:
                self.suspend()
            try:
                candidate, master, remote_file = self.start_remote(new_host, key)
            except Exception:
                if old[0]:
                    self.resume()
                raise
            self.close_pair(*old)
            self.child, self.master = candidate, master
            self.remote_file = remote_file
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
        self.close()
        self.source = self.host = ""

    def close(self):
        self.close_pair(self.child, self.host, self.remote_file)
        self.child = self.master = self.remote_tty = self.remote_file = None

    def abandon(self):
        stop_child(self.child)
        self.child = self.master = self.remote_tty = self.remote_file = None

    def check(self):
        if self.master and not process_alive(self.master):
            self.abandon(); self.source = self.host = ""
            return "Viewer SSH master exited unexpectedly"
        if self.child and self.child.poll() is not None:
            self.close(); self.source = self.host = ""
            return "Viewer attachment exited unexpectedly"
        return ""

    def shutdown(self):
        self.close()
        if self.daemon_socket:
            self.cancel_daemon_forward()


def activate(state, slot, key, selected=None):
    state.open(key, selected)
    try:
        focus(slot)
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
            state.shutdown(); path.unlink(missing_ok=True)
