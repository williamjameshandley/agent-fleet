import json

from .model import ServerRef, Session, SessionRef


SESSION_FIELDS = {
    "server", "id", "name", "created", "activity", "attached", "windows",
    "command", "title", "cwd", "agent_name", "reported_state", "summary",
    "recency", "transcript_id", "human_activity", "evaluation",
    "evaluation_started", "transcript_path", "worked", "attachment",
    "evaluator", "managed", "hibernation",
}
SERVER_FIELDS = {"source", "socket", "pid", "started", "kind"}
RELATIVE_SERVER_FIELDS = SERVER_FIELDS - {"source"}
ATTACHMENT_FIELDS = {"server", "id"}
ACTOR_FIELDS = {
    "addr", "kind", "state", "hibernation", "cwd", "evaluator", "preset",
    "spawn", "label", "managed", "worked", "created", "last_operation_activity",
    "evaluation_started", "active_evaluation", "latest_displayable_output",
    "source_activity", "unresolved_requests",
}


def _server(server, relative=False):
    value = {"socket": server.socket, "pid": server.pid,
             "started": server.started, "kind": server.kind}
    if not relative:
        value["source"] = server.source
    return value


def _session(session, relative=False):
    return {
        "server": _server(session.ref.server, relative),
        "id": session.ref.session_id, "name": session.name,
        "created": session.created, "activity": session.activity,
        "attached": session.attached, "windows": session.windows,
        "command": session.command, "title": session.title, "cwd": session.cwd,
        "agent_name": session.agent_name,
        "reported_state": session.reported_state,
        "summary": session.summary, "recency": session.recency,
        "transcript_id": session.transcript_id,
        "human_activity": session.human_activity,
        "evaluation": session.evaluation,
        "evaluation_started": session.evaluation_started,
        "transcript_path": session.transcript_path,
        "worked": session.worked,
        "attachment": ({"server": _server(session.attachment.server, relative),
                        "id": session.attachment.session_id}
                       if session.attachment else None),
        "evaluator": session.evaluator,
        "managed": session.managed,
        "hibernation": session.hibernation,
    }


def encode(sessions, usage=None, unavailable=None):
    message = {"version": 4,
               "sessions": [_session(session) for session in sessions],
               "usage": usage or {}, "unavailable": unavailable or []}
    return json.dumps(message, separators=(",", ":"))


def encode_observation(sessions, available, actors):
    return json.dumps({"version": 4,
                       "sessions": [_session(session, relative=True)
                                    for session in sessions],
                       "available": available, "actors": actors},
                      separators=(",", ":"))


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"invalid Fleet {name}")


def _ref(raw, session_id, source=None):
    fields = RELATIVE_SERVER_FIELDS if source is not None else SERVER_FIELDS
    _exact(raw, fields, "server")
    source = source or raw["source"]
    if "@" not in source:
        raise ValueError("invalid Fleet source")
    return SessionRef(ServerRef(source, raw["socket"], raw["pid"], raw["started"],
                                raw["kind"]), session_id)


def _sessions(items, source=None):
    if not isinstance(items, list):
        raise ValueError("invalid Fleet sessions")
    sessions = []
    for raw_item in items:
        _exact(raw_item, SESSION_FIELDS, "session")
        item = dict(raw_item)
        server = item.pop("server")
        session_id = item.pop("id")
        attachment = item.pop("attachment")
        if attachment is not None:
            _exact(attachment, ATTACHMENT_FIELDS, "attachment")
            attachment = _ref(attachment["server"], attachment["id"], source)
        sessions.append(Session(ref=_ref(server, session_id, source),
                                attachment=attachment, **item))
    return sessions


def decode(line):
    return decode_message(line)[0]


def decode_message(line):
    message = json.loads(line)
    _exact(message, {"version", "sessions", "usage", "unavailable"},
           "message")
    if message["version"] != 4:
        raise ValueError(f"unsupported Fleet protocol version {message['version']}")
    return _sessions(message["sessions"]), message["usage"], message["unavailable"]


def _actors(items):
    if not isinstance(items, list):
        raise ValueError("invalid Fleet actors")
    result = []
    for actor in items:
        _exact(actor, ACTOR_FIELDS, "actor")
        if not isinstance(actor["addr"], str) or not isinstance(actor["kind"], str):
            raise ValueError("invalid Fleet actor identity")
        if not isinstance(actor["managed"], bool) or not isinstance(actor["worked"], bool):
            raise ValueError("invalid Fleet actor flags")
        if not isinstance(actor["source_activity"], dict) or not isinstance(
                actor["unresolved_requests"], dict):
            raise ValueError("invalid Fleet actor mappings")
        nullable_strings = {"state", "hibernation", "cwd", "evaluator", "preset",
                            "spawn", "label", "created", "last_operation_activity",
                            "evaluation_started", "active_evaluation"}
        if any(actor[field] is not None and not isinstance(actor[field], str)
               for field in nullable_strings):
            raise ValueError("invalid Fleet actor scalar")
        output = actor["latest_displayable_output"]
        if output is not None and (not isinstance(output, dict)
                or set(output) not in ({"status", "value"}, {"status", "error"})
                or not all(isinstance(value, str) for value in output.values())):
            raise ValueError("invalid Fleet actor output")
        if not all(isinstance(addr, str) and isinstance(time, str)
                   for addr, time in actor["source_activity"].items()):
            raise ValueError("invalid Fleet actor activity")
        for reference, request in actor["unresolved_requests"].items():
            if not isinstance(reference, str) or not isinstance(request, dict) \
                    or set(request) != {"time", "payload"} \
                    or not isinstance(request["time"], str):
                raise ValueError("invalid Fleet actor request")
        result.append(actor)
    return result


def decode_observation(line, source):
    message = json.loads(line)
    _exact(message, {"version", "sessions", "available", "actors"}, "observation")
    if message["version"] != 4:
        raise ValueError(f"unsupported Fleet protocol version {message['version']}")
    if not isinstance(message["available"], bool):
        raise ValueError("invalid Fleet availability")
    return (_sessions(message["sessions"], source.key), message["available"],
            _actors(message["actors"]))
