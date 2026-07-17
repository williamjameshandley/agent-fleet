import os
import time
import shutil
import re

from .config import RUNTIME, machine
from .daemon import snapshot
from .protocol import decode_message
from . import viewer


STATE_ORDER = {"working": 0, "needs-action": 1, "waiting": 2, "finished": 3}
RESET = "\033[0m"
STATE_COLOUR = {
    "working": "\033[32m",
    "needs-action": "\033[1;31m",
    "waiting": "\033[33m",
    "finished": "\033[90m",
}
HOST_COLOUR = {
    "newton": "\033[34m",
    "lovelace": "\033[35m",
    "boltzmann": "\033[33m",
    "turing": "\033[36m",
    "noether": "\033[32m",
}
FZF_COLOUR = "16,fg:-1,bg:-1,fg+:-1,bg+:8,hl:3,hl+:3,info:4,prompt:2,pointer:1,marker:1,spinner:6,header:4,gutter:-1,border:8"


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
    width = shutil.get_terminal_size((100, 24)).columns
    placement = {source: slot for slot, source in viewer.slots() if source}
    for session in sessions:
        age = max(0, now - (session.recency or session.activity))
        elapsed = f"{age // 60}m" if age < 3600 else f"{age // 3600}h"
        marker = ("?" if session.ref.server.host in unavailable else
                  "x" if session.attention == "done" else
                  {"needs-action": "!", "working": "*", "waiting": ".",
                   "finished": "-"}[session.state])
        agent = {"claude": "Claude", "codex": "OpenAI", "gemini": "Gemini",
                 "multiple": "Agents", "shell": ""}[session.agent]
        summary = " ".join((session.summary or session.title).split())
        summary = re.sub(r"^[\u2800-\u28ff✳●*]+\s*", "", summary)
        description = " ".join(x for x in (agent, summary) if x).strip()
        place = placement.get(session.ref.key, "")
        room = max(8, width - 2 - 1 - 20 - 8 - 4 - 8)
        host_colour = HOST_COLOUR.get(session.ref.server.host, "")
        state_colour = ("\033[31m" if marker == "?" else "\033[90m" if marker == "x"
                        else STATE_COLOUR[session.state])
        visible = (f"{host_colour}{machine(session.ref.server.host):<2}{RESET} "
                   f"{state_colour}{marker}{RESET} {session.name:<20.20} "
                   f"{place:<8.8} {description:<{room}.{room}} {elapsed:>4}")
        print(f"{session.ref.key}\t{visible}")


def ordered():
    sessions, usage, unavailable = decode_message(snapshot())
    sessions.sort(key=lambda s: (s.ref.server.host in unavailable,
                                 s.attention == "done", STATE_ORDER.get(s.state, 2),
                                 -(s.recency or s.activity), s.ref.key))
    return sessions, usage, unavailable


def muster():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock = RUNTIME / "muster.sock"
    sock.unlink(missing_ok=True)
    command = [
        "fzf", "--listen", str(sock), "--track", "--disabled", "--no-input", "--ansi",
        f"--color={FZF_COLOUR}",
        "--no-unicode", "--pointer=>", "--gutter= ",
        "--no-scrollbar", "--no-hscroll",
        "--delimiter=\t", "--with-nth=2..", "--nth=2..", "--id-nth=1",
        "--layout=reverse", "--no-multi", "--info=inline", "--border=none",
        f"--header={header()}",
        "--bind=/:enable-search+show-input+unbind(c,r,d,x,j,k)",
        "--bind=esc:disable-search+clear-query+hide-input+rebind(c,r,d,x,j,k)",
        "--bind=j:down,k:up",
        f"--bind=load:pos({cursor()})",
        "--bind=enter:execute-silent(fleet-next show {1})",
        "--bind=left-click:execute-silent(fleet-next show {1})",
        "--bind=double-click:execute-silent(fleet-next show {1})",
        "--bind=c:execute-silent(fleet-next create)+reload-sync(fleet-next items)",
        "--bind=r:execute-silent(fleet-next rename {1})+reload-sync(fleet-next items)",
        "--bind=d:execute-silent(fleet-next done {1})+reload(fleet-next items)",
        "--bind=x:execute-silent(fleet-next dismiss-source {1})+reload-sync(fleet-next items)",
        "--bind=tab:execute-silent(tmux select-window -t fleet@muster:history)",
        "--bind=shift-tab:execute-silent(tmux select-window -t fleet@muster:history)",
        "--preview=fleet-next preview {1} $FZF_PREVIEW_COLUMNS $FZF_PREVIEW_LINES",
        "--preview-window=down,45%,nowrap,follow,border-none",
    ]
    os.execvp(command[0], command)


def header():
    _, usage, unavailable = ordered()
    empty = "5h [--------]   0%/0h  7d [--------]   0%/0h"
    offline = f"  |  offline {' '.join(unavailable)}" if unavailable else ""
    return (f"Claude {usage.get('claude', empty)}{offline}\n"
            f"OpenAI {usage.get('codex', empty)}\n"
            "N/L/B/T/OE  * working  ! needs action  Enter show  / search  Tab history  c create  r rename  d done  x dismiss")


def cursor():
    sessions, _, _ = ordered()
    return next((i for i, session in enumerate(sessions, 1)
                 if session.attention != "done" and session.state == "waiting"), 1)


def history():
    command = [
        "fzf", "--track", "--delimiter=\t", "--with-nth=2..",
        f"--color={FZF_COLOUR}",
        "--nth=2..", "--id-nth=1", "--layout=reverse", "--no-multi",
        "--header=History  Enter resurrect  Tab live",
        "--bind=enter:execute-silent(fleet-next resurrect {1})+reload-sync(fleet-next history-rows)",
        "--bind=tab:execute-silent(tmux select-window -t fleet@muster:live)",
        "--bind=shift-tab:execute-silent(tmux select-window -t fleet@muster:live)",
    ]
    os.execvp(command[0], command)
