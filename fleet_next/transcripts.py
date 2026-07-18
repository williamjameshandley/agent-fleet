import json
import mmap
import os
import re
import shlex
import subprocess
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


def find(session_id, agent=None):
    matches = [item for item in all_transcripts(agent)
               if item.session_id.startswith(session_id)]
    identities = {item.session_id for item in matches}
    if not matches:
        raise SystemExit(f"no transcript matches {session_id!r}")
    if len(identities) != 1:
        raise SystemExit(f"ambiguous transcript {session_id!r}")
    return matches[0]


def history(limit=100):
    rows = []
    seen = set()
    for item in all_transcripts():
        key = item.agent, item.session_id
        if key in seen:
            continue
        seen.add(key)
        cwd = item.cwd() or str(Path.home())
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(cwd).name).strip("-")
        rows.append({"agent": item.agent, "session_id": item.session_id,
                     "mtime": int(item.mtime), "cwd": cwd,
                     "name": name or f"{item.agent}-{item.session_id[:8]}"})
        if len(rows) == limit:
            break
    return rows


def resume(agent, session_id, name):
    item = find(session_id, agent)
    command = (["claude", "--resume", item.session_id] if agent == "claude"
               else ["codex", "resume", item.session_id])
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c",
                    item.cwd() or str(Path.home()), *command], check=True)
    subprocess.run(["tmux", "set-option", "-t", f"={name}", "status", "off"],
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


def reverse_events(path):
    with open(path, "rb") as stream, mmap.mmap(
            stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        end = len(data)
        while end:
            start = data.rfind(b"\n", 0, end)
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
                and metadata["payload"].get("source") == "cli"):
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


def observe(sessions):
    claude = {item["pid"]: item for item in json.loads(subprocess.run(
        ["claude", "agents", "--json"], text=True, capture_output=True,
        check=True).stdout)}
    children = process_tree()
    rows = []
    panes = subprocess.run(["tmux", "list-panes", "-a", "-F", PANE_FORMAT],
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
            summary = title
        else:
            try:
                item = codex_transcript(tree)
            except RuntimeError:
                continue
            identity = item.session_id
            state, summary, updated = codex_state(item)
        rows.append((session_id, agent, state, " ".join(summary.split()), updated, identity))

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
                                  recency=row[4]))
        else:
            result.append(replace(session, agent_name=row[1], reported_state=row[2],
                                  summary=row[3], recency=row[4], transcript_id=row[5]))
    return result
