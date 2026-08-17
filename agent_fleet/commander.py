from pathlib import PurePath

from .config import KINDS


FIELDS = {
    "show": {"type", "request_id", "snapshot_revision", "source", "workstation", "slot"},
    "clear_slot": {"type", "request_id", "snapshot_revision", "workstation", "slot"},
    "create": {"type", "request_id", "snapshot_revision", "host", "agent", "name", "cwd"},
    "rename": {"type", "request_id", "snapshot_revision", "source", "name"},
    "archive": {"type", "request_id", "snapshot_revision", "source"},
    "open": {"type", "request_id", "snapshot_revision", "history", "workstation", "slot"},
}


def validate_proposal(proposal, request):
    operation = proposal.get("type") if isinstance(proposal, dict) else None
    if operation not in FIELDS or set(proposal) != FIELDS[operation]:
        raise ValueError("invalid Commander proposal shape")
    snapshot = request["snapshot"]
    if (proposal["request_id"] != request["request_id"] or
            proposal["snapshot_revision"] != snapshot["revision"]):
        raise ValueError("Commander proposal refers to another request")
    sessions = {item["source"]: item for item in snapshot["sessions"]}
    workstations = snapshot["workstations"]
    history = {item["key"] for item in snapshot["history"]}

    if operation in {"show", "clear_slot", "open"}:
        workstation = proposal["workstation"]
        slots = {item["slot"] for item in workstations.get(workstation, {}).get("slots", [])}
        if workstation not in workstations or proposal["slot"] not in slots:
            raise ValueError("unknown workstation viewer slot")
    if operation in {"show", "rename", "archive"} and proposal["source"] not in sessions:
        raise ValueError("unknown Fleet source")
    if operation == "archive":
        session = sessions[proposal["source"]]
        retained = (session["agent"] == "llm" and
                    proposal["source"].startswith("alan:")) or (
            session["agent"] in {"claude", "codex"} and
            (session.get("transcript_id") or session.get("worked") is False))
        if not retained:
            raise ValueError("source is not a recoverable LLM session")
    if operation == "open" and proposal["history"] not in history:
        raise ValueError("unknown Fleet history key")
    if operation == "create":
        cwd = proposal["cwd"]
        if proposal["host"] not in snapshot["hosts"] or proposal["agent"] not in KINDS:
            raise ValueError("invalid create target")
        if not isinstance(proposal["name"], str) or not proposal["name"]:
            raise ValueError("invalid session name")
        if cwd is not None and (not isinstance(cwd, str) or not PurePath(cwd).is_absolute()):
            raise ValueError("cwd must be null or absolute")
    if operation == "rename" and (not isinstance(proposal["name"], str) or not proposal["name"]):
        raise ValueError("invalid session name")
    return proposal
