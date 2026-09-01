import json
import mmap
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import tempfile
import textwrap
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .config import tmux_command


CLAUDE = Path.home() / ".claude/projects"
CODEX = Path.home() / ".codex/sessions"
ANTIGRAVITY = Path.home() / ".gemini/antigravity-cli"
GROK = Path.home() / ".grok/sessions"
AGENTS = {"claude", "codex", "grok", "antigravity"}
PRIORITY = {"needs-action": 0, "working": 1, "waiting": 2, "finished": 3}
PANE_FORMAT = ("name=#{q:session_name} session=#{q:session_id} pid=#{q:pane_pid} "
               "command=#{q:pane_current_command} title=#{q:pane_title}")


@dataclass(frozen=True)
class Transcript:
    agent: str
    session_id: str
    path: Path
    mtime: float

    def events(self):
        with self.path.open() as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)

    def cwd(self):
        if self.agent == "antigravity":
            return antigravity_workspace(self.session_id)
        if self.agent == "grok":
            summary = json.loads((self.path.parent / "summary.json").read_text())
            return summary["info"]["cwd"]
        for event in self.events():
            if self.agent == "claude" and "cwd" in event:
                return event["cwd"]
            if self.agent == "codex" and event.get("type") == "session_meta":
                return event["payload"]["cwd"]
        return ""

    def texts(self, role="assistant", limit=1):
        found = deque(maxlen=limit)
        for event in self.events():
            text = event_text(self.agent, event, role)
            if text:
                found.append(text)
        return list(found)


def transcript(agent, path):
    path = Path(path)
    if agent == "antigravity":
        return Transcript(agent, path.parents[2].name, path, path.stat().st_mtime)
    if agent == "grok":
        return Transcript(agent, path.parent.name, path, path.stat().st_mtime)
    return Transcript(agent, path.stem[-36:], path, path.stat().st_mtime)


def antigravity_transcript_path(conversation):
    return (ANTIGRAVITY / "brain" / conversation
            / ".system_generated/logs/transcript_full.jsonl")


def antigravity_workspace(conversation):
    database = ANTIGRAVITY / "conversation_summaries.db"
    if not database.is_file():
        return ""
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "select workspace_uris from conversation_summaries"
            " where conversation_id = ?", (conversation,)).fetchall()
    for (uris,) in rows:
        for uri in json.loads(uris):
            if uri.startswith("file://"):
                return uri.removeprefix("file://")
    return ""


def all_transcripts(agent=None):
    found = []
    if agent in (None, "claude"):
        found.extend(transcript("claude", path) for path in CLAUDE.glob("*/*.jsonl"))
    if agent in (None, "codex"):
        found.extend(transcript("codex", path)
                     for path in CODEX.glob("*/*/*/rollout-*.jsonl"))
    if agent in (None, "antigravity"):
        found.extend(
            transcript("antigravity", path) for path in
            ANTIGRAVITY.glob("brain/*/.system_generated/logs/transcript_full.jsonl"))
    if agent in (None, "grok"):
        found.extend(transcript("grok", path)
                     for path in GROK.glob("*/*/updates.jsonl"))
    return sorted(found, key=lambda item: item.mtime, reverse=True)


def catalog():
    result = {}
    for item in all_transcripts():
        key = item.agent, item.session_id
        if key in result:
            raise ValueError(
                f"duplicate {item.agent} transcript identity {item.session_id}: "
                f"{result[key].path} and {item.path}"
            )
        result[key] = item
    return result


def find(session_id, agent=None):
    matches = [item for item in all_transcripts(agent)
               if item.session_id.startswith(session_id)]
    identities = {item.session_id for item in matches}
    if not matches:
        raise LookupError(f"no transcript matches {session_id!r}")
    if len(identities) != 1:
        raise LookupError(f"ambiguous transcript {session_id!r}")
    return matches[0]


def verify(agent, session_id):
    item = find(session_id, agent)
    if item.session_id != session_id:
        raise RuntimeError(f"transcript identity changed: {session_id}")
    return item


