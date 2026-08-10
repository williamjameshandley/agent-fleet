import json
import mmap
import os
import re
import shlex
import subprocess
import textwrap
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path


CLAUDE = Path.home() / ".claude/projects"
CODEX = Path.home() / ".codex/sessions"
AGENTS = {"claude", "codex"}
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
    return Transcript(agent, path.stem[-36:], path, path.stat().st_mtime)


def all_transcripts(agent=None):
    found = []
    if agent in (None, "claude"):
        found.extend(transcript("claude", path) for path in CLAUDE.glob("*/*.jsonl"))
    if agent in (None, "codex"):
        found.extend(transcript("codex", path)
                     for path in CODEX.glob("*/*/*/rollout-*.jsonl"))
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
        cwd = ""
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
    command = (["claude", "--resume", item.session_id] if agent == "claude"
               else ["codex", "resume", item.session_id])
    subprocess.run(["/usr/bin/tmux", "-N", "new-session", "-d", "-s", name, "-c",
                    item.cwd() or str(Path.home()), *command], check=True)
    subprocess.run(["/usr/bin/tmux", "-N", "set-option", "-t", name, "status", "on"],
                   check=True)


def event_text(agent, event, role):
    if agent == "claude" and event.get("type") == role:
        blocks = event["message"]["content"]
        if isinstance(blocks, str):
            return blocks
        return "\n".join(block["text"] for block in blocks
                         if block.get("type") == "text")
    if agent == "codex" and event.get("type") == "event_msg":
        wanted = {"assistant": "agent_message", "user": "user_message"}[role]
        if event["payload"]["type"] == wanted:
            return event["payload"]["message"]
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


def last_event_time(path):
    for event in reverse_events(path):
        if "timestamp" in event:
            return int(datetime.fromisoformat(
                event["timestamp"].replace("Z", "+00:00")).timestamp())
    raise ValueError(f"no timestamped events in {path}")


def last_human_time(item):
    for event in reverse_events(item.path):
        human = False
        if item.agent == "claude" and event.get("type") == "user" and not event.get("isMeta"):
            content = event.get("message", {}).get("content")
            human = (isinstance(content, str) or
                     (isinstance(content, list) and
                      any(block.get("type") == "text" for block in content)))
        elif item.agent == "codex" and event.get("type") == "event_msg":
            human = event.get("payload", {}).get("type") == "user_message"
        if human and "timestamp" in event:
            return int(datetime.fromisoformat(
                event["timestamp"].replace("Z", "+00:00")).timestamp())
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
        if session.ref.server.kind != "alan" or session.agent not in AGENTS:
            result.append(session)
            continue
        item = transcripts.get((session.agent, session.transcript_id))
        result.append(replace(
            session, transcript_path=str(item.path) if item else "",
            summary=latest_assistant_text(item) if item else "",
            human_activity=max(session.human_activity, last_human_time(item))
            if item else session.human_activity,
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
    explicit = [target for target in targets
                if any(identity in target for identity in resumed)]
    if len(explicit) == 1:
        return explicit[0]
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


def codex_transcript(pids):
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
    return transcript("codex", select_codex(targets, resumed))


def indexed_claude_agents(output):
    return {item["pid"]: item for item in json.loads(output) if item.get("pid") is not None}


def observe(sessions, transcripts=None):
    sessions = project_native(sessions, transcripts)
    claude = indexed_claude_agents(subprocess.run(
        ["claude", "agents", "--json"], text=True, capture_output=True,
        check=True).stdout)
    children = process_tree()
    rows = []
    panes = subprocess.run(["/usr/bin/tmux", "-N", "list-panes", "-a", "-F", PANE_FORMAT],
                           text=True, capture_output=True, check=True).stdout
    for line in panes.splitlines():
        name, session_id, pid, command, title = (
            field.split("=", 1)[1] for field in shlex.split(line))
        agent = Path(command).name
        if agent not in AGENTS or "@" in name:
            continue
        tree = [int(pid), *descendants(int(pid), children)]
        if agent == "claude":
            entry = next((claude[item] for item in tree if item in claude), None)
            if entry is None:
                continue
            identity = entry["sessionId"]
            state = ("needs-action" if entry.get("state") == "blocked" else
                     "waiting" if entry["status"] == "idle" or title.startswith("✳") else
                     "working")
            path = CLAUDE / entry["cwd"].replace("/", "-").replace(".", "-") / f"{identity}.jsonl"
            updated = last_event_time(path) if path.exists() else 0
            human_activity = last_human_time(transcript("claude", path)) if path.exists() else 0
            summary = (latest_assistant_text(transcript("claude", path))
                       if path.exists() else "")
        else:
            try:
                item = codex_transcript(tree)
            except RuntimeError:
                continue
            identity = item.session_id
            state, summary, updated = codex_state(item)
            human_activity = last_human_time(item)
        rows.append((session_id, agent, state, " ".join(summary.split()), updated, identity,
                     human_activity))

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
        if len(actors) > 1 or len(native) > 1:
            raise RuntimeError(
                f"expected at most one Alan actor and provider session for {identity}, "
                f"found {len(actors)} and {len(native)}"
            )
        if not native:
            continue
        actor, provider = actors[0], native[0]
        replacements[actor.ref] = replace(actor, attachment=provider.ref)
        consumed.add(provider.ref)

    return [replacements.get(session.ref, session)
            for session in sessions if session.ref not in consumed]
