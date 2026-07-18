import json
import os
import shlex
import subprocess

from fleet_next import viewer
from fleet_next.remote import find

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
    key = viewer.exchange(instance.removeprefix("fleet-"), "STATUS")
    if not key:
        return None
    session = find(key)
    pane = _active_pane(session.ref.server.host, session.ref.session_id)
    return Destination(
        key=key,
        host=session.ref.server.host,
        session_id=session.ref.session_id,
        pane_id=pane,
        label=f"{session.ref.server.host} › {session.name} › {pane}",
        window_id=node["window"],
    )


def _active_pane(host, session_id):
    command = ["tmux", "display-message", "-p", "-t", session_id, "#{pane_id}"]
    if host != os.uname().nodename:
        command = ["ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(command)]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()