def history(limit=100):
    rows = []
    for item in sorted(catalog().values(), key=lambda value: value.mtime, reverse=True):
        cwd = item.cwd() or str(Path.home())
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(cwd).name).strip("-")
        rows.append({"agent": item.agent, "session_id": item.session_id,
                     "mtime": int(item.mtime), "cwd": cwd,
                     "name": name or f"{item.agent}-{item.session_id[:8]}"})
        if len(rows) == limit:
            break
    return rows


def search(query):
    query = query.casefold()
    rows = []
    for item in catalog().values():
        cwd = item.cwd() if item.agent in {"grok", "antigravity"} else ""
        matches = []
        with item.path.open() as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if item.agent == "claude" and not cwd and "cwd" in event:
                    cwd = event["cwd"]
                elif (item.agent == "codex" and not cwd
                      and event.get("type") == "session_meta"):
                    cwd = event["payload"]["cwd"]
                for role in ("user", "assistant"):
                    text = event_text(item.agent, event, role)
                    if text and query in text.casefold():
                        matches.append({
                            "agent": item.agent,
                            "session_id": item.session_id,
                            "path": str(item.path),
                            "line": line_number,
                            "role": role,
                            "text": text,
                        })
                        break
        rows.extend({**match, "cwd": cwd} for match in matches)
    return rows


def resume(agent, session_id, name):
    item = verify(agent, session_id)
    command = {"claude": ["claude", "--resume", item.session_id],
               "codex": ["codex", "resume", item.session_id],
               "grok": ["grok", "--resume", item.session_id],
               "antigravity": ["agy", "--conversation", item.session_id]}[agent]
    subprocess.run(tmux_command("new-session", "-d", "-s", name, "-c",
                                item.cwd() or str(Path.home()), *command), check=True)
    subprocess.run(tmux_command("set-option", "-t", name, "status", "on"),
                   check=True)


def resume_native(agent, session_id):
    item = verify(agent, session_id)
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    public = state / "alan" / "loop.sock"
    arguments = {"claude": ["--resume", item.session_id],
                 "codex": ["resume", item.session_id],
                 "grok": ["--resume", item.session_id]}[agent]
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "alan/native"
    runtime.mkdir(parents=True, exist_ok=True)
    root = tempfile.mkdtemp(prefix=f"{agent}-", dir=runtime)
    name = "fleet@native-" + secrets.token_hex(16)
    subprocess.run(tmux_command(
        "new-session", "-d", "-s", name,
        "-c", item.cwd() or str(Path.home()),
        "-e", "ALAN_NATIVE_INNER=1", "-e", f"ALAN_NATIVE_ROOT={root}",
        "-e", f"LOOP_PUBLIC_SOCKET={public}",
        "/usr/lib/alan/alan-native-session", agent, *arguments,
    ), check=True)
    subprocess.run(tmux_command("set-option", "-t", name,
                                "status", "off"), check=True)
    subprocess.run(tmux_command("set-option", "-t", name,
                                "mouse", "on"), check=True)


def event_text(agent, event, role):
    if agent == "claude" and event.get("type") == role:
        blocks = event["message"]["content"]
        if isinstance(blocks, str):
            return blocks
        return "\n".join(block["text"] for block in blocks
                         if block.get("type") == "text")
    if agent == "codex":
        if event.get("type") == "event_msg":
            wanted = {"assistant": "agent_message", "user": "user_message"}[role]
            if event["payload"]["type"] == wanted:
                return event["payload"]["message"]
        if event.get("type") == "response_item":
            payload = event["payload"]
            if payload.get("type") == "message" and payload.get("role") == role:
                wanted = {"assistant": "output_text", "user": "input_text"}[role]
                return "\n".join(block["text"] for block in payload["content"]
                                 if block.get("type") == wanted)
    if agent == "grok" and "session/update" in event.get("method", ""):
        update = event["params"]["update"]
        wanted = {"assistant": "agent_message_chunk",
                  "user": "user_message_chunk"}[role]
        if update.get("sessionUpdate") == wanted:
            return update["content"]["text"]
    if agent == "antigravity":
        wanted = {"assistant": "PLANNER_RESPONSE", "user": "USER_INPUT"}[role]
        if event.get("type") == wanted:
            content = event.get("content", "")
            if role == "user":
                match = re.search(r"<USER_REQUEST>\n(.*?)\n</USER_REQUEST>",
                                  content, re.DOTALL)
                return match.group(1) if match else content
            return content
    return ""


