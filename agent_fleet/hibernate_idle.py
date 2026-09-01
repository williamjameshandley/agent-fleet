import argparse
import time

from .actions import hibernate
from .daemon import snapshot
from .protocol import decode_message


def duration(value):
    suffix = value[-1:]
    scale = {"h": 60 * 60, "d": 24 * 60 * 60}.get(suffix, 1)
    number = value[:-1] if suffix in {"h", "d"} else value
    seconds = int(number) * scale
    if seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return seconds


def candidates(sessions, older_than, now=None):
    now = int(time.time()) if now is None else now
    for session in sessions:
        if reason(session, older_than, now) == "eligible":
            yield session


def reason(session, older_than, now):
    activity = session.recency or session.human_activity or session.created
    if session.ref.server.kind != "alan":
        return "not-an-alan-actor"
    if (session.agent not in {"python", "claude", "codex"}
            or session.hibernation == "unsupported"):
        return "unsupported-evaluator"
    if session.state != "waiting":
        return session.state
    if session.attached:
        return "attached"
    if not activity:
        return "unknown-activity"
    if now - activity < older_than:
        return "recent"
    return "eligible"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--older-than", type=duration, default="48h")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sessions, _usage, _unavailable = decode_message(snapshot())
    now = int(time.time())
    selected = list(candidates(sessions, args.older_than, now))
    rows = sessions if args.dry_run else selected
    for session in rows:
        activity = session.recency or session.human_activity or session.created
        status = reason(session, args.older_than, now)
        print(f"{session.ref.key}\t{session.name}\t{session.agent}\t"
              f"{session.ref.server.host}\t{activity}\t{status}")
        if not args.dry_run and status == "eligible":
            hibernate(session.ref.key)
