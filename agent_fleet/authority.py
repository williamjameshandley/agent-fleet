"""Finite source-authority mutations for the Fleet action boundary."""

import json
import os

from . import alan, presentation, tmux, transcripts
from .config import KINDS


FIELDS = {
    "create": {"operation", "agent", "name", "cwd"},
    "rename-alan": {"operation", "actor", "name"},
    "rename-tmux": {"operation", "source", "name"},
    "archive-alan": {"operation", "actor", "agent"},
    "archive-composite": {"operation", "actor", "agent", "source", "transcript"},
    "archive-pristine": {"operation", "actor", "agent", "source"},
    "archive-tmux": {"operation", "source", "agent", "transcript"},
    "close-native": {"operation", "actor", "agent", "transcript", "source"},
    "restore-alan": {"operation", "actor"},
    "restore-native": {"operation", "actor", "agent", "transcript"},
    "restore-transcript": {"operation", "agent", "transcript", "name"},
}

def execute(request):
    operation = request.get("operation")
    if operation not in FIELDS or set(request) != FIELDS[operation]:
        raise ValueError("invalid authority action")
    if any(not isinstance(value, str) or not value
           for name, value in request.items() if name != "operation"):
        raise ValueError("invalid authority action")
    if operation == "create":
        if request["agent"] not in KINDS:
            raise ValueError("create requires a language-actor kind")
        addr = alan.create(request["agent"], request["name"], request["cwd"])
        return {"source": f"alan:{addr}"}
    if operation == "rename-alan":
        alan.rename(request["actor"], request["name"])
        return {"name": request["name"]}
    if operation == "rename-tmux":
        tmux.mutate(request["source"], "rename", [request["name"]])
        return {"name": request["name"]}
    if operation == "archive-alan":
        if request["agent"] not in {"llm", "claude", "codex", "grok", "antigravity", "python"}:
            raise ValueError("archive requires an Alan actor")
        if request["agent"] in {"llm", "antigravity"}:
            presentation.close(request["actor"])
        alan.retire(request["actor"])
        return {}
    if operation == "archive-composite":
        if request["agent"] not in {"claude", "codex", "grok"}:
            raise ValueError("archive requires a natively adopted agent")
        transcripts.verify(request["agent"], request["transcript"])
        tmux.mutate(request["source"], "archive", [])
        alan.retire(request["actor"])
        return {}
    if operation == "archive-pristine":
        if request["agent"] not in {"claude", "codex", "grok"}:
            raise ValueError("archive requires a natively adopted agent")
        alan.verify_pristine(request["actor"])
        tmux.mutate(request["source"], "archive", [])
        alan.retire(request["actor"])
        return {}
    if operation == "archive-tmux":
        transcripts.verify(request["agent"], request["transcript"])
        tmux.mutate(request["source"], "archive", [])
        return {}
    if operation == "close-native":
        if request["agent"] not in {"claude", "codex", "grok"}:
            raise ValueError("close requires a natively adopted agent")
        if (alan.address_identity(request["actor"], request["agent"])
                != request["transcript"]):
            raise ValueError("actor and transcript identity differ")
        transcripts.verify(request["agent"], request["transcript"])
        actors = alan.actors()
        matches = [actor for actor in actors
                   if actor["addr"] == request["actor"]]
        if len(matches) != 1:
            raise RuntimeError("native actor is unavailable or ambiguous")
        [actor] = matches
        if (actor.get("evaluator") != "native"
                or actor.get("capabilities") != "full"
                or actor.get("managed", False)):
            raise RuntimeError("actor is not an adopted full native session")
        sessions = tmux.inventory(os.uname().nodename, actors)
        sessions = transcripts.observe(sessions, transcripts.catalog())
        selected = [session for session in sessions
                    if session.ref.key == f"alan:{request['actor']}"]
        if len(selected) != 1:
            raise RuntimeError("native actor is unavailable or ambiguous")
        [session] = selected
        if (session.agent != request["agent"]
                or session.transcript_id != request["transcript"]
                or session.attachment is None
                or session.attachment.key != request["source"]):
            raise RuntimeError("native actor attachment identity differs")
        if session.state != "waiting":
            raise RuntimeError("native actor or provider is not waiting")
        tmux.mutate(request["source"], "archive", [])
        return {}
    if operation == "restore-alan":
        return {"source": f"alan:{alan.resume(request['actor'])}"}
    if operation == "restore-native":
        if alan.address_identity(request["actor"], request["agent"]) != request["transcript"]:
            raise ValueError("actor and transcript identity differ")
        transcripts.resume_native(request["agent"], request["transcript"])
        return {"source": f"alan:{request['actor']}"}
    if request["agent"] in {"claude", "codex", "grok"}:
        transcripts.resume_native(request["agent"], request["transcript"])
    else:
        transcripts.resume(
            request["agent"], request["transcript"], request["name"]
        )
    return {"agent": request["agent"], "transcript": request["transcript"]}


def execute_json(raw):
    return json.dumps(execute(json.loads(raw)), separators=(",", ":"))