def preview(agent, session_id, columns=0, lines=0):
    return render_preview(find(session_id, agent), columns, lines)


def render_preview(item, columns=0, lines=0):
    messages = deque()
    for event in reverse_events(item.path):
        for role in ("user", "assistant"):
            if text := event_text(item.agent, event, role):
                messages.appendleft((role, text.strip()))
                break
        if len(messages) == 8:
            break
    rendered = "\n\n".join(
        f"{role.capitalize()}\n{text}" for role, text in messages)
    if columns:
        rendered = "\n".join(
            line if not line else "\n".join(textwrap.wrap(
                line, width=columns, replace_whitespace=False,
                drop_whitespace=False))
            for line in rendered.splitlines())
    if lines:
        rendered = "\n".join(rendered.splitlines()[-lines:])
    return rendered + ("\n" if rendered else "")


def reverse_events(path):
    with open(path, "rb") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            return
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            end = len(data)
            while end > 0:
                start = data.rfind(b"\n", 0, end)
                if start < 0:
                    line = data[:end]
                    end = 0
                else:
                    line = data[start + 1:end]
                    end = start
                if line:
                    yield json.loads(line)


def event_time(value):
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def last_event_time(path):
    for event in reverse_events(path):
        if "timestamp" in event:
            return event_time(event["timestamp"])
    return 0


def last_human_time(item):
    for event in reverse_events(item.path):
        human = False
        if item.agent == "claude" and event.get("type") == "user" and not event.get("isMeta"):
            content = event.get("message", {}).get("content")
            human = (isinstance(content, str) or
                     (isinstance(content, list) and
                      any(block.get("type") == "text" for block in content)))
        elif item.agent == "codex":
            human = bool(event_text(item.agent, event, "user"))
        elif item.agent == "grok":
            human = ("session/update" in event.get("method", "")
                     and event["params"]["update"].get("sessionUpdate")
                     == "user_message_chunk")
        elif item.agent == "antigravity":
            human = event.get("type") == "USER_INPUT"
        stamp = event.get("timestamp") or event.get("created_at")
        if human and stamp:
            return event_time(stamp)
    return 0


def latest_assistant_text(item):
    for event in reverse_events(item.path):
        if text := event_text(item.agent, event, "assistant"):
            return " ".join(text.split())
    return ""


def project_native(sessions, transcripts=None):
    transcripts = catalog() if transcripts is None else transcripts
    result = []
    for session in sessions:
        if (session.ref.server.kind != "alan" or session.agent not in AGENTS
                or not session.transcript_id):
            result.append(session)
            continue
        item = transcripts.get((session.agent, session.transcript_id))
        try:
            result.append(replace(
                session, transcript_path=str(item.path) if item else "",
                summary=latest_assistant_text(item) if item else "",
                recency=max(session.recency, last_event_time(item.path))
                if item else session.recency,
                human_activity=max(session.human_activity, last_human_time(item))
                if item else session.human_activity,
            ))
        except (json.JSONDecodeError, ValueError) as error:
            result.append(replace(
                session, transcript_path=str(item.path), reported_state="needs-action",
                summary=f"Transcript is invalid: {item.path.name}: {error}",
            ))
    return result


def codex_state(item):
    boundary, summary, updated = "task_complete", "", 0
    for event in item.events():
        if "timestamp" in event:
            updated = int(datetime.fromisoformat(
                event["timestamp"].replace("Z", "+00:00")).timestamp())
        if event.get("type") == "event_msg":
            kind = event["payload"]["type"]
            if kind in {"task_started", "task_complete"}:
                boundary = kind
            elif kind == "agent_message":
                summary = event["payload"]["message"]
    return ("working" if boundary == "task_started" else "waiting"), summary, updated


