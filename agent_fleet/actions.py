import os
import subprocess
import shlex
import json
import time
from pathlib import Path

from .config import hosts, ssh_environment
from .remote import find
from .daemon import commander_context as commander_projection, preview as pane_preview, snapshot
from .protocol import decode
from .protocol import decode_message
from . import viewer
from . import workstation
from .alan import rename as alan_rename


def host_command(host, *command, capture_output=False, stdout=None):
    argv = list(command) if host == os.uname().nodename else [
        "ssh", "-T", "-o", "BatchMode=yes", host, shlex.join(command)]
    return subprocess.run(argv, text=True, check=True, capture_output=capture_output,
                          stdout=stdout)


def desktop_input(prompt, values=(), fixed=False):
    result = subprocess.run(
        ["tmux", "show-options", "-qv", "-t", "fleet@muster", "@fleet_workstation"],
        text=True, capture_output=True, check=True)
    name = result.stdout.strip()
    if not name:
        raise SystemExit("Muster has no attached workstation")
    try:
        return workstation.request(name, {
            "operation": "prompt", "prompt": prompt,
            "values": list(values), "fixed": fixed,
        })
    except (OSError, RuntimeError) as error:
        raise SystemExit(str(error))


def muster_input(prompt, values=(), initial="", context="", title="Create session"):
    from .ui import FZF_COLOUR
    command = ["fzf", "--layout=reverse", "--no-multi", "--no-unicode",
               f"--color={FZF_COLOUR}", f"--prompt={prompt}> ",
               f"--header={title}  {context}"]
    if values:
        result = subprocess.run(command, input="\n".join(values) + "\n",
                                text=True, stdout=subprocess.PIPE)
        if result.returncode:
            raise SystemExit(result.returncode)
        return result.stdout.strip()
    command.extend(("--disabled", "--print-query", f"--query={initial}"))
    result = subprocess.run(command, input=initial + "\n", text=True,
                            stdout=subprocess.PIPE)
    if result.returncode:
        raise SystemExit(result.returncode)
    return result.stdout.splitlines()[0].strip()


def created_key(host, name):
    result = host_command(host, "fleet", "snapshot", "--host", host,
                          capture_output=True)
    matches = [session.ref.key for session in decode(result.stdout)
               if session.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"created session {host}:{name} did not resolve uniquely")
    return matches[0]


def session_name(value):
    return value.strip().strip(".:").replace(".", "-").replace(":", "-")


def create_tab():
    subprocess.run(["tmux", "new-window", "-t", "fleet@muster", "-n", "create",
                    "exec fleet create"], check=True)


def create():
    host = muster_input("host", hosts())
    agent = muster_input("agent", ("codex", "claude"),
                         context=host)
    name = session_name(muster_input("name", context=f"{host} · {agent}"))
    cwd = muster_input("directory", initial=str(Path.home()),
                       context=f"{host} · {agent} · {name}") or str(Path.home())
    if not name:
        raise SystemExit("session name is required")
    result = host_command(host, "fleet", "actor-create", agent, name, cwd,
                          stdout=subprocess.PIPE)
    addr = result.stdout.strip()
    key = f"alan:{addr}"
    wait_for_projection(key)
    viewer.open_main(key)


def rename_tab(key):
    command = shlex.join(("exec", "fleet", "rename", key))
    subprocess.run(["tmux", "new-window", "-t", "fleet@muster", "-n", "rename",
                    command], check=True)


def rename(key):
    session = find(key)
    name = session_name(muster_input("name", initial=session.name,
                                     context=session.ref.server.host,
                                     title="Rename session"))
    if name:
        if session.ref.server.kind == "alan":
            if session.ref.server.host == os.uname().nodename:
                alan_rename(session.ref.session_id, name)
            else:
                host_command(session.ref.server.host, "fleet", "alan-rename",
                             session.ref.session_id, name)
        else:
            host_command(session.ref.server.host, "fleet", "mutate", key, "rename", name)


def wait_for_absence(key):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            find(key)
        except SystemExit:
            return
        time.sleep(.1)
    raise RuntimeError(f"Fleet projection did not archive {key}")


def archive(key):
    session = find(key)
    if session.agent not in {"claude", "codex"} or not session.transcript_id:
        raise SystemExit("archive requires a durable Claude or Codex identity")
    if session.ref.server.kind == "alan":
        host_command(session.ref.server.host, "fleet", "alan-retire",
                     session.ref.session_id, capture_output=True)
    else:
        host_command(session.ref.server.host, "fleet", "transcript-check",
                     session.agent, session.transcript_id, capture_output=True)
        host_command(session.ref.server.host, "fleet", "mutate", key, "archive",
                     capture_output=True)
    wait_for_absence(key)
    for slot, source in viewer.slots():
        if source == key:
            viewer.request(slot, "")


