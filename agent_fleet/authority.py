"""Finite source-authority mutations for the Fleet action boundary."""

import json
from pathlib import Path

from . import alan, presentation, tmux, transcripts
from .config import KINDS


FIELDS = {
    "create": {"operation", "agent", "name", "cwd"},
    "rename-alan": {"operation", "actor", "name"},
    "rename-tmux": {"operation", "target", "name"},
    "archive-alan": {"operation", "actor", "agent"},
    "archive-composite": {"operation", "actor", "agent", "target", "transcript"},
    "archive-pristine": {"operation", "actor", "agent", "target"},
    "archive-tmux": {"operation", "target", "agent", "transcript"},
    "stop-alan": {"operation", "actor"},
    "open-alan": {"operation", "actor"},
    "restore-native": {"operation", "actor", "agent", "transcript"},
    "restore-transcript": {"operation", "agent", "transcript", "name"},
}

def execute(request):
    operation = request.get("operation")
    if operation not in FIELDS or set(request) != FIELDS[operation]:
        raise ValueError("invalid authority action")
    if any(not isinstance(value, str) or (not value and name != "cwd")
           for name, value in request.items()
           if name not in {"operation", "target"}):
        raise ValueError("invalid authority action")
    if "target" in request and (not isinstance(request["target"], list)
                                or len(request["target"]) != 4):
        raise ValueError("invalid authority action")
    if operation == "create":
        if request["agent"] not in KINDS:
            raise ValueError("create requires a language-actor kind")
        cwd = request["cwd"] or str(Path.home())
        addr = alan.create(request["agent"], request["name"], cwd)
        return {"source": f"alan:{addr}"}
    if operation == "rename-alan":
        alan.rename(request["actor"], request["name"])
        return {"name": request["name"]}
    if operation == "rename-tmux":
        tmux.mutate_target(request["target"], "rename", [request["name"]])
        return {"name": request["name"]}
    if operation == "archive-alan":
        if request["agent"] not in {"llm", "claude", "codex", "grok", "antigravity"}:
            raise ValueError("archive requires a language actor")
        if request["agent"] in {"llm", "antigravity"}:
            presentation.close(request["actor"])
        alan.close(request["actor"])
        return {}
    if operation == "archive-composite":
        if request["agent"] not in {"claude", "codex", "grok"}:
            raise ValueError("archive requires a natively adopted agent")
        transcripts.verify(request["agent"], request["transcript"])
        tmux.mutate_target(request["target"], "archive", [])
        alan.close(request["actor"])
        return {}
    if operation == "archive-pristine":
        if request["agent"] not in {"claude", "codex", "grok"}:
            raise ValueError("archive requires a natively adopted agent")
        alan.verify_pristine(request["actor"])
        tmux.mutate_target(request["target"], "archive", [])
        alan.close(request["actor"])
        return {}
    if operation == "archive-tmux":
        transcripts.verify(request["agent"], request["transcript"])
        tmux.mutate_target(request["target"], "archive", [])
        return {}
    if operation == "stop-alan":
        alan.stop(request["actor"])
        return {}
    if operation == "open-alan":
        alan.open(request["actor"])
        return {}
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
