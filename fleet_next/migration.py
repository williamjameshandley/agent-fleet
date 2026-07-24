import hashlib
import json
import os
import shlex
import time
from pathlib import Path

from libtmux.session import Session as TmuxSession
from libtmux.exc import LibTmuxException

from .agent import observe
from .alan import (discard_native_import, import_native, native_import_status,
                   request as alan_request)
from .tmux import inventory, server, split_key


def migration_id(key, native_id):
    return hashlib.sha256(f"{key}\0{native_id}".encode()).hexdigest()


def inventory_records(host):
    return [{"key": item.ref.key, "host": host, "provider": item.agent,
             "native_id": item.transcript_id, "name": item.name, "cwd": item.cwd,
             "attention": item.attention, "last_touch": item.human_activity,
             "reported_state": item.reported_state, "windows": item.windows}
            for item in observe(inventory(host)) if item.agent in {"claude", "codex"}]


def _condition(socket, pid, started, session_id, pane_id, pane_pid, provider):
    checks = [f"#{{==:#{{socket_path}},{socket}}}", f"#{{==:#{{pid}},{pid}}}",
              f"#{{==:#{{start_time}},{started}}}",
              f"#{{==:#{{session_id}},{session_id}}}",
              "#{==:#{session_windows},1}", "#{==:#{window_panes},1}",
              f"#{{==:#{{pane_id}},{pane_id}}}"]
    if pane_pid is not None:
        checks.append(f"#{{==:#{{pane_pid}},{pane_pid}}}")
    if provider is not None:
        checks.append(f"#{{==:#{{pane_current_command}},{provider}}}")
    result = checks[-1]
    for check in reversed(checks[:-1]):
        result = f"#{{&&:{check},{result}}}"
    return result


def _foreground_argv(pane):
    stat = Path(f"/proc/{pane.pane_pid}/stat").read_text()
    fields = stat[stat.rfind(")") + 2:].split()
    foreground = int(fields[5])
    data = Path(f"/proc/{foreground}/cmdline").read_bytes().rstrip(b"\0")
    if foreground <= 0 or not data:
        raise RuntimeError("agent pane has no recoverable foreground argv")
    return foreground, [part.decode() for part in data.split(b"\0")]


def _rollback_argv(pane, provider, native_id):
    foreground, argv = _foreground_argv(pane)
    if Path(argv[0]).name != provider:
        raise RuntimeError("foreground argv does not match observed provider")
    if provider == "codex":
        resumes = [index for index, value in enumerate(argv) if value == "resume"]
        if resumes:
            if resumes[-1] + 1 >= len(argv) or argv[resumes[-1] + 1] != native_id:
                raise RuntimeError("Codex argv names a different native identity")
            return foreground, argv, argv
        return foreground, argv, [*argv, "resume", native_id]
    resumes = [index for index, value in enumerate(argv) if value in {"--resume", "-r"}]
    if resumes:
        if resumes[-1] + 1 >= len(argv) or argv[resumes[-1] + 1] != native_id:
            raise RuntimeError("Claude argv names a different native identity")
        return foreground, argv, argv
    return foreground, argv, [*argv, "--resume", native_id]


def _agent_pane(session, provider):
    panes = [pane for window in session.windows for pane in window.panes]
    agents = [pane for pane in panes if pane.pane_current_command == provider]
    if len(agents) != 1:
        raise RuntimeError(f"expected one {provider} pane, found {len(agents)}")
    return agents[0]


def _preserve_other_panes(tmux, session, agent_pane, wanted_migration):
    panes = [pane for window in session.windows for pane in window.panes]
    argv = None
    if len(session.windows) == 1 and len(panes) == 1:
        return agent_pane, argv, None, []
    preserved = [(pane.pane_id, int(pane.pane_pid)) for pane in panes
                 if pane.pane_id != agent_pane.pane_id]
    auxiliary = f"fleet-preserved-{session.session_id.lstrip('$')}-{wanted_migration[:8]}"
    if tmux.has_session(auxiliary):
        raise RuntimeError(f"preservation session already exists: {auxiliary}")
    tmux.new_session(session_name=auxiliary, attach=False,
                     window_command="sleep infinity")
    agent_window = agent_pane.window.window_id
    for window in list(session.windows):
        if window.window_id != agent_window:
            tmux.cmd("move-window", "-s", window.window_id, "-t", f"{auxiliary}:")
    current = TmuxSession.from_session_id(tmux, session.session_id)
    for pane in [pane for pane in current.active_window.panes
                 if pane.pane_id != agent_pane.pane_id]:
        tmux.cmd("break-pane", "-d", "-s", pane.pane_id, "-t", f"{auxiliary}:")
    locations = {line.split("\t")[1]: (line.split("\t")[0], int(line.split("\t")[2]))
                 for line in tmux.cmd("list-panes", "-a", "-F",
                                      "#{session_name}\t#{pane_id}\t#{pane_pid}").stdout}
    for pane_id, pane_pid in preserved:
        if locations.get(pane_id) != (auxiliary, pane_pid):
            raise RuntimeError(f"preserved pane failed verification: {pane_id}")
    current = TmuxSession.from_session_id(tmux, session.session_id)
    panes = [pane for window in current.windows for pane in window.panes]
    if len(current.windows) != 1 or len(panes) != 1 or panes[0].pane_id != agent_pane.pane_id:
        raise RuntimeError("agent pane isolation failed")
    return panes[0], argv, auxiliary, preserved


