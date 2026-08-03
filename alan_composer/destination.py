import json
import os
import shlex
import subprocess

from agent_fleet.config import HUB
from agent_fleet.remote import find
from agent_fleet import alan

from .model import Destination


def _focused(node):
    if node.get("focused"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = _focused(child)
        if found:
            return found


def capture():
    tree = json.loads(subprocess.run(
        ["i3-msg", "-t", "get_tree"], check=True, capture_output=True, text=True
    ).stdout)
    node = _focused(tree)
    properties = node.get("window_properties", {})
    instance = properties.get("instance", "")
    if not instance.startswith("fleet-") or instance in {"fleet-muster", "fleet-commander"}:
        return None
    slot = instance.removeprefix("fleet-")
    command = ["python", "-c",
               "import sys; from agent_fleet.viewer import exchange; print(exchange(sys.argv[1], 'STATUS'))",
               slot]
    if slot == "main" and os.uname().nodename.split(".", 1)[0] != HUB:
        command = _remote(HUB, command)
    status = subprocess.run(command, capture_output=True, text=True)
    key = status.stdout.strip() if status.returncode == 0 else ""
    if not key:
        return None
    session = find(key)
    pane = _active_pane(session)
    return Destination(
        key=key,
        host=session.ref.server.host,
        session_id=session.ref.session_id,
        pane_id=pane,
        label=f"{session.ref.server.host} › {session.name} › {pane}",
        window_id=node["window"],
    )


def revalidate(destination):
    session = find(destination.key)
    if (session.ref.server.host, session.ref.session_id) != (
            destination.host, destination.session_id):
        raise RuntimeError(f"stale destination identity: {destination.key}")
    pane = _active_pane(session)
    if pane != destination.pane_id:
        raise RuntimeError(f"destination pane changed: {destination.pane_id} -> {pane}")


def _active_pane(session):
    host = session.ref.server.host
    session_id = session.ref.session_id
    if (getattr(session.ref.server, "kind", "tmux") == "alan"
            and session.agent in {"claude", "codex"}):
        session_id = "=fleet@alan-" + alan.runtime_name(session_id) + ":"
    command = ["/usr/bin/tmux", "-N", "display-message", "-p", "-t", session_id,
               "#{pane_id}"]
    if host != os.uname().nodename:
        command = _remote(host, command)
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def _remote(host, command):
    return ["ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(command)]
