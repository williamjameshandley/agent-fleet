import os
import subprocess
import shlex
import json
import time
import hashlib
from pathlib import Path

from .config import RUNTIME, hosts, ssh_environment
from .remote import find
from .daemon import preview as pane_preview, snapshot
from .protocol import decode
from .protocol import decode_message
from . import viewer
from .alan import rename as alan_rename, set_attention as alan_attention
from .alan import refresh as alan_refresh
from .alan import attachment_usable as alan_attachment_usable
from .tmux import refresh as tmux_refresh, inventory as tmux_inventory
from .migration import migration_id


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


def session_name(value):
    return value.strip().strip(".:").replace(".", "-").replace(":", "-")


def create_tab():
    subprocess.run(["tmux", "new-window", "-t", "fleet@muster", "-n", "create",
                    "exec fleet-next create"], check=True)


def create():
    host = muster_input("host", hosts())
    agent = muster_input("agent", ("claude", "codex", "python", "shell"),
                         context=host)
    name = session_name(muster_input("name", context=f"{host} · {agent}"))
    cwd = muster_input("directory", initial=str(Path.home()),
                       context=f"{host} · {agent} · {name}") or str(Path.home())
    if not name:
        raise SystemExit("session name is required")
    if agent == "shell":
        host_command(host, "tmux", "new-session", "-d", "-s", name, "-c", cwd,
                     *agent_command(agent, name))
        key = created_key(host, name)
    else:
        result = host_command(host, "fleet-next", "alan-spawn", agent, name, cwd,
                              capture_output=True)
        key = f"alan:{host}:{result.stdout.strip()}"
        wait_for_projection(key)
    viewer.open_main(key)


def rename_tab(key):
    command = shlex.join(("exec", "fleet-next", "rename", key))
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
                host_command(session.ref.server.host, "fleet-next", "alan-rename",
                             session.ref.session_id, name)
        else:
            host_command(session.ref.server.host, "fleet-next", "mutate", key, "rename", name)


def done(key):
    session = find(key)
    if session.ref.server.kind == "alan":
        for slot, source in viewer.slots():
            if source == key:
                viewer.request(slot, "")
        if session.ref.server.host == os.uname().nodename:
            alan_attention(session.ref.session_id, "done")
        else:
            host_command(session.ref.server.host, "fleet-next", "alan-attention",
                         session.ref.session_id, "done")
        return
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


def refresh_local(key):
    if key.startswith("alan:"):
        _, host, addr = key.split(":", 2)
        if host != os.uname().nodename:
            raise SystemExit(f"identity is for {host}, not {os.uname().nodename}")
        alan_refresh(addr)
    else:
        tmux_refresh(key)


def refresh_check(key, native_id):
    if key.startswith("alan:"):
        _, host, addr = key.split(":", 2)
        if host != os.uname().nodename:
            raise SystemExit(f"identity is for {host}, not {os.uname().nodename}")
        if not alan_attachment_usable(addr, native_id):
            raise SystemExit(f"actor {addr} has no usable current attachment")
    else:
        host = key.split(":", 1)[0]
        if host != os.uname().nodename:
            raise SystemExit(f"identity is for {host}, not {os.uname().nodename}")
        if not any(session.ref.key == key for session in tmux_inventory(host)):
            raise SystemExit(f"session identity changed: {key}")


