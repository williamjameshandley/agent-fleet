import os
import selectors
import shlex
import signal
import socket
import subprocess
import re

from .config import HUB, RUNTIME, ssh_environment
from .tmux import inventory
from .remote import find
from .model import key_host


SLOT = re.compile(r"^[A-Za-z0-9_-]+$")


def check_slot(slot):
    if not SLOT.fullmatch(slot):
        raise SystemExit(f"invalid viewer slot {slot!r}")
    return slot


def exchange(slot, message):
    check_slot(slot)
    path = RUNTIME / f"viewer-{slot}.sock"
    with socket.socket(socket.AF_UNIX) as client:
        try:
            client.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            raise SystemExit(f"viewer slot {slot!r} is not running")
        client.sendall((message + "\n").encode())
        reply = client.makefile().readline().strip()
        if message != "STATUS" and reply != "OK":
            raise SystemExit(reply or f"viewer {slot!r} did not acknowledge")
        return reply


def request(slot, key):
    exchange(slot, f"OPEN {key}" if key else "CLEAR")
    if key and slot == "main" and os.uname().nodename.split(".", 1)[0] == HUB:
        result = subprocess.run(["tmux", "show-options", "-qv", "-t", "fleet@muster",
                                 "@fleet_workstation"], text=True,
                                capture_output=True, check=True)
        workstation = result.stdout.strip()
        if workstation:
            focus = shlex.join(("env", "DISPLAY=:0", "i3-msg",
                                '[instance="fleet-main"] focus'))
            subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", workstation,
                            focus], check=True, env=ssh_environment(),
                           stdout=subprocess.DEVNULL)
        return
    if key:
        subprocess.run(["i3-msg", f'[instance="fleet-{slot}"] focus'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def slots():
    found = []
    for path in sorted(RUNTIME.glob("viewer-*.sock")):
        slot = path.name.removeprefix("viewer-").removesuffix(".sock")
        try:
            found.append((slot, exchange(slot, "STATUS")))
        except SystemExit:
            continue
    return found


def show(key, slot=None):
    session = find(key)
    if session.attention == "done":
        host = session.ref.server.host
        command = shlex.join(("fleet-next", "mutate", key, "attention", "tracked"))
        argv = (["fleet-next", "mutate", key, "attention", "tracked"]
                if host == os.uname().nodename else
                ["ssh", "-T", "-o", "BatchMode=yes", host, command])
        subprocess.run(argv, check=True)
        signal = (["fleet-next", "signal"] if host == os.uname().nodename else
                  ["ssh", "-T", "-o", "BatchMode=yes", host, "fleet-next signal"])
        subprocess.run(signal, check=True)
    available = slots()
    for name, source in available:
        if source == key:
            request(name, key)
            return
    if slot:
        request(slot, key)
        return
    if len(available) == 1 and available[0][0] == "main":
        request("main", key)
        return
    for name, source in available:
        if not source:
            request(name, key)
            return
    subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                    "All viewer slots are occupied; choose a slot explicitly"])


def command(key):
    host = key_host(key)
    local = os.uname().nodename
    attach = ["fleet-next", "attach", key]
    return attach if host == local else ["ssh", "-tt", "-o", "BatchMode=yes", host,
                                         shlex.join(attach)]


def serve(slot):
    check_slot(slot)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = RUNTIME / f"viewer-{slot}.sock"
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen()
    selector = selectors.DefaultSelector()
    selector.register(server, selectors.EVENT_READ)
    child = None
    source = ""
    try:
        while True:
            for selected, _ in selector.select(timeout=.5):
                connection, _ = selected.fileobj.accept()
                message = connection.makefile().readline().strip()
                if message == "STATUS":
                    connection.sendall((source + "\n").encode())
                    connection.close()
                    continue
                if message == "CLEAR":
                    verb, key = "OPEN", ""
                else:
                    verb, key = message.split(" ", 1)
                if verb != "OPEN":
                    raise ValueError(f"unknown viewer request {verb!r}")
                if child and child.poll() is None:
                    child.send_signal(signal.SIGHUP)
                    child.wait()
                source = key
                try:
                    environment = {name: value for name, value in ssh_environment().items()
                                   if name not in {"TMUX", "TMUX_PANE"}}
                    child = subprocess.Popen(command(key), env=environment) if key else None
                except OSError as error:
                    connection.sendall((f"ERROR {error}\n").encode())
                    connection.close()
                    source = ""
                    continue
                connection.sendall(b"OK\n")
                connection.close()
            if child and child.poll() is not None:
                child.wait()
                child = None
                source = ""
    finally:
        if child and child.poll() is None:
            child.send_signal(signal.SIGHUP)
            child.wait()
        path.unlink(missing_ok=True)


def attach(key):
    session = find(key)
    if session.ref.server.kind == "alan":
        attachment = session.attachment or {}
        if attachment.get("kind") == "jupyter":
            os.execvp("jupyter", ["jupyter", "console", "--existing",
                                   attachment["connection_file"]])
            return
        if attachment.get("kind") == "codex":
            os.execvp("codex", ["codex", "resume", "--remote",
                                "unix://" + attachment["socket"],
                                attachment["thread_id"]])
            return
        raise SystemExit(f"actor {session.ref.session_id} has no supported attachment")
    host = session.ref.server.host
    current = [s for s in inventory(host) if s.ref.key == key]
    if len(current) != 1:
        raise SystemExit(f"session identity changed: {key}")
    os.execvp("tmux", ["tmux", "attach-session", "-t", current[0].ref.session_id])
