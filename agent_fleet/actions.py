import os
import subprocess
import shlex
import json
import time
from pathlib import Path

from .config import KINDS, hosts
from .remote import find
from .daemon import action as fleet_action, history as history_projection, history_search
from .daemon import preview as pane_preview, snapshot
from .protocol import decode_message
from . import viewer
from . import workstation


def desktop_input(prompt, values=(), fixed=False):
    result = subprocess.run(
        ["/usr/bin/tmux", "-N", "show-options", "-qv", "-t", "fleet@muster", "@fleet_workstation"],
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


def create_tab():
    subprocess.run(["/usr/bin/tmux", "-N", "new-window", "-t", "fleet@muster", "-n", "create",
                    "exec /usr/lib/agent-fleet/ui create"], check=True)


def _create_human_root(host, agent, name, cwd):
    """Create and present one human-rooted Alan actor for Muster."""
    if os.environ.get("LOOP_SOCKET"):
        raise RuntimeError("Muster create cannot run with an actor socket; use loop.spawn()")
    value = fleet_action({"operation": "create", "host": host,
                          "agent": agent, "name": name, "cwd": cwd})
    key = value["source"]
    viewer.open_main(key)
    return key


def create_prompt():
    """Collect Muster input and create one human-rooted actor."""
    host = muster_input("host", hosts())
    agent = muster_input("agent", KINDS, context=host)
    name = muster_input("name", context=f"{host} · {agent}")
    cwd = muster_input("directory", initial=str(Path.home()),
                       context=f"{host} · {agent} · {name}") or str(Path.home())
    return _create_human_root(host, agent, name, cwd)


def rename_tab(key):
    command = shlex.join(("exec", "/usr/lib/agent-fleet/ui", "rename-prompt", key))
    subprocess.run(["/usr/bin/tmux", "-N", "new-window", "-t", "fleet@muster", "-n", "rename",
                    command], check=True)


def rename(key, name):
    """Rename one canonical source from an explicit value."""
    value = fleet_action({"operation": "rename", "source": key, "name": name})
    return value["name"]


def rename_prompt(key):
    """Collect a Muster name and call :func:`rename`."""
    session = find(key)
    name = muster_input("name", initial=session.name,
                        context=session.ref.server.host,
                        title="Rename session")
    return rename(key, name)


def archive(key):
    fleet_action({"operation": "archive", "source": key})


def next_waiting():
    from .daemon import request
    key = request("next-waiting\t" + dict(viewer.slots()).get("main", "")).strip()
    if not key:
        subprocess.run(["/usr/bin/tmux", "-N", "display-message", "-t", "fleet@muster",
                        "No waiting sessions"])
        return
    viewer.open_main(key)


def preview(key, columns=0, lines=0):
    find(key)
    print(pane_preview(key, columns, lines), end="")


def history():
    value = history_projection()
    rows = [(f"error:{host}", host, "", f"history unavailable: {error}", "")
            for host, error in sorted(value["errors"].items())]
    rows.extend(tuple(item[key] for key in ("key", "host", "agent", "name", "cwd"))
                for item in sorted(value["entries"],
                                   key=lambda row: row["mtime"], reverse=True))
    return rows


def open_history(key):
    name = "" if key.startswith("alan:") else desktop_input("new session name")
    value = fleet_action({"operation": "restore", "history": key, "name": name})
    viewer.open_main(value["source"])


def search_history(query):
    return [(
        item["source"], item["host"], item["agent"], item["name"],
        f"{item['path']}:{item['line']}", item["role"], item["cwd"],
        " ".join(item["text"].split()),
    ) for item in history_search(query)]


def search_history_prompt():
    query = muster_input("search", title="Search history")
    if query:
        from .ui import search_history as show
        show(query)


def arrive(profile, available=False):
    sessions, _, unavailable = decode_message(snapshot())
    if unavailable and not available:
        raise RuntimeError("inventory incomplete; unavailable: " + " ".join(unavailable))
    result = subprocess.run(["/usr/bin/tmux", "-N", "show-options", "-gv",
                             "@fleet_profile"], text=True, capture_output=True)
    current = result.stdout.strip() if result.returncode == 0 else ""
    if current == profile:
        return
    epoch = str(time.time_ns())
    subprocess.run(["/usr/bin/tmux", "-N", "set-option", "-g", "@fleet_profile", profile],
                   check=True)
    subprocess.run(["/usr/bin/tmux", "-N", "set-option", "-g", "@fleet_epoch", epoch],
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


def context():
    sessions, _, unavailable = decode_message(snapshot())
    profile = subprocess.run(["/usr/bin/tmux", "-N", "show-options", "-gv",
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
    return data