def _respawn(tmux, pane, condition, cwd, argv):
    result = tmux.cmd("if-shell", "-t", pane.pane_id, "-F", condition,
                      shlex.join(["respawn-pane", "-k", "-t", pane.pane_id,
                                  "-c", cwd, *argv]),
                      "display-message -p FLEET_STALE")
    if result.stdout and result.stdout[0] == "FLEET_STALE":
        raise RuntimeError("stale source identity")


def _wait_command(tmux, pane_id, command):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = tmux.cmd("display-message", "-p", "-t", pane_id,
                          "#{pane_current_command}\t#{pane_dead}")
        if result.stdout:
            current, dead = result.stdout[0].split("\t")
            if dead == "1":
                raise RuntimeError(f"pane died before entering {command} state")
            if current == command:
                return
        time.sleep(.01)
    raise RuntimeError(f"pane did not enter {command} state")


def _holder_condition(tmux, socket, pid, started, session_id, pane_id):
    session = TmuxSession.from_session_id(tmux, session_id)
    panes = [pane for window in session.windows for pane in window.panes]
    if (len(session.windows) != 1 or len(panes) != 1 or
            panes[0].pane_id != pane_id or panes[0].pane_current_command != "sleep"):
        raise RuntimeError("neutral holder identity could not be established")
    return _condition(socket, pid, started, session_id, pane_id,
                      int(panes[0].pane_pid), "sleep")


def _dead_holder_condition(tmux, socket, pid, started, session_id, pane_id):
    session = TmuxSession.from_session_id(tmux, session_id)
    panes = [pane for window in session.windows for pane in window.panes]
    if (len(session.windows) != 1 or len(panes) != 1 or panes[0].pane_id != pane_id or
            int(panes[0].pane_dead) != 1):
        raise RuntimeError("neutral holder identity could not be established")
    base = _condition(socket, pid, started, session_id, pane_id, None, None)
    return f"#{{&&:{base},#{{==:#{{pane_dead}},1}}}}"


def _remain_on_exit(tmux, pane_id, condition, enabled):
    result = tmux.cmd("if-shell", "-t", pane_id, "-F", condition,
                      shlex.join(["set-option", "-w", "-t", pane_id,
                                  "remain-on-exit", "on" if enabled else "off"]),
                      "display-message -p FLEET_STALE")
    if result.stdout and result.stdout[0] == "FLEET_STALE":
        raise RuntimeError("stale source identity")


def _verify_rollback(host, key, native_id, provider, expected_argv):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        matches = [item for item in observe(inventory(host)) if item.ref.key == key]
        if (len(matches) == 1 and matches[0].agent == provider and
                matches[0].transcript_id == native_id and matches[0].reported_state):
            _, _, _, _, session_id = split_key(key)
            pane = TmuxSession.from_session_id(server(), session_id).active_pane
            try:
                _, argv = _foreground_argv(pane)
            except (FileNotFoundError, ProcessLookupError, RuntimeError):
                pass
            else:
                if argv == expected_argv:
                    return
        time.sleep(.1)
    raise RuntimeError("legacy rollback did not restore its exact native identity")


def _revalidate_source(host, key, provider, native_id, pane_id, pane_pid,
                       foreground_pid, foreground_argv, isolated):
    matches = [item for item in observe(inventory(host)) if item.ref.key == key]
    if len(matches) != 1:
        raise RuntimeError("stale source identity")
    item = matches[0]
    if (item.agent != provider or item.reported_state != "waiting" or
            item.transcript_id != native_id):
        raise RuntimeError("source changed before cutover")
    tmux = server()
    _, _, _, _, session_id = split_key(key)
    session = TmuxSession.from_session_id(tmux, session_id)
    panes = [pane for window in session.windows for pane in window.panes]
    matches = [pane for pane in panes if pane.pane_id == pane_id]
    if (len(matches) != 1 or int(matches[0].pane_pid) != pane_pid or
            matches[0].pane_current_command != provider or
            (isolated and (len(session.windows) != 1 or len(panes) != 1))):
        raise RuntimeError("source pane changed before cutover")
    current_pid, current_argv = _foreground_argv(matches[0])
    if current_pid != foreground_pid or current_argv != foreground_argv:
        raise RuntimeError("provider process changed before cutover")