def wait_for_projection(key, native_id=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            session = find(key)
        except SystemExit:
            time.sleep(.1)
            continue
        attachment = session.attachment or {}
        if (session.ref.server.kind != "alan" or
                ((native_id is None or session.transcript_id == native_id) and
                 attachment.get("kind") not in {None, "none"})):
            return
        time.sleep(.1)
    raise RuntimeError(f"Fleet projection did not restore {key}")


def refresh(key):
    session = find(key)
    shown = [slot for slot, source in viewer.slots() if source == key]
    try:
        host_command(session.ref.server.host, "fleet-next", "refresh-local", key,
                     capture_output=True)
    except subprocess.CalledProcessError as failure:
        try:
            host_command(session.ref.server.host, "fleet-next", "refresh-check", key,
                         session.transcript_id, capture_output=True)
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
    except (RuntimeError, subprocess.CalledProcessError, SystemExit) as error:
        reason = (error.stderr.strip() if isinstance(error, subprocess.CalledProcessError)
                  and error.stderr else str(error))
        subprocess.run(["tmux", "display-message", "-t", "fleet@muster",
                        f"Refresh failed: {reason}"])
        raise SystemExit(reason)


class ViewerHandoffError(RuntimeError):
    def __init__(self, outcome, failures, slots):
        self.outcome = outcome
        self.slots = slots
        super().__init__("viewer handoff failed: " + "; ".join(failures) +
                         f"; committed source is {outcome['new_key']}")


class ProjectionHandoffError(RuntimeError):
    def __init__(self, outcome, error, slots):
        self.outcome = outcome
        self.slots = slots
        super().__init__(f"projection failed: {error}; committed source is "
                         f"{outcome['new_key']}")


def adopt(key, expected=None):
    session = find(key)
    if session.ref.server.kind == "alan":
        raise SystemExit("source is already owned by Alan")
    shown = [slot for slot, source in viewer.slots() if source == key]
    provider = expected["provider"] if expected else session.agent
    native_id = expected["native_id"] if expected else session.transcript_id
    holder = session.agent == "shell" and session.command == "sleep"
    if ((session.agent != provider or session.transcript_id != native_id) and
            not (expected and holder)):
        raise RuntimeError("source identity differs from migration manifest")
    wanted_migration = migration_id(key, native_id)
    result = host_command(session.ref.server.host, "fleet-next", "adopt-local", key,
                          wanted_migration, provider, native_id, capture_output=True)
    outcome = json.loads(result.stdout)
    new_key = outcome["new_key"]
    try:
        wait_for_projection(new_key, native_id)
    except (RuntimeError, SystemExit) as error:
        raise ProjectionHandoffError(outcome, error, shown) from error
    failures = []
    for slot in shown:
        try:
            viewer.request(slot, new_key)
        except (Exception, SystemExit) as error:
            failures.append(f"{slot}: {error}")
    if failures:
        raise ViewerHandoffError(outcome, failures, shown)
    return outcome


def _migration_paths():
    directory = RUNTIME / "native-session-migration"
    return directory, directory / "manifest.json", directory / "manifest.sha256", \
        directory / "outcomes.jsonl"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _load_or_capture_manifest():
    directory, manifest_path, digest_path, _ = _migration_paths()
    if manifest_path.exists():
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest_path.read_text().strip():
            raise RuntimeError("migration manifest digest mismatch")
        return json.loads(raw)
    records = []
    configured_hosts = hosts()
    for host in configured_hosts:
        result = host_command(host, "fleet-next", "migration-inventory", "--host", host,
                              capture_output=True)
        records.extend(json.loads(result.stdout))
    manifest = {"captured": int(time.time()), "hosts": configured_hosts,
                "sessions": sorted(records, key=lambda item: item["key"])}
    raw = _canonical(manifest)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    digest = os.open(digest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(digest, "w") as stream:
        stream.write(hashlib.sha256(raw).hexdigest() + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return manifest


def converge():
    manifest = _load_or_capture_manifest()
    _, manifest_path, digest_path, outcomes_path = _migration_paths()
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest_path.read_text().strip():
        raise RuntimeError("migration manifest digest mismatch")
    outcomes = []
    if outcomes_path.exists():
        outcomes = [json.loads(line) for line in outcomes_path.read_text().splitlines()]
    succeeded = {(row["key"], row.get("migration_id"), row.get("manifest_entry")): row
                 for row in outcomes if row.get("status") == "migrated" and
                 isinstance(row.get("new_key"), str) and row["new_key"].startswith("alan:")}
    sessions, _, unavailable = decode_message(snapshot())
    live = {session.ref.key: session for session in sessions}
    failed = False
    for record in manifest["sessions"]:
        key = record["key"]
        wanted_migration = migration_id(key, record["native_id"])
        entry_id = hashlib.sha256(_canonical(record)).hexdigest()
        success_key = (key, wanted_migration, entry_id)
        if success_key in succeeded:
            previous = succeeded[success_key]
            if previous.get("cleanup_error"):
                try:
                    cleanup = adopt(key, record)
                except (ViewerHandoffError, ProjectionHandoffError) as error:
                    error_field = ("viewer_error" if isinstance(error, ViewerHandoffError)
                                   else "projection_error")
                    row = {"time": int(time.time()), "key": key,
                           "status": "migrated", "migration_id": wanted_migration,
                           "manifest_entry": entry_id,
                           "new_key": error.outcome["new_key"],
                           error_field: str(error), "handoff_slots": error.slots}
                    if error.outcome.get("cleanup_error"):
                        row["cleanup_error"] = error.outcome["cleanup_error"]
                    failed = True
                except (RuntimeError, subprocess.CalledProcessError, SystemExit) as error:
                    detail = (error.stderr.strip()
                              if isinstance(error, subprocess.CalledProcessError)
                              and error.stderr else str(error))
                    row = {"time": int(time.time()), "key": key,
                           "status": "migrated", "migration_id": wanted_migration,
                           "manifest_entry": entry_id, "new_key": previous["new_key"],
                           "cleanup_error": " ".join(detail.split())}
                    failed = True
                else:
                    row = {"time": int(time.time()), "key": key,
                           "status": "migrated", "migration_id": wanted_migration,
                           "manifest_entry": entry_id, "new_key": cleanup["new_key"]}
                    if cleanup.get("cleanup_error"):
                        row["cleanup_error"] = cleanup["cleanup_error"]
                        failed = True
                with outcomes_path.open("a") as stream:
                    stream.write(json.dumps(row, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                print(f"{key}\t{'failed' if row.get('cleanup_error') else 'migrated'}: "
                      f"{row.get('cleanup_error', row['new_key'])}")
                continue
            handoff_error = previous.get("projection_error") or previous.get("viewer_error")
            if handoff_error:
                stage = "projection"
                try:
                    wait_for_projection(previous["new_key"], record["native_id"])
                    stage = "viewer"
                    for slot in previous.get("handoff_slots", []):
                        viewer.request(slot, previous["new_key"])
                except (Exception, SystemExit) as error:
                    row = {"time": int(time.time()), "key": key, "status": "migrated",
                           "migration_id": wanted_migration, "manifest_entry": entry_id,
                           "new_key": previous["new_key"], "handoff_slots": previous.get(
                               "handoff_slots", []), f"{stage}_error": str(error)}
                    with outcomes_path.open("a") as stream:
                        stream.write(json.dumps(row, sort_keys=True) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    print(f"{key}\tfailed: {error}")
                    failed = True
                else:
                    row = {"time": int(time.time()), "key": key, "status": "migrated",
                           "migration_id": wanted_migration, "manifest_entry": entry_id,
                           "new_key": previous["new_key"]}
                    with outcomes_path.open("a") as stream:
                        stream.write(json.dumps(row, sort_keys=True) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    print(f"{key}\tmigrated: {previous['new_key']}")
            else:
                print(f"{key}\tmigrated: {previous['new_key']}")
            continue
        reason = None
        terminal = "skipped"
        current = live.get(key)
        current_holder = bool(current and current.agent == "shell" and
                              current.command == "sleep")
        if record["host"] in unavailable:
            reason = "unavailable"
            terminal = "failed"
        elif current is None:
            reason = "source-missing-without-success"
            terminal = "failed"
        elif ((current.agent != record["provider"] or
               current.transcript_id != record["native_id"]) and
              not current_holder):
            reason = "source-identity-changed"
            terminal = "failed"
        elif current.reported_state != "waiting" and not current_holder:
            reason = current.reported_state or "unknown-state"
        elif not current.transcript_id and not current_holder:
            reason = "no-durable-identity"
        if reason:
            print(f"{key}\t{terminal}: {reason}")
            row = {"time": int(time.time()), "key": key, "status": terminal,
                   "reason": reason, "migration_id": wanted_migration,
                   "manifest_entry": entry_id}
            with outcomes_path.open("a") as stream:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            failed = failed or terminal == "failed"
            continue
        try:
            outcome = adopt(key, record)
        except (ViewerHandoffError, ProjectionHandoffError) as error:
            error_field = ("viewer_error" if isinstance(error, ViewerHandoffError)
                           else "projection_error")
            row = {"time": int(time.time()), "key": key, "status": "migrated",
                   "migration_id": wanted_migration, "manifest_entry": entry_id,
                   "new_key": error.outcome["new_key"], error_field: str(error),
                   "handoff_slots": error.slots}
            if error.outcome.get("cleanup_error"):
                row["cleanup_error"] = error.outcome["cleanup_error"]
            print(f"{key}\tfailed: {error}")
            failed = True
        except (RuntimeError, subprocess.CalledProcessError, SystemExit) as error:
            detail = (error.stderr.strip() if isinstance(error, subprocess.CalledProcessError)
                      and error.stderr else str(error))
            row = {"time": int(time.time()), "key": key, "status": "failed",
                   "reason": " ".join(detail.split()),
                   "migration_id": wanted_migration, "manifest_entry": entry_id}
            print(f"{key}\tfailed: {row['reason']}")
            failed = True
        else:
            row = {"time": int(time.time()), "key": key, "status": "migrated",
                   "migration_id": wanted_migration, "manifest_entry": entry_id,
                   "new_key": outcome["new_key"]}
            if outcome.get("cleanup_error"):
                row["cleanup_error"] = outcome["cleanup_error"]
                failed = True
            print(f"{key}\tmigrated: {row['new_key']}")
        with outcomes_path.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    if failed:
        raise SystemExit(1)


def refresh_all():
    sessions, _, unavailable = decode_message(snapshot())
    failed = False
    for session in sorted(sessions, key=lambda item: item.ref.key):
        key = session.ref.key
        reason = None
        if session.ref.server.host in unavailable:
            reason = "unavailable"
        elif session.agent not in {"claude", "codex"}:
            reason = f"unsupported-{session.agent}"
        elif session.state != "waiting":
            reason = session.state
        elif session.windows != 1:
            reason = f"windows-{session.windows}"
        elif not session.transcript_id:
            reason = "no-durable-identity"
        if reason:
            print(f"{key}\tskipped: {reason}")
            continue
        try:
            refresh(key)
        except (RuntimeError, subprocess.CalledProcessError, SystemExit) as error:
            detail = (error.stderr.strip()
                      if isinstance(error, subprocess.CalledProcessError) and error.stderr
                      else str(error))
            detail = " ".join(detail.split())
            print(f"{key}\tfailed: {detail}")
            failed = True
        else:
            print(f"{key}\trefreshed")
    if failed:
        raise SystemExit(1)


def refresh_command(key, all_sessions):
    if all_sessions:
        refresh_all()
    else:
        refresh_report(key)


def next_waiting_key(sessions, active):
    waiting = [session for session in sessions
               if session.attention != "done" and session.state == "waiting"]
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
                      "agent": s.agent, "state": s.state, "attention": s.attention,
                      "summary": s.summary, "recency": s.human_activity}
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
