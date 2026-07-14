import os
import time
import shutil
import re

from .config import RUNTIME, machine
from .daemon import snapshot
from .protocol import decode_message
from . import viewer


STATE_ORDER = {"working": 0, "needs-action": 1, "waiting": 2, "finished": 3}


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
        visible = (f"{machine(session.ref.server.host):<2} {marker} {session.name:<20.20} "
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
        "fzf", "--listen", str(sock), "--track", "--disabled", "--no-input",
        "--no-unicode", "--pointer=>", "--gutter= ",
        "--no-scrollbar", "--no-hscroll",
        "--delimiter=\t", "--with-nth=2..", "--nth=2..", "--id-nth=1",
        "--layout=reverse", "--no-multi", "--info=inline", "--border=none",
        f"--header={header()}",
        "--bind=/:enable-search+show-input+unbind(c,r,d,x)",
        "--bind=esc:disable-search+clear-query+hide-input+rebind(c,r,d,x)",
        f"--bind=load:pos({cursor()})",
        "--bind=enter:execute-silent(fleet-next show {1})",
        "--bind=left-click:execute-silent(fleet-next show {1})",
        "--bind=double-click:execute-silent(fleet-next show {1})",
        "--bind=c:execute(fleet-next create)+reload(fleet-next items)",
        "--bind=r:execute(fleet-next rename {1})+reload(fleet-next items)",
        "--bind=d:execute-silent(fleet-next done {1})+reload(fleet-next items)",
        "--bind=x:execute-silent(fleet-next dismiss-source {1})+reload-sync(fleet-next items)",
        "--bind=tab:execute-silent(tmux select-window -t =fleet@muster:history)",
        "--bind=shift-tab:execute-silent(tmux select-window -t =fleet@muster:history)",
        "--preview=fleet-next preview {1}", "--preview-window=down,45%,wrap",
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
        "--nth=2..", "--id-nth=1", "--layout=reverse", "--no-multi",
        "--header=History  Enter resurrect  Tab live",
        "--bind=enter:execute(fleet-next resurrect {1})+reload(fleet-next history-rows)",
        "--bind=tab:execute-silent(tmux select-window -t =fleet@muster:live)",
        "--bind=shift-tab:execute-silent(tmux select-window -t =fleet@muster:live)",
    ]
    os.execvp(command[0], command)
