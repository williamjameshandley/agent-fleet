import os
import subprocess
import shlex
import json
import time
from pathlib import Path

from .config import hosts, ssh_environment
from .remote import find
from .daemon import preview as pane_preview, snapshot
from .protocol import decode
from .protocol import decode_message
from . import viewer
from .alan import spawn_codex, spawn_python, rename as alan_rename


def host_command(host, *command, capture_output=False):
    argv = list(command) if host == os.uname().nodename else [
        "ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(command)]
    return subprocess.run(argv, text=True, check=True, capture_output=capture_output)


def desktop_input(prompt, values=(), fixed=False):
    result = subprocess.run(
        ["tmux", "show-options", "-qv", "-t", "fleet@muster", "@fleet_workstation"],
        text=True, capture_output=True, check=True)
    workstation = result.stdout.strip()
    if not workstation:
        raise SystemExit("Muster has no attached workstation")
    command = ["env", "DISPLAY=:0", "rofi", "-dmenu", "-p", prompt,
               "-location", "2", "-theme", "rofi"]
    if fixed:
        command.extend(("-i", "-no-custom"))
    result = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", workstation, shlex.join(command)],
        input="\n".join(values) + "\n", text=True, capture_output=True)
    if result.returncode:
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def agent_command(agent, name):
    if agent == "claude":
        return ["claude", "--dangerously-skip-permissions", "--name", name]
    if agent == "codex":
        return ["codex", "--sandbox", "danger-full-access",
                "--ask-for-approval", "never"]
    return [os.environ.get("SHELL", "/bin/sh")]


def created_key(host, name):
    result = host_command(host, "fleet-next", "snapshot", "--host", host,
                          capture_output=True)
    matches = [session.ref.key for session in decode(result.stdout)
               if session.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"created session {host}:{name} did not resolve uniquely")
    return matches[0]


def create():
    host = desktop_input("host", hosts(), fixed=True)
    agent = desktop_input("agent", ("claude", "codex", "python", "shell"), fixed=True)
    name = desktop_input("session name")
    cwd = desktop_input("directory", (str(Path.home()),)) or str(Path.home())
    if not name:
        raise SystemExit("session name is required")
    if agent in ("python", "codex"):
        if host != os.uname().nodename:
            result = host_command(host, "fleet-next", "alan-spawn", agent, name, cwd,
                                  capture_output=True)
            key = f"alan:{host}:{result.stdout.strip()}"
        else:
            spawn = spawn_python if agent == "python" else spawn_codex
            key = f"alan:{host}:{spawn(name, cwd)}"
    else:
        host_command(host, "tmux", "new-session", "-d", "-s", name, "-c", cwd,
                     *agent_command(agent, name))
        key = created_key(host, name)
    viewer.request("main", key)


def rename(key):
    session = find(key)
    name = desktop_input(f"rename {session.name}")
    if name:
        if session.ref.server.kind == "alan":
            if session.ref.server.host == os.uname().nodename:
                alan_rename(session.ref.session_id, name)
            else:
                host_command(session.ref.server.host, "fleet-next", "alan-rename",
                             session.ref.session_id, name)
        else:
            host_command(session.ref.server.host, "fleet-next", "mutate", key, "rename", name)


def done(key):
    session = find(key)
    if session.ref.server.kind == "alan":
        raise SystemExit("Alan actor attention is not implemented yet")
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
    subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                    "Viewer dismissed; source session is still running"])


def preview(key, columns=0, lines=0):
    session = find(key)
    if session.ref.server.kind == "alan":
        print(f"{session.name}\n{session.agent} · {session.state}\n{session.cwd}")
    else:
        print(pane_preview(key, columns, lines), end="")


def history():
    live = {(session.ref.server.host, session.agent, session.transcript_id)
            for session in decode(snapshot()) if session.transcript_id}
    rows = []
    for host in hosts():
        result = host_command(host, "fleet-next", "transcripts", "--limit", "100",
                              capture_output=True)
        for item in json.loads(result.stdout):
            if (host, item["agent"], item["session_id"]) not in live:
                rows.append((item["mtime"], host, item))
    for _, host, item in sorted(rows, key=lambda row: row[0], reverse=True):
        key = f'{host}:{item["agent"]}:{item["session_id"]}'
        print("\t".join((key, host, item["agent"], item["name"], item["cwd"])))


def resurrect(key):
    host, agent, transcript = key.split(":", 2)
    if any((session.ref.server.host, session.agent, session.transcript_id) ==
           (host, agent, transcript) for session in decode(snapshot())):
        raise SystemExit("that transcript already has a live session")
    name = desktop_input("new session name")
    if not name:
        raise SystemExit("session name is required")
    host_command(host, "fleet-next", "resume", agent, transcript, name)
    viewer.request("main", created_key(host, name))


def arrive(profile, available=False):
    sessions, _, unavailable = decode_message(snapshot())
    if unavailable and not available:
        raise SystemExit("inventory incomplete; unavailable: " + " ".join(unavailable))
    result = subprocess.run(["tmux", "show-options", "-gv",
                             "@fleet_profile"], text=True, capture_output=True)
    current = result.stdout.strip() if result.returncode == 0 else ""
    if current == profile:
        return
    epoch = str(time.time_ns())
    subprocess.run(["tmux", "set-option", "-g", "@fleet_profile", profile],
                   check=True)
    subprocess.run(["tmux", "set-option", "-g", "@fleet_epoch", epoch],
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
    profile = subprocess.run(["tmux", "show-options", "-gv",
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


def commander_context():
    local = json.loads(subprocess.run(["fleet-next", "context"], text=True,
                                      capture_output=True, check=True).stdout)
    environment = ssh_environment()
    workstations = {}
    for host in ("boltzmann", "noether", "newton"):
        remote = json.loads(subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", host, "fleet-next context"],
            text=True, capture_output=True, check=True, env=environment).stdout)
        workstations[host] = {key: remote[key]
                              for key in ("profile", "unavailable", "slots")}
    print(json.dumps({"sessions": local["sessions"],
                      "unavailable": local["unavailable"],
                      "workstations": workstations}, indent=2))
