import json

from .model import ServerRef, Session, SessionRef


def encode(sessions, usage=None, unavailable=None):
    items = [{
        "server": {"host": s.ref.server.host, "socket": s.ref.server.socket,
                   "pid": s.ref.server.pid, "started": s.ref.server.started},
        "id": s.ref.session_id, "name": s.name, "created": s.created,
        "activity": s.activity, "attached": s.attached, "windows": s.windows,
        "command": s.command, "title": s.title, "cwd": s.cwd,
        "attention": s.attention,
        "agent_name": s.agent_name, "reported_state": s.reported_state,
        "summary": s.summary, "recency": s.recency,
        "transcript_id": s.transcript_id,
        "attachment": s.attachment,
        "human_activity": s.human_activity,
        "source_kind": s.ref.server.kind,
    } for s in sessions]
    return json.dumps({"sessions": items, "usage": usage or {},
                       "unavailable": unavailable or []}, separators=(",", ":"))


def decode(line):
    return decode_message(line)[0]


def decode_message(line):
    sessions = []
    message = json.loads(line)
    for item in message["sessions"]:
        raw = item.pop("server")
        sid = item.pop("id")
        kind = item.pop("source_kind", raw.pop("kind", "tmux"))
        ref = SessionRef(ServerRef(raw["host"], raw["socket"], raw["pid"], raw["started"], kind), sid)
        sessions.append(Session(ref=ref, **item))
    return sessions, message["usage"], message["unavailable"]
