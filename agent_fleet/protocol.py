import json

import networkx as nx

from .model import ServerRef, Session, SessionRef


def encode(sessions, usage=None, unavailable=None, graph=None):
    items = [{
        "server": {"host": s.ref.server.host, "socket": s.ref.server.socket,
                   "pid": s.ref.server.pid, "started": s.ref.server.started,
                   "kind": s.ref.server.kind},
        "id": s.ref.session_id, "name": s.name, "created": s.created,
        "activity": s.activity, "attached": s.attached, "windows": s.windows,
        "command": s.command, "title": s.title, "cwd": s.cwd,
        "agent_name": s.agent_name, "reported_state": s.reported_state,
        "summary": s.summary, "recency": s.recency,
        "transcript_id": s.transcript_id,
        "human_activity": s.human_activity,
        "evaluation": s.evaluation,
        "evaluation_started": s.evaluation_started,
    } for s in sessions]
    message = {"version": 1, "sessions": items, "usage": usage or {},
               "unavailable": unavailable or []}
    if graph is not None:
        message["alan"] = nx.node_link_data(graph, edges="edges")
    return json.dumps(message, separators=(",", ":"))


def decode(line):
    return decode_message(line)[0]


def decode_message(line):
    sessions = []
    message = json.loads(line)
    if message["version"] != 1:
        raise ValueError(f"unsupported Fleet protocol version {message['version']}")
    for item in message["sessions"]:
        raw = item.pop("server")
        sid = item.pop("id")
        kind = raw.pop("kind")
        ref = SessionRef(
            ServerRef(raw["host"], raw["socket"], raw["pid"], raw["started"], kind),
            sid,
        )
        sessions.append(Session(ref=ref, **item))
    return sessions, message["usage"], message["unavailable"]


def decode_graph(line):
    data = json.loads(line).get("alan")
    return nx.node_link_graph(data, edges="edges") if data is not None else None
