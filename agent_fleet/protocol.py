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
        "transcript_path": s.transcript_path,
        "attachment": ({
            "server": {
                "host": s.attachment.server.host,
                "socket": s.attachment.server.socket,
                "pid": s.attachment.server.pid,
                "started": s.attachment.server.started,
                "kind": s.attachment.server.kind,
            },
            "id": s.attachment.session_id,
        } if s.attachment else None),
    } for s in sessions]
    message = {"version": 1, "sessions": items, "usage": usage or {},
               "unavailable": unavailable or []}
    if graph is not None:
        message["alan"] = nx.node_link_data(graph, edges="edges")
    return json.dumps(message, separators=(",", ":"))


def decode(line):
    return decode_message(line)[0]


def decode_message(line):
    return decode_value(json.loads(line))


def decode_value(message):
    sessions = []
    if message["version"] != 1:
        raise ValueError(f"unsupported Fleet protocol version {message['version']}")
    for item in message["sessions"]:
        raw = item.pop("server")
        sid = item.pop("id")
        kind = raw.pop("kind")
        attachment = item.pop("attachment", None)
        if attachment:
            attachment_server = attachment["server"]
            attachment = SessionRef(
                ServerRef(
                    attachment_server["host"],
                    attachment_server["socket"],
                    attachment_server["pid"],
                    attachment_server["started"],
                    attachment_server["kind"],
                ),
                attachment["id"],
            )
        ref = SessionRef(
            ServerRef(raw["host"], raw["socket"], raw["pid"], raw["started"], kind),
            sid,
        )
        sessions.append(Session(ref=ref, attachment=attachment, **item))
    return sessions, message["usage"], message["unavailable"]


def decode_graph(line):
    return graph_value(json.loads(line))


def graph_value(message):
    data = message.get("alan")
    return nx.node_link_graph(data, edges="edges") if data is not None else None


def decode_observation(line):
    message = json.loads(line)
    sessions, usage, unavailable = decode_value(message)
    return sessions, usage, unavailable, graph_value(message)
