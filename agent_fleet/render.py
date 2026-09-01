"""Render the Muster projection into fzf rows and header text."""

import re
import time

from .config import machine
from . import alan


STATE_ORDER = {"working": 0, "needs-action": 1, "unavailable": 1, "waiting": 2,
               "hibernated": 3, "finished": 4}
RESET = "\033[0m"
BOLD = "\033[1m"
STATE_COLOUR = {
    "working": "\033[30;42m",
    "needs-action": "\033[1;37;41m",
    "unavailable": "\033[1;37;41m",
    "waiting": "\033[30;43m",
    "hibernated": "\033[37;44m",
    "finished": "\033[37;100m",
}
HOST_COLOUR = {
    "newton": "\033[34m",
    "lovelace": "\033[35m",
    "boltzmann": "\033[33m",
    "turing": "\033[36m",
    "noether": "\033[32m",
}
AGENT_COLOUR = {"claude": "\033[38;5;173m", "codex": "\033[38;5;75m",
                "antigravity": "\033[38;5;141m",
                "grok": "\033[38;5;250m"}
COLUMN_ICONS = {"machine": "", "agent": "", "time": "",
                "status": "", "title": "", "summary": ""}


def recency(session):
    return session.human_activity or session.created


def order(sessions, unavailable, graph, expanded=(), show_python=False):
    sessions = sorted(sessions,
                      key=lambda s: (s.ref.server.source in unavailable,
                                     STATE_ORDER.get(s.state, 2),
                                     -recency(s), s.ref.key))
    if graph is not None:
        return alan.project(sessions, graph, expanded=expanded,
                            show_python=show_python)
    return [alan.Projected(session, 0, 0, False) for session in sessions]


def column_header(sessions):
    icon = COLUMN_ICONS
    sessions = [item.session for item in sessions]
    working = sum(1 for session in sessions if session.state == "working")
    waiting = sum(1 for session in sessions if session.state == "waiting")
    hibernated = sum(1 for session in sessions if session.state == "hibernated")
    asleep = f"  {hibernated} hibernated" if hibernated else ""
    return (f"{icon['machine']} {icon['agent']} {icon['time']:^4} {icon['status']} "
            f"{'':4} {icon['title']:<20} {icon['summary']}  "
            f"{working} working  {waiting} waiting{asleep}  {len(sessions)} total")


def header_text(projected, usage, unavailable):
    empty = "5h [--------]   0%/0h  7d [--------]   0%/0h"
    claude = usage.get("claude", empty)
    codex = usage.get("codex", empty)
    offline = f"  |  offline {' '.join(unavailable)}" if unavailable else ""
    return (f"Claude {claude}{offline}\n"
            f"OpenAI {codex}\n"
            f"{column_header(projected)}")


def rows_text(projected, unavailable, width, now=None, revision=None):
    now = int(time.time()) if now is None else now
    lines = []
    for projection in projected:
        session = projection.session
        if "\t" in session.ref.key or "\n" in session.ref.key:
            raise ValueError("session key contains a row delimiter")
        timestamp = recency(session)
        age = max(0, now - timestamp)
        elapsed = ("?" if not timestamp else
                   f"{age // 60}m" if age < 3600 else f"{age // 3600}h")
        marker = ("?" if session.ref.server.source in unavailable else
                  {"needs-action": "!", "unavailable": "!",
                   "working": "*", "waiting": ".",
                   "hibernated": "z", "finished": "-"}[session.state])
        agent = {"codex": "X", "shell": ""}.get(
            session.agent, session.agent[:1].upper())
        summary = " ".join(
            (session.title if session.agent == "shell" else session.summary).split()
        )
        summary = re.sub(r"^[\u2800-\u28ff✳●*]+\s*", "", summary)
        fold = ("" if not projection.child_count else
                f"{'▾' if projection.expanded else '▸'} {projection.child_count}")
        name = "  " * projection.depth + session.name
        room = max(8, width - 1 - 1 - 1 - 1 - 4 - 1 - 1 - 1 - 4 - 1 - 20 - 1)
        host_colour = HOST_COLOUR.get(session.ref.server.host, "")
        agent_colour = AGENT_COLOUR.get(session.agent, "")
        state_colour = ("\033[37;41m" if marker == "?" else STATE_COLOUR[session.state])
        emphasis = BOLD if session.state in {"working", "needs-action", "unavailable"} else ""
        visible = (f"{emphasis}{host_colour}{machine(session.ref.server.host)}{RESET}{emphasis} "
                   f"{agent_colour}{agent:1}{RESET}{emphasis} {elapsed:>4} "
                   f"{state_colour}{marker}{RESET}{emphasis} "
                   f"{fold:<4} {name:<20.20} {summary:<{room}.{room}}{RESET}")
        identity = (session.ref.key if revision is None else
                    f"{session.ref.key}\t{revision}\t{projection.child_count}")
        lines.append(f"{identity}\t{visible}")
    return "\n".join(lines)