def _status(provider, native_id, wanted_migration):
    status = native_import_status(provider, native_id, wanted_migration)
    if status.get("ambiguous"):
        raise RuntimeError("reconciliation-required: ambiguous ownership commit")
    commit = status.get("commit")
    if not commit:
        return None, status
    actor = commit.get("payload", {}).get("actor")
    if not actor:
        raise RuntimeError("reconciliation-required: commit has no actor")
    return actor, status


def _repair_forward(provider, native_id, wanted_migration):
    actor, status = _status(provider, native_id, wanted_migration)
    if actor is None:
        return None
    if not status.get("ready"):
        try:
            result = alan_request({"op": "spawn", "source": actor})
        except RuntimeError as error:
            if str(error) != "already_active":
                raise
        else:
            if result.get("addr") != actor:
                raise RuntimeError("reconciliation-required: actor resume changed identity")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        actor_again, status = _status(provider, native_id, wanted_migration)
        if actor_again != actor:
            raise RuntimeError("reconciliation-required: ownership actor changed")
        if status.get("ready"):
            return actor
        time.sleep(.1)
    raise RuntimeError("committed actor did not restore its native attachment")


def _cleanup_holder(tmux, holder_condition, pane_id, session_id):
    result = tmux.cmd("if-shell", "-t", pane_id, "-F", holder_condition,
                      shlex.join(["kill-session", "-t", session_id]),
                      "display-message -p FLEET_STALE")
    stale = result.stdout and result.stdout[0] == "FLEET_STALE"
    failed = bool(getattr(result, "returncode", 0) or getattr(result, "stderr", []))
    if stale or failed or tmux.has_session(session_id):
        return "legacy holder cleanup failed"
    return None


