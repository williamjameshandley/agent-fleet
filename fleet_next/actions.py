import os
import subprocess
import shlex
import json
import time

from .config import hosts
from .remote import find
from .daemon import preview as pane_preview, snapshot
from .protocol import decode
from .protocol import decode_message
from . import viewer
from .agent import LEGACY


def host_command(host, *command, capture_output=False):
    argv = list(command) if host == os.uname().nodename else [
        "ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(command)]
    return subprocess.run(argv, text=True, check=True, capture_output=capture_output)


def legacy(host):
    return str(LEGACY) if host == os.uname().nodename else "/usr/lib/agent-fleet/fleet-legacy"


def choose(values, prompt):
    result = subprocess.run(["fzf", "--prompt", prompt], input="\n".join(values) + "\n",
                            text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def create():
    host = choose(hosts(), "host> ")
    agent = choose(["claude", "codex", "shell"], "agent> ")
    name = input("session name: ").strip()
    cwd = input("directory [home]: ").strip()
    if not name:
        raise SystemExit("session name is required")
    command = os.environ.get("SHELL", "/bin/sh") if agent == "shell" else agent
    arguments = ["tmux", "new-session", "-d", "-s", name]
    if cwd:
        arguments.extend(("-c", cwd))
    host_command(host, *arguments, command)


def rename(key):
    session = find(key)
    name = input(f"rename {session.name} to: ").strip()
    if name:
        host_command(session.ref.server.host, "fleet-next", "mutate", key, "rename", name)


def done(key):
    session = find(key)
    for slot, source in viewer.slots():
        if source == key:
            viewer.request(slot, "")
    host_command(session.ref.server.host, "fleet-next", "mutate", key,
                 "attention", "done")
    host_command(session.ref.server.host, "fleet-next", "signal")


def dismiss_source(key):
    shown = [slot for slot, source in viewer.slots() if source == key]
    if not shown:
        raise SystemExit("that source is not shown locally")
    for slot in shown:
        viewer.request(slot, "")
    subprocess.run(["tmux", "display-message", "-t", "=fleet@muster",
                    "Viewer dismissed; source session is still running"])


def preview(key, columns=0, lines=0):
    print(pane_preview(key, columns, lines), end="")


def history():
    live = {(session.ref.server.host, session.agent, session.transcript_id)
            for session in decode(snapshot()) if session.transcript_id}
    rows = []
    for host in hosts():
        result = host_command(host, legacy(host), "_history", "-n", "100", capture_output=True)
        for item in json.loads(result.stdout):
            if (host, item["agent"], item["session_id"]) not in live:
                rows.append((item["mtime"], host, item))
    for _, host, item in sorted(rows, reverse=True):
        key = f'{host}:{item["agent"]}:{item["session_id"]}'
        print("\t".join((key, host, item["agent"], item["name"], item["cwd"])))


def resurrect(key):
    host, agent, transcript = key.split(":", 2)
    if any((session.ref.server.host, session.agent, session.transcript_id) ==
           (host, agent, transcript) for session in decode(snapshot())):
        raise SystemExit("that transcript already has a live session")
    name = input("new session name: ").strip()
    if not name:
        raise SystemExit("session name is required")
    host_command(host, legacy(host), "_resurrect", agent, transcript, name)


def arrive(profile, available=False):
    sessions, _, unavailable = decode_message(snapshot())
    if unavailable and not available:
        raise SystemExit("inventory incomplete; unavailable: " + " ".join(unavailable))
    result = subprocess.run(["tmux", "show-options", "-v", "-t", "=fleet@muster",
                             "@fleet_profile"], text=True, capture_output=True)
    current = result.stdout.strip() if result.returncode == 0 else ""
    if current == profile:
        return
    epoch = str(time.time_ns())
    subprocess.run(["tmux", "set-option", "-t", "=fleet@muster", "@fleet_profile", profile],
                   check=True)
    subprocess.run(["tmux", "set-option", "-t", "=fleet@muster", "@fleet_epoch", epoch],
                   check=True)
    placements = viewer.slots()
    free = [slot for slot, source in placements if not source]
    shown = {source for _, source in placements if source}
    ranked = sorted((session for session in sessions
                     if session.attention != "done" and session.windows == 1
                     and session.ref.key not in shown),
                    key=lambda session: ({"needs-action": 0, "working": 1,
                                          "waiting": 2, "finished": 3}.get(session.state, 2),
                                         -(session.recency or session.activity)))
    for slot, session in zip(free, ranked):
        viewer.request(slot, session.ref.key)


def focused_slot():
    result = subprocess.run(["i3-msg", "-t", "get_tree"], text=True,
                            capture_output=True, check=True)
    tree = json.loads(result.stdout)
    while tree.get("focus"):
        wanted = tree["focus"][0]
        tree = next(node for node in tree.get("nodes", []) + tree.get("floating_nodes", [])
                    if node["id"] == wanted)
    instance = tree.get("window_properties", {}).get("instance", "")
    if not instance.startswith("fleet-") or instance in {"fleet-muster", "fleet-commander"}:
        raise SystemExit("the focused window is not a Fleet viewer")
    return instance.removeprefix("fleet-")


def context():
    sessions, _, unavailable = decode_message(snapshot())
    profile = subprocess.run(["tmux", "show-options", "-v", "-t", "=fleet@muster",
                              "@fleet_profile"], text=True, capture_output=True).stdout.strip()
    data = {
        "profile": profile,
        "unavailable": unavailable,
        "slots": [{"slot": slot, "source": source} for slot, source in viewer.slots()],
        "sessions": [{"source": s.ref.key, "host": s.ref.server.host, "name": s.name,
                      "agent": s.agent, "state": s.state, "attention": s.attention,
                      "summary": s.summary, "recency": s.recency or s.activity}
                     for s in sessions],
    }
    print(json.dumps(data, indent=2))
