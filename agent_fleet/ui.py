import os
import subprocess
import time
import shutil
import re
import textwrap

from .config import RUNTIME, machine
from .daemon import snapshot
from .protocol import decode_message
from . import viewer


STATE_ORDER = {"working": 0, "needs-action": 1, "waiting": 2, "finished": 3}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
STATE_COLOUR = {
    "working": "\033[30;42m",
    "needs-action": "\033[1;37;41m",
    "waiting": "\033[30;43m",
    "finished": "\033[37;100m",
}
HOST_COLOUR = {
    "newton": "\033[34m",
    "lovelace": "\033[35m",
    "boltzmann": "\033[33m",
    "turing": "\033[36m",
    "noether": "\033[32m",
}
AGENT_COLOUR = {"claude": "\033[38;5;173m", "codex": "\033[38;5;75m"}
FZF_COLOUR = "16,fg:-1,bg:-1,fg+:-1,bg+:8,hl:3,hl+:3,info:4,prompt:2,pointer:1,marker:1,spinner:6,header:4,gutter:-1,border:8"
COLUMN_ICONS = {"machine": "", "agent": "", "time": "",
                "status": "", "title": "", "summary": ""}


def rows(include_header=True):
    sessions, usage, unavailable = ordered()
    now = int(time.time())
    empty = "5h [--------]   0%/0h  7d [--------]   0%/0h"
    claude = usage.get("claude", empty)
    codex = usage.get("codex", empty)
    offline = f"  |  offline {' '.join(unavailable)}" if unavailable else ""
    if include_header:
        print(f"Claude {claude}{offline}")
        print(f"OpenAI {codex}")
        print(column_header())
    width = shutil.get_terminal_size((100, 24)).columns
    for session in sessions:
        timestamp = recency(session)
        age = max(0, now - timestamp)
        elapsed = ("?" if not timestamp else
                   f"{age // 60}m" if age < 3600 else f"{age // 3600}h")
        marker = ("?" if session.ref.server.host in unavailable else
                  "x" if session.attention == "done" else
                  {"needs-action": "!", "working": "*", "waiting": ".",
                   "finished": "-"}[session.state])
        agent = {"claude": "C", "codex": "X", "python": "P", "gemini": "G",
                 "multiple": "M", "shell": ""}[session.agent]
        summary = " ".join((session.summary or session.title).split())
        summary = re.sub(r"^[\u2800-\u28ff✳●*]+\s*", "", summary)
        room = max(8, width - 1 - 1 - 1 - 1 - 4 - 1 - 1 - 1 - 20 - 1)
        host_colour = HOST_COLOUR.get(session.ref.server.host, "")
        agent_colour = AGENT_COLOUR.get(session.agent, "")
        state_colour = ("\033[37;41m" if marker == "?" else "\033[30;47m" if marker == "x"
                        else STATE_COLOUR[session.state])
        emphasis = (DIM if marker == "x" else BOLD
                    if session.state in {"working", "needs-action"} else "")
        visible = (f"{emphasis}{host_colour}{machine(session.ref.server.host)}{RESET}{emphasis} "
                   f"{agent_colour}{agent:1}{RESET}{emphasis} {elapsed:>4} "
                   f"{state_colour}{marker}{RESET}{emphasis} "
                   f"{session.name:<20.20} {summary:<{room}.{room}}{RESET}")
        print(f"{session.ref.key}\t{visible}")


def ordered():
    sessions, usage, unavailable = decode_message(snapshot())
    sessions.sort(key=lambda s: (s.ref.server.host in unavailable,
                                 s.attention == "done", STATE_ORDER.get(s.state, 2),
                                 -recency(s), s.ref.key))
    return sessions, usage, unavailable


def recency(session):
    return session.human_activity or session.created


def column_header():
    icon = COLUMN_ICONS
    return (f"{icon['machine']} {icon['agent']} {icon['time']:^4} {icon['status']} "
            f"{icon['title']:<20} {icon['summary']}")