def adopt_local(key, wanted_migration, expected_provider=None, expected_native_id=None):
    host, socket, pid, started, session_id = split_key(key)
    if host != os.uname().nodename:
        raise RuntimeError(f"identity is for {host}, not {os.uname().nodename}")
    matches = [item for item in observe(inventory(host)) if item.ref.key == key]
    if len(matches) != 1:
        raise RuntimeError("stale source identity")
    item = matches[0]
    expected_provider = expected_provider or item.agent
    expected_native_id = expected_native_id or item.transcript_id
    if wanted_migration != migration_id(key, expected_native_id):
        raise RuntimeError("migration ID does not match source identity")
    tmux = server()
    session = TmuxSession.from_session_id(tmux, session_id)
    if (session.socket_path, int(session.pid), int(session.start_time)) != (socket, pid, started):
        raise RuntimeError("stale source identity")
    if item.agent != expected_provider or item.transcript_id != expected_native_id:
        panes = [pane for window in session.windows for pane in window.panes]
        if (expected_provider not in {"claude", "codex"} or len(session.windows) != 1 or
                len(panes) != 1 or panes[0].pane_current_command != "sleep"):
            raise RuntimeError("source identity differs from migration manifest")
        holder_condition = _holder_condition(
            tmux, socket, pid, started, session_id, panes[0].pane_id)
        actor = _repair_forward(expected_provider, expected_native_id, wanted_migration)
        if actor is None:
            raise RuntimeError("neutral holder has no ownership commit")
        outcome = {"key": key, "provider": expected_provider,
                   "native_id": expected_native_id, "pane_id": panes[0].pane_id,
                   "actor": actor, "new_key": f"alan:{host}:{actor}"}
        if error := _cleanup_holder(
                tmux, holder_condition, panes[0].pane_id, session_id):
            outcome["cleanup_error"] = error
        return outcome
    if item.agent not in {"claude", "codex"}:
        raise RuntimeError(f"unsupported provider {item.agent}")
    if item.reported_state != "waiting":
        raise RuntimeError(f"literal waiting state required, got {item.reported_state or 'unknown'}")
    if not item.transcript_id:
        raise RuntimeError("durable native identity required")
    actors = alan_request({"op": "list"})["actors"]
    collisions = [actor for actor in actors if actor.get("type") == item.agent and
                  (actor.get("native") or {}).get("id") == item.transcript_id]
    if collisions:
        raise RuntimeError("native identity already belongs to Alan")
    pane = _agent_pane(session, item.agent)
    foreground_pid, foreground_argv, argv = _rollback_argv(
        pane, item.agent, item.transcript_id)
    pane_pid = int(pane.pane_pid)
    _revalidate_source(host, key, item.agent, item.transcript_id,
                       pane.pane_id, pane_pid, foreground_pid, foreground_argv, False)
    pane, _, auxiliary, preserved = _preserve_other_panes(
        tmux, session, pane, wanted_migration)
    _revalidate_source(host, key, item.agent, item.transcript_id,
                       pane.pane_id, pane_pid, foreground_pid, foreground_argv, True)
    condition = _condition(socket, pid, started, session_id, pane.pane_id,
                           pane_pid, item.agent)
    retained = {"key": key, "provider": item.agent, "native_id": item.transcript_id,
                "name": item.name, "cwd": item.cwd, "attention": item.attention,
                "last_touch": item.human_activity, "pane_id": pane.pane_id,
                "rollback_argv": argv, "preserved_session": auxiliary}
    _remain_on_exit(tmux, pane.pane_id, condition, True)
    _respawn(tmux, pane, condition, item.cwd, ["sleep", "infinity"])
    try:
        _wait_command(tmux, pane.pane_id, "sleep")
    except (RuntimeError, LibTmuxException):
        try:
            rollback_condition = _holder_condition(
                tmux, socket, pid, started, session_id, pane.pane_id)
        except (RuntimeError, LibTmuxException):
            rollback_condition = _dead_holder_condition(
                tmux, socket, pid, started, session_id, pane.pane_id)
        _respawn(tmux, pane, rollback_condition, item.cwd, argv)
        _verify_rollback(host, key, item.transcript_id, item.agent, argv)
        _remain_on_exit(
            tmux, pane.pane_id,
            _condition(socket, pid, started, session_id, pane.pane_id, None, None), False)
        raise
    holder_condition = _holder_condition(
        tmux, socket, pid, started, session_id, pane.pane_id)
    source = {"host": host, "key": key, "pane_id": pane.pane_id,
              "preserved_session": auxiliary,
              "preserved_panes": [{"pane_id": pane_id, "pane_pid": pane_pid}
                                  for pane_id, pane_pid in preserved]}
    try:
        response = import_native(item.agent, item.transcript_id, item.name, item.cwd,
                                 item.attention, item.human_activity, source,
                                 wanted_migration)
    except (ConnectionError, FileNotFoundError, OSError, json.JSONDecodeError):
        response = None
    committed = response.get("committed") if response else None
    if response and committed is False:
        _respawn(tmux, pane, holder_condition, item.cwd, argv)
        _verify_rollback(host, key, item.transcript_id, item.agent, argv)
        _remain_on_exit(
            tmux, pane.pane_id,
            _condition(socket, pid, started, session_id, pane.pane_id, None, None), False)
        raise RuntimeError(response.get("error", "import failed before commit"))
    if response and response.get("ok") and committed is True:
        actor = response["addr"]
    else:
        try:
            actor = _repair_forward(item.agent, item.transcript_id, wanted_migration)
        except (ConnectionError, FileNotFoundError, OSError, json.JSONDecodeError,
                RuntimeError):
            raise
        if actor is None:
            try:
                discard_native_import(item.agent, item.transcript_id, wanted_migration)
            except RuntimeError as error:
                if str(error) != "unknown_import":
                    raise
            _respawn(tmux, pane, holder_condition, item.cwd, argv)
            _verify_rollback(host, key, item.transcript_id, item.agent, argv)
            _remain_on_exit(
                tmux, pane.pane_id,
                _condition(socket, pid, started, session_id, pane.pane_id, None, None), False)
            error = response.get("error", "import failed before commit") if response else "lost response"
            raise RuntimeError(error)
    status_actor, status = _status(item.agent, item.transcript_id, wanted_migration)
    if status_actor != actor:
        raise RuntimeError("reconciliation-required: returned and committed actors differ")
    if not status.get("ready"):
        repaired = _repair_forward(item.agent, item.transcript_id, wanted_migration)
        if repaired != actor:
            raise RuntimeError("reconciliation-required: ownership actor changed")
    if error := _cleanup_holder(tmux, holder_condition, pane.pane_id, session_id):
        return {**retained, "actor": actor, "new_key": f"alan:{host}:{actor}",
                "cleanup_error": error}
    return {**retained, "actor": actor, "new_key": f"alan:{host}:{actor}"}
