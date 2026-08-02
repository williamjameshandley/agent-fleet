import os
import subprocess
import shlex
import json
import sys
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


def host_python(host, source, *arguments, capture_output=False, stdout=None):
    """Run one fixed Python operation across the SSH host boundary."""
    return host_command(
        host, sys.executable, "-c", source, *arguments,
        capture_output=capture_output, stdout=stdout,
    )


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
    result = host_python(
        host,
        "import sys; from agent_fleet.tmux import inventory; "
        "from agent_fleet.protocol import encode; "
        "print(encode(inventory(sys.argv[1])))",
        host, capture_output=True,
    )
    matches = [session.ref.key for session in decode(result.stdout)
               if session.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"created session {host}:{name} did not resolve uniquely")
    return matches[0]


def session_name(value):
    return value.strip().strip(".:").replace(".", "-").replace(":", "-")


def create_tab():
    subprocess.run(["tmux", "new-window", "-t", "fleet@muster", "-n", "create",
                    "exec /usr/lib/agent-fleet/ui create"], check=True)


def create(host, agent, name, cwd):
    """Create and present one Alan actor from explicit values."""
    if agent not in {"claude", "codex"}:
        raise ValueError(f"unsupported agent {agent!r}")
    name = session_name(name)
    if not name:
        raise ValueError("session name is required")
    result = host_python(
        host,
        "import sys; from agent_fleet.alan import create; "
        "print(create(sys.argv[1], sys.argv[2], sys.argv[3]))",
        agent, name, cwd,
                          stdout=subprocess.PIPE)
    addr = result.stdout.strip()
    key = f"alan:{addr}"
    wait_for_projection(key)
    viewer.open_main(key)
    return key


def create_prompt():
    """Collect Muster input and call :func:`create`."""
    host = muster_input("host", hosts())
    agent = muster_input("agent", ("codex", "claude"), context=host)
    name = muster_input("name", context=f"{host} · {agent}")
    cwd = muster_input("directory", initial=str(Path.home()),
                       context=f"{host} · {agent} · {name}") or str(Path.home())
    return create(host, agent, name, cwd)


def rename_tab(key):
    command = shlex.join(("exec", "/usr/lib/agent-fleet/ui", "rename-prompt", key))
    subprocess.run(["tmux", "new-window", "-t", "fleet@muster", "-n", "rename",
                    command], check=True)


def rename(key, name):
    """Rename one canonical source from an explicit value."""
    session = find(key)
    name = session_name(name)
    if name:
        if session.ref.server.kind == "alan":
            if session.ref.server.host == os.uname().nodename:
                alan_rename(session.ref.session_id, name)
            else:
                host_python(
                    session.ref.server.host,
                    "import sys; from agent_fleet.alan import rename; "
                    "rename(sys.argv[1], sys.argv[2])",
                    session.ref.session_id, name,
                )
        else:
            host_python(
                session.ref.server.host,
                "import sys; from agent_fleet.tmux import mutate; "
                "mutate(sys.argv[1], 'rename', [sys.argv[2]])",
                key, name,
            )
    return name


def rename_prompt(key):
    """Collect a Muster name and call :func:`rename`."""
    session = find(key)
    name = muster_input("name", initial=session.name,
                        context=session.ref.server.host,
                        title="Rename session")
    return rename(key, name)


def wait_for_absence(key):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            find(key)
        except LookupError:
            return
        time.sleep(.1)
    raise RuntimeError(f"Fleet projection did not archive {key}")


def archive(key):
    session = find(key)
    if session.ref.server.kind == "alan":
        if session.agent not in {"llm", "claude", "codex"}:
            raise ValueError("archive requires a language actor")
        host_python(
            session.ref.server.host,
            "import sys; from agent_fleet.alan import retire; retire(sys.argv[1])",
            session.ref.session_id, capture_output=True,
        )
    else:
        if session.agent not in {"claude", "codex"} or not session.transcript_id:
            raise ValueError("archive requires a durable Claude or Codex identity")
        host_python(
            session.ref.server.host,
            "import sys; from agent_fleet.transcripts import verify; "
            "from agent_fleet.tmux import mutate; "
            "verify(sys.argv[1], sys.argv[2]); mutate(sys.argv[3], 'archive', [])",
            session.agent, session.transcript_id, key, capture_output=True,
        )
    wait_for_absence(key)
    for slot, source in viewer.slots():
        if source == key:
            viewer.request(slot, "")


def archive_report(key):
    try:
        archive(key)
    except (LookupError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        reason = (error.stderr.strip() if isinstance(error, subprocess.CalledProcessError)
                  and error.stderr else str(error))
        subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                        f"Archive failed: {reason}"])
        raise SystemExit(reason)


def refresh(key):
    session = find(key)
    if session.ref.server.kind != "alan":
        raise ValueError("refresh requires an Alan actor")
    shown = [slot for slot, source in viewer.slots() if source == key]
    host_python(
        session.ref.server.host,
        "import sys; from agent_fleet.presentation import refresh; refresh(sys.argv[1])",
        session.ref.session_id, capture_output=True,
    )
    for slot in shown:
        viewer.request(slot, key)


def refresh_report(key):
    try:
        refresh(key)
    except (LookupError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        reason = (error.stderr.strip() if isinstance(error, subprocess.CalledProcessError)
                  and error.stderr else str(error))
        subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                        f"Refresh failed: {reason}"])
        raise SystemExit(reason)


def wait_for_projection(key):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            session = find(key)
        except LookupError:
            time.sleep(.1)
            continue
        return
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
    projected, _, _ = ordered()
    sessions = [item.session for item in projected]
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
    entries = json.loads(commander_projection())["history"]
    return [tuple(item[key] for key in ("key", "host", "agent", "name", "cwd"))
            for item in sorted(entries, key=lambda row: row["mtime"], reverse=True)]


def open_history(key):
    if key.startswith("alan:"):
        addr = key.removeprefix("alan:")
        host = addr.rsplit("@", 1)[1]
        host_python(
            host,
            "import sys; from agent_fleet.alan import resume; print(resume(sys.argv[1]))",
            addr, capture_output=True,
        )
        wait_for_projection(key)
        viewer.open_main(key)
        return
    host, agent, transcript = key.split(":", 2)
    if any((session.ref.server.host, session.agent, session.transcript_id) ==
           (host, agent, transcript) for session in decode(snapshot())):
        raise ValueError("that transcript already has a live session")
    name = desktop_input("new session name")
    if not name:
        raise ValueError("session name is required")
    host_python(
        host,
        "import sys; from agent_fleet.transcripts import resume; "
        "resume(sys.argv[1], sys.argv[2], sys.argv[3])",
        agent, transcript, name, capture_output=True,
    )
    viewer.request("main", created_key(host, name))


def open_history_report(key):
    try:
        open_history(key)
    except (LookupError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        reason = (error.stderr.strip() if isinstance(error, subprocess.CalledProcessError)
                  and error.stderr else str(error))
        subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                        f"Open failed: {reason}"])
        raise SystemExit(reason)


def arrive(profile, available=False):
    sessions, _, unavailable = decode_message(snapshot())
    if unavailable and not available:
        raise RuntimeError("inventory incomplete; unavailable: " + " ".join(unavailable))
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
    return data