def antigravity_state(item):
    last, summary, updated = "", "", 0
    for event in item.events():
        if stamp := event.get("created_at"):
            updated = int(datetime.fromisoformat(
                stamp.replace("Z", "+00:00")).timestamp())
        last = event.get("type", "")
        if last == "PLANNER_RESPONSE":
            summary = event.get("content", "")
    return ("waiting" if last == "CHECKPOINT" else "working"), summary, updated


def antigravity_candidates(pids):
    conversations = set()
    for pid in pids:
        try:
            descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            continue
        for fd in descriptors:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            match = re.search(
                r"antigravity-cli/conversations/([0-9a-f-]{36})\.db$", target)
            if match:
                conversations.add(match.group(1))
    return conversations


def grok_state(item):
    active, summary, updated = False, "", 0
    for event in item.events():
        if "timestamp" in event:
            updated = event_time(event["timestamp"])
        if "session/update" not in event.get("method", ""):
            continue
        update = event["params"]["update"]
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk":
            active = True
        elif kind == "turn_completed":
            active = False
        elif kind == "agent_message_chunk":
            summary = update["content"]["text"]
    return ("working" if active else "waiting"), summary, updated


def grok_candidates(pids):
    sessions = {}
    for pid in pids:
        try:
            descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            continue
        for fd in descriptors:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            match = re.search(
                r"/\.grok/sessions/[^/]+/([0-9a-f-]{36})/", target)
            if match:
                sessions[match.group(1)] = Path(target).parent
    return sessions


def process_tree():
    children = {}
    for line in subprocess.run(["ps", "-eo", "pid=,ppid="], text=True,
                               capture_output=True, check=True).stdout.splitlines():
        pid, parent = map(int, line.split())
        children.setdefault(parent, []).append(pid)
    return children


