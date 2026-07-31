import os
import selectors
import shlex
import signal
import socket
import subprocess
import re

from .config import RUNTIME, ssh_environment
from .tmux import inventory
from .remote import find
from .model import key_host
from . import alan, presentation, workstation


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
    if key and slot == "main":
        focus_main()
        return
    if key:
        subprocess.run(["i3-msg", f'[instance="fleet-{slot}"] focus'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def focus_main():
    result = subprocess.run(
        ["tmux", "show-options", "-qv", "-t", "fleet@muster", "@fleet_workstation"],
        text=True, capture_output=True, check=True)
    name = result.stdout.strip()
    if not name:
        raise RuntimeError("Muster has no attached workstation")
    workstation.request(name, {"operation": "focus", "slot": "main"})


def slots():
    found = []
    for path in sorted(RUNTIME.glob("viewer-*.sock")):
        slot = path.name.removeprefix("viewer-").removesuffix(".sock")
        try:
            found.append((slot, exchange(slot, "STATUS")))
        except SystemExit:
            continue
    return found


def open_main(key):
    request("main", key)
    from .ui import select
    select()


def show(key, slot=None):
    session = find(key)
    available = slots()
    for name, source in available:
        if source == key:
            if name == "main":
                open_main(key)
            else:
                request(name, key)
            return
    if slot:
        if slot == "main":
            open_main(key)
        else:
            request(slot, key)
        return
    if len(available) == 1 and available[0][0] == "main":
        open_main(key)
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
    attach = ["fleet", "attach", key]
    return attach if host == local else ["ssh", "-tt", "-o", "BatchMode=yes", host,
                                         shlex.join(attach)]


def stop_child(child):
    if not child or child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGHUP)
    try:
        child.wait(timeout=.1)
        return
    except subprocess.TimeoutExpired:
        pass
    os.killpg(child.pid, signal.SIGKILL)
    child.wait()


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
                    stop_child(child)
                source = key
                try:
                    environment = {name: value for name, value in ssh_environment().items()
                                   if name not in {"TMUX", "TMUX_PANE"}}
                    child = subprocess.Popen(command(key), env=environment,
                                             start_new_session=True) if key else None
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
            stop_child(child)
        path.unlink(missing_ok=True)


def attach(key):
    if key.startswith("alan:"):
        actor = key.removeprefix("alan:")
        host = key_host(key)
        if host != os.uname().nodename:
            raise SystemExit(f"identity is for {host}, not {os.uname().nodename}")
        [descriptor] = [item for item in alan.actors() if item["addr"] == actor]
        if descriptor["kind"] == "python":
            connection_file = alan.native_dir(actor) / "kernel.json"
            presentation.python_console(actor, connection_file)
            return
        os.execvp("fleet", ["fleet", "actor-view", actor])
        return
    host = key_host(key)
    if host != os.uname().nodename:
        raise SystemExit(f"identity is for {host}, not {os.uname().nodename}")
    current = [s for s in inventory(host) if s.ref.key == key]
    if len(current) != 1:
        raise SystemExit(f"session identity changed: {key}")
    if current[0].agent == "codex":
        subprocess.run(["tmux", "set-option", "-t", current[0].ref.session_id,
                        "mouse", "on"], check=True)
    os.execvp("tmux", ["tmux", "attach-session", "-t", current[0].ref.session_id])