def archive_report(key):
    try:
        archive(key)
    except (RuntimeError, subprocess.CalledProcessError, SystemExit) as error:
        reason = (error.stderr.strip() if isinstance(error, subprocess.CalledProcessError)
                  and error.stderr else str(error))
        subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                        f"Archive failed: {reason}"])
        raise SystemExit(reason)


def wait_for_projection(key, native_id=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            session = find(key)
        except SystemExit:
            time.sleep(.1)
            continue
        if (session.ref.server.kind != "alan" or
                native_id is None or session.transcript_id == native_id):
            return
        time.sleep(.1)
    raise RuntimeError(f"Fleet projection did not restore {key}")


def next_waiting_key(sessions, active):
    waiting = [session for session in sessions if session.state == "waiting"]
    if not waiting:
        return None
    current = next((i for i, session in enumerate(waiting)
                    if session.ref.key == active), -1)
    return waiting[(current + 1) % len(waiting)].ref.key


def next_waiting():
    from .ui import ordered
    sessions, _, _ = ordered()
    key = next_waiting_key(sessions, dict(viewer.slots()).get("main"))
    if key is None:
        subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                        "No waiting sessions"])
        return
    viewer.show(key, "main")


def preview(key, columns=0, lines=0):
    find(key)
    print(pane_preview(key, columns, lines), end="")


def history():
    live = {(session.ref.server.host, session.agent, session.transcript_id)
            for session in decode(snapshot()) if session.transcript_id}
    authorities = set(live)
    rows = []
    for host in hosts():
        result = host_command(host, "fleet", "alan-actors", capture_output=True)
        for actor in json.loads(result.stdout):
            native_id = (actor.get("native") or {}).get("id")
            identity = (host, actor.get("kind"), native_id)
            if (actor.get("kind") in {"claude", "codex"} and native_id and
                    actor.get("state") in {"retired", "unavailable"}):
                authorities.add(identity)
                key = f'alan:{actor["addr"]}'
                mtime = max(actor.get("human_activity", 0), actor.get("created", 0))
                rows.append((mtime, key, host, actor["kind"],
                             actor.get("label") or actor["addr"], actor.get("cwd") or ""))
        result = host_command(host, "fleet", "transcripts", "--limit", "100",
                              capture_output=True)
        for item in json.loads(result.stdout):
            if (host, item["agent"], item["session_id"]) not in authorities:
                key = f'{host}:{item["agent"]}:{item["session_id"]}'
                rows.append((item["mtime"], key, host, item["agent"],
                             item["name"], item["cwd"]))
    for _, key, host, agent, name, cwd in sorted(rows, reverse=True):
        print("\t".join((key, host, agent, name, cwd)))


def open_history(key):
    if key.startswith("alan:"):
        addr = key.removeprefix("alan:")
        host = addr.rsplit("@", 1)[1]
        host_command(host, "fleet", "alan-resume", addr, capture_output=True)
        wait_for_projection(key)
        viewer.open_main(key)
        return
    host, agent, transcript = key.split(":", 2)
    if any((session.ref.server.host, session.agent, session.transcript_id) ==
           (host, agent, transcript) for session in decode(snapshot())):
        raise SystemExit("that transcript already has a live session")
    name = desktop_input("new session name")
    if not name:
        raise SystemExit("session name is required")
    host_command(host, "fleet", "resume", agent, transcript, name,
                 capture_output=True)
    viewer.request("main", created_key(host, name))


def open_history_report(key):
    try:
        open_history(key)
    except (RuntimeError, subprocess.CalledProcessError, SystemExit) as error:
        reason = (error.stderr.strip() if isinstance(error, subprocess.CalledProcessError)
                  and error.stderr else str(error))
        subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                        f"Open failed: {reason}"])
        raise SystemExit(reason)


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
                     if session.windows == 1
                     and session.ref.key not in shown),
                    key=lambda session: ({"needs-action": 0, "working": 1,
                                          "waiting": 2, "finished": 3}.get(session.state, 2),
                                         -session.human_activity))
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
                      "agent": s.agent, "state": s.state,
                      "summary": s.summary, "recency": s.human_activity}
                     for s in sessions],
    }
    print(json.dumps(data, indent=2))


def commander_context():
    print(json.dumps(json.loads(commander_projection()), indent=2))