def descendants(pid, children):
    found, pending = [], [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def select_codex(targets, resumed):
    targets = list(dict.fromkeys(targets))
    explicit = [target for target in targets
                if any(identity in target for identity in resumed)]
    if len(explicit) == 1:
        return explicit[0]
    if len(targets) == 1:
        return targets[0]
    roots = []
    for target in targets:
        with open(target) as stream:
            metadata = json.loads(stream.readline())
        if (metadata.get("type") == "session_meta"
                and metadata["payload"].get("parent_thread_id") is None):
            roots.append(target)
    if len(roots) != 1:
        raise RuntimeError(f"expected one root Codex rollout, found {len(roots)}")
    return roots[0]


def codex_candidates(pids):
    targets, resumed = [], set()
    for pid in pids:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
            descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            continue
        resumed.update(argv[index + 1] for index, value in enumerate(argv[:-1])
                       if value == "resume")
        for fd in descriptors:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if "rollout-" in target:
                targets.append(target)
    return targets, resumed


def codex_transcript(pids):
    targets, resumed = codex_candidates(pids)
    return transcript("codex", select_codex(targets, resumed))


def indexed_claude_agents(output):
    return {item["pid"]: item for item in json.loads(output) if item.get("pid") is not None}


def native_actor(pids):
    actors = set()
    for pid in pids:
        try:
            environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        roots = [value.split(b"=", 1)[1] for value in environment
                 if value.startswith(b"ALAN_NATIVE_ROOT=")]
        for root in roots:
            path = Path(os.fsdecode(root)) / "actor"
            try:
                actor = path.read_text().strip()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise RuntimeError(f"cannot read published native actor {path}: {error}")
            if not actor:
                raise RuntimeError(f"published native actor is empty: {path}")
            actors.add(actor)
    if len(actors) > 1:
        raise RuntimeError("provider process tree carries multiple Alan actors: "
                           + ", ".join(sorted(actors)))
    return next(iter(actors), "")


def observe(sessions, transcripts=None):
    transcripts = catalog() if transcripts is None else transcripts
    sessions = project_native(sessions, transcripts)
    claude = indexed_claude_agents(subprocess.run(
        ["claude", "agents", "--json"], text=True, capture_output=True,
        check=True).stdout)
    children = process_tree()
    rows = []
    sessions_by_id = {session.ref.session_id: session for session in sessions}
    panes = subprocess.run(tmux_command("list-panes", "-a", "-F", PANE_FORMAT),
                           text=True, capture_output=True, check=True).stdout
    for line in panes.splitlines():
        name, session_id, pid, command, title = (
            field.split("=", 1)[1] for field in shlex.split(line))
        agent = Path(command).name
        if agent == "agy":
            agent = "antigravity"
        tree = [int(pid), *descendants(int(pid), children)]
        if name.startswith("fleet@native-"):
            agents = set()
            for item in tree:
                try:
                    executable = os.readlink(f"/proc/{item}/exe")
                except OSError:
                    continue
                if (candidate := Path(executable).name) in AGENTS:
                    agents.add(candidate)
            if len(agents) != 1:
                continue
            [agent] = agents
        elif agent not in AGENTS or "@" in name:
            continue
        try:
            actor = native_actor(tree)
        except RuntimeError as error:
            rows.append((session_id, agent, "needs-action", str(error), 0, "", 0))
            continue
        expected = f"{agent}-"
        suffix = f"@{sessions_by_id[session_id].ref.server.host}"
        if actor and (not actor.startswith(expected) or not actor.endswith(suffix)):
            rows.append((session_id, agent, "needs-action",
                         f"native actor does not match provider presentation: {actor}",
                         0, "", 0))
            continue
        actor_identity = actor[len(expected):-len(suffix)] if actor else ""
        if agent == "claude":
            entry = next((claude[item] for item in tree if item in claude), None)
            if entry is None and not actor_identity:
                continue
            identity = actor_identity or entry["sessionId"]
            if entry is not None and actor_identity and entry["sessionId"] != actor_identity:
                rows.append((session_id, agent, "needs-action",
                             "Claude registry identity does not match Alan actor", 0,
                             actor_identity, 0))
                continue
            state = ("needs-action" if entry and entry.get("state") == "blocked" else
                     "waiting" if (entry and entry.get("status") == "idle")
                     or title.startswith("✳") else "working")
            item = transcripts.get(("claude", identity))
            path = item.path if item else None
            try:
                updated = last_event_time(path) if item else 0
                human_activity = last_human_time(item) if item else 0
                summary = latest_assistant_text(item) if item else ""
            except (json.JSONDecodeError, ValueError) as error:
                state = "needs-action"
                summary = f"Transcript is invalid: {path.name}: {error}"
                updated = int(path.stat().st_mtime)
                human_activity = 0
        elif agent == "grok":
            identities = grok_candidates(tree)
            if len(identities) != 1:
                continue
            [(identity, directory)] = identities.items()
            path = directory / "updates.jsonl"
            if not path.exists():
                continue
            item = transcript("grok", path)
            try:
                state, summary, updated = grok_state(item)
                human_activity = last_human_time(item)
            except (json.JSONDecodeError, ValueError) as error:
                state = "needs-action"
                summary = f"Transcript is invalid: {item.path.name}: {error}"
                updated = int(item.mtime)
                human_activity = 0
        elif agent == "antigravity":
            conversations = antigravity_candidates(tree)
            if len(conversations) != 1:
                continue
            [identity] = conversations
            path = antigravity_transcript_path(identity)
            if not path.exists():
                continue
            item = transcript("antigravity", path)
            try:
                state, summary, updated = antigravity_state(item)
                human_activity = last_human_time(item)
            except (json.JSONDecodeError, ValueError) as error:
                state = "needs-action"
                summary = f"Transcript is invalid: {item.path.name}: {error}"
                updated = int(item.mtime)
                human_activity = 0
        else:
            targets, resumed = codex_candidates(tree)
            try:
                item = transcript("codex", select_codex(targets, resumed))
            except RuntimeError:
                continue
            except (json.JSONDecodeError, ValueError) as error:
                identity = ""
                state = "needs-action"
                summary = f"Codex transcript selection failed: {error}"
                updated = max((int(Path(path).stat().st_mtime) for path in targets), default=0)
                human_activity = 0
                rows.append((session_id, agent, state, " ".join(summary.split()), updated,
                             identity, human_activity))
                continue
            identity = item.session_id
            if actor_identity and identity != actor_identity:
                rows.append((session_id, agent, "needs-action",
                             "Codex rollout identity does not match Alan actor", 0,
                             actor_identity, 0))
                continue
            try:
                state, summary, updated = codex_state(item)
                human_activity = last_human_time(item)
            except (json.JSONDecodeError, ValueError) as error:
                state = "needs-action"
                summary = f"Transcript is invalid: {item.path.name}: {error}"
                updated = int(item.mtime)
                human_activity = 0
        projection_identity = (actor_identity or identity if name.startswith("fleet@native-")
                               else identity)
        rows.append((session_id, agent, state, " ".join(summary.split()), updated,
                     projection_identity, human_activity))

    by_session, counts = {}, {}
    for row in rows:
        sid = row[0]
        counts[sid] = counts.get(sid, 0) + 1
        if sid not in by_session or PRIORITY[row[2]] < PRIORITY[by_session[sid][2]]:
            by_session[sid] = row
        elif row[4] > by_session[sid][4]:
            current = list(by_session[sid])
            current[4] = row[4]
            by_session[sid] = tuple(current)
        current = list(by_session[sid])
        current[6] = max(current[6], row[6])
        by_session[sid] = tuple(current)
    result = []
    for session in sessions:
        row = by_session.get(session.ref.session_id)
        if not row:
            result.append(session)
        elif counts[session.ref.session_id] > 1:
            count = counts[session.ref.session_id]
            result.append(replace(session, agent_name="multiple",
                                  reported_state="needs-action",
                                  summary=f"{count} agent panes — management required",
                                  recency=row[4],
                                  human_activity=row[6] or session.human_activity))
        else:
            result.append(replace(session, agent_name=row[1], reported_state=row[2],
                                  summary=row[3], recency=row[4], transcript_id=row[5],
                                  human_activity=row[6] or session.human_activity))
    return fold_adopted(result)


def fold_adopted(sessions):
    alan = {}
    providers = {}
    for session in sessions:
        if not session.transcript_id or session.agent not in AGENTS:
            continue
        identity = session.ref.server.host, session.agent, session.transcript_id
        target = alan if session.ref.server.kind == "alan" else providers
        target.setdefault(identity, []).append(session)

    replacements = {}
    consumed = set()
    for identity, actors in alan.items():
        native = providers.get(identity, [])
        if len(actors) > 1:
            raise RuntimeError(
                f"expected at most one Alan actor and provider session for {identity}, "
                f"found {len(actors)} and {len(native)}"
            )
        if not native:
            actor = actors[0]
            if (actor.state == "unavailable" and actor.transcript_path
                    and actor.hibernation == "transcript" and not actor.managed):
                replacements[actor.ref] = replace(
                    actor,
                    summary="provider presentation absent; hibernation recovery required")
            continue
        actor = actors[0]
        if len(native) > 1:
            summary = (f"{len(native)} provider presentations share "
                       f"{actor.ref.session_id}")
            if actor.state != "retired":
                replacements[actor.ref] = replace(
                    actor, reported_state="needs-action", summary=summary,
                    attachment_ambiguous=True)
            for provider in native:
                replacements[provider.ref] = replace(
                    provider, name=actor.name, reported_state="needs-action",
                    summary=summary)
            continue
        [provider] = native
        if actor.state == "retired":
            replacements[provider.ref] = replace(provider, name=actor.name)
            continue
        replacements[actor.ref] = replace(
            actor, attachment=provider.ref, attached=provider.attached,
            recency=max(actor.recency, provider.recency),
            reported_state=(provider.state if actor.state in {"retired", "unavailable"}
                            else actor.reported_state))
        consumed.add(provider.ref)

    return [replacements.get(session.ref, session)
            for session in sessions
            if session.ref not in consumed
            and not (session.ref.server.kind == "alan"
                     and session.state in {"retired", "unavailable"}
                     and session.ref not in replacements)]