def muster():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock = RUNTIME / "muster.sock"
    sock.unlink(missing_ok=True)
    command = [
        "fzf", "--listen", str(sock), "--track", "--disabled", "--no-input", "--ansi",
        f"--color={FZF_COLOUR}",
        "--no-unicode", "--pointer=>", "--gutter= ",
        "--no-scrollbar", "--no-hscroll",
        "--delimiter=\t", "--with-nth=2..", "--id-nth=1",
        "--layout=reverse", "--no-sort", "--no-multi", "--info=inline", "--border=none",
        f"--header={header()}",
        f"--footer={footer()}",
        "--footer-border=bottom",
        "--bind=start:unbind(esc)",
        "--bind=/:enable-search+toggle-sort+show-input+change-prompt(Search: )+unbind(/,c,r,R,d,x,j,k)+rebind(esc)",
        "--bind=esc:disable-search+toggle-sort+clear-query+hide-input+change-prompt(> )+unbind(esc)+rebind(/,c,r,R,d,x,j,k)",
        "--bind=j:down,k:up",
        f"--bind=load:pos({cursor()})+unbind(load)",
        "--bind=enter:execute-silent(fleet show --slot main {1})",
        "--bind=left-click:execute-silent(fleet show --slot main {1})",
        "--bind=double-click:execute-silent(fleet show --slot main {1})",
        "--bind=c:execute-silent(fleet create-tab)",
        "--bind=r:execute-silent(fleet rename-tab {1})",
        "--bind=R:execute-silent(fleet refresh {1})+reload-sync(fleet items)",
        "--bind=d:execute-silent(fleet done {1})+reload(fleet items)",
        "--bind=x:execute-silent(fleet dismiss-source {1})+reload-sync(fleet items)",
        "--bind=tab:execute-silent(tmux select-window -t fleet@muster:history)",
        "--bind=shift-tab:execute-silent(tmux select-window -t fleet@muster:history)",
        "--preview=fleet preview {1} $FZF_PREVIEW_COLUMNS $FZF_PREVIEW_LINES",
        "--preview-window=down,45%,nowrap,follow,border-none",
    ]
    os.execvp(command[0], command)


def header():
    _, usage, unavailable = ordered()
    empty = "5h [--------]   0%/0h  7d [--------]   0%/0h"
    offline = f"  |  offline {' '.join(unavailable)}" if unavailable else ""
    return (f"Claude {usage.get('claude', empty)}{offline}\n"
            f"OpenAI {usage.get('codex', empty)}\n"
            f"{column_header()}")


def footer():
    hints = ("Enter show  c create  r rename  R refresh  d done  x dismiss")
    width = max(1, shutil.get_terminal_size((100, 24)).columns - 2)
    return textwrap.fill(hints, width=width, break_long_words=False,
                         break_on_hyphens=False)


def cursor():
    sessions, _, _ = ordered()
    active = dict(viewer.slots()).get("main")
    if active:
        position = next((i for i, session in enumerate(sessions, 1)
                         if session.ref.key == active), None)
        if position:
            return position
    return next((i for i, session in enumerate(sessions, 1)
                 if session.attention != "done" and session.state == "waiting"), 1)


def select(key):
    sessions, _, _ = ordered()
    position = next((i for i, session in enumerate(sessions, 1)
                     if session.ref.key == key), None)
    if position is None:
        return
    path = RUNTIME / "muster.sock"
    if path.exists():
        subprocess.run(["curl", "-fsS", "--max-time", "2", "--unix-socket", str(path),
                        "-XPOST", "-d", f"pos({position})", "http://localhost"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def history():
    command = [
        "fzf", "--track", "--delimiter=\t", "--with-nth=2..",
        f"--color={FZF_COLOUR}",
        "--id-nth=1", "--layout=reverse", "--no-sort", "--no-multi",
        "--header=History  Enter resurrect  Tab live",
        "--bind=enter:execute-silent(fleet resurrect {1})+reload-sync(fleet history-rows)",
        "--bind=tab:execute-silent(tmux select-window -t fleet@muster:live)",
        "--bind=shift-tab:execute-silent(tmux select-window -t fleet@muster:live)",
    ]
    os.execvp(command[0], command)
