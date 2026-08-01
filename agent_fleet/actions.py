import os
import subprocess
import shlex
import json
import sys
import time
from pathlib import Path

from .config import hosts, ssh_environment
from .remote import find
from .daemon import preview as pane_preview, snapshot
from .protocol import decode
from .protocol import decode_message
from . import viewer
from . import workstation
from .alan import rename as alan_rename
from .alan import refresh as alan_refresh
from .alan import native_identity_usable as alan_native_identity_usable


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
    command = ["alan-create"]
    if agent == "codex":
        command.append("--present")
    result = host_command(host, *command, agent, name, cwd,
                          stdout=subprocess.PIPE)
    addr = result.stdout.strip()
    key = f"alan:{host}:{addr}"
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
    if session.agent not in {"claude", "codex"} or not session.transcript_id:
        raise ValueError("archive requires a durable Claude or Codex identity")
    if session.ref.server.kind == "alan":
        host_python(
            session.ref.server.host,
            "import sys; from agent_fleet.alan import retire; retire(sys.argv[1])",
            session.ref.session_id, capture_output=True,
        )
    else:
        host_python(
            session.ref.server.host,
            "import sys; from agent_fleet.transcripts import verify; "
            "verify(sys.argv[1], sys.argv[2])",
            session.agent, session.transcript_id, capture_output=True,
        )
        host_python(
            session.ref.server.host,
            "import sys; from agent_fleet.tmux import mutate; "
            "mutate(sys.argv[1], 'archive', [])",
            key, capture_output=True,
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


def refresh_local(key):
    if not key.startswith("alan:"):
        raise ValueError("refresh requires an Alan-owned session")
    _, host, addr = key.split(":", 2)
    if host != os.uname().nodename:
        raise ValueError(f"identity is for {host}, not {os.uname().nodename}")
    alan_refresh(addr)


def refresh_check(key, native_id):
    if not key.startswith("alan:"):
        raise ValueError("refresh requires an Alan-owned session")
    _, host, addr = key.split(":", 2)
    if host != os.uname().nodename:
        raise ValueError(f"identity is for {host}, not {os.uname().nodename}")
    if not alan_native_identity_usable(addr, native_id):
        raise RuntimeError(f"actor {addr} has no usable current native identity")


def wait_for_projection(key, native_id=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            session = find(key)
        except LookupError:
            time.sleep(.1)
            continue
        if (session.ref.server.kind != "alan" or
                native_id is None or session.transcript_id == native_id):
            return
        time.sleep(.1)
    raise RuntimeError(f"Fleet projection did not restore {key}")


def refresh(key):
    session = find(key)
    shown = [slot for slot, source in viewer.slots() if source == key]
    try:
        host_python(
            session.ref.server.host,
            "import sys; from agent_fleet.actions import refresh_local; "
            "refresh_local(sys.argv[1])",
            key, capture_output=True,
        )
    except subprocess.CalledProcessError as failure:
        try:
            host_python(
                session.ref.server.host,
                "import sys; from agent_fleet.actions import refresh_check; "
                "refresh_check(sys.argv[1], sys.argv[2])",
                key, session.transcript_id, capture_output=True,
            )
        except subprocess.CalledProcessError:
            pass
        else:
            for slot in shown:
                viewer.request(slot, key)
        raise failure
    else:
        wait_for_projection(key, session.transcript_id)
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


def history():
    live = {(session.ref.server.host, session.agent, session.transcript_id)
            for session in decode(snapshot()) if session.transcript_id}
    authorities = set(live)
    rows = []
    for host in hosts():
        result = host_python(
            host,
            "import json; from agent_fleet.alan import actors; print(json.dumps(actors()))",
            capture_output=True,
        )
        for actor in json.loads(result.stdout):
            native_id = (actor.get("native") or {}).get("id")
            identity = (host, actor.get("type"), native_id)
            if (actor.get("type") in {"claude", "codex"} and native_id and
                    actor.get("state") in {"retired", "failed"}):
                authorities.add(identity)
                key = f'alan:{host}:{actor["addr"]}'
                mtime = max(actor.get("human_activity", 0), actor.get("created", 0))
                rows.append((mtime, key, host, actor["type"],
                             actor.get("label") or actor["addr"], actor.get("cwd") or ""))
        result = host_python(
            host,
            "import json; from agent_fleet.transcripts import history; "
            "print(json.dumps(history(100)))",
            capture_output=True,
        )
        for item in json.loads(result.stdout):
            if (host, item["agent"], item["session_id"]) not in authorities:
                key = f'{host}:{item["agent"]}:{item["session_id"]}'
                rows.append((item["mtime"], key, host, item["agent"],
                             item["name"], item["cwd"]))
    return [(key, host, agent, name, cwd)
            for _, key, host, agent, name, cwd in sorted(rows, reverse=True)]


def open_history(key):
    if key.startswith("alan:"):
        _, host, addr = key.split(":", 2)
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
