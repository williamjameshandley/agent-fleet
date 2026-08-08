import os
import shutil
import subprocess
import textwrap
import time

from .config import RUNTIME
from .daemon import snapshot
from .protocol import decode_graph, decode_message
from . import hot, render


FZF_COLOUR = "16,fg:-1,bg:-1,fg+:-1,bg+:8,hl:3,hl+:3,info:4,prompt:2,pointer:1,marker:1,spinner:6,header:4,gutter:-1,border:8"
def ordered():
    raw = snapshot()
    sessions, usage, unavailable = decode_message(raw)
    graph = decode_graph(raw)
    projected = render.order(sessions, unavailable, graph,
                             expanded=expanded(),
                             show_python=option("@fleet_show_python"))
    return projected, usage, unavailable


def option(name):
    result = subprocess.run(
        ["tmux", "show-options", "-qv", "-t", "=fleet@muster:", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "1"


def toggle(kind):
    name = {"python": "@fleet_show_python"}[kind]
    subprocess.run(
        ["tmux", "set-option", "-t", "=fleet@muster:", name, "0" if option(name) else "1"],
        check=True,
    )


def expanded():
    result = subprocess.run(
        ["tmux", "show-options", "-qv", "-t", "=fleet@muster:", "@fleet_expanded"],
        capture_output=True,
        text=True,
    )
    return set(result.stdout.split())


def fold(action, key):
    projected, _, _ = ordered()
    [projection] = [item for item in projected if item.session.ref.key == key]
    session = projection.session
    if session.ref.server.kind != "alan" or not projection.child_count:
        return
    actors = expanded()
    actor = session.ref.session_id
    if action == "open":
        actors.add(actor)
    else:
        actors.discard(actor)
    subprocess.run(
        ["tmux", "set-option", "-t", "=fleet@muster:", "@fleet_expanded",
         " ".join(sorted(actors))],
        check=True,
    )


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
        "--bind=/:enable-search+toggle-sort+show-input+change-prompt(Search: )+unbind(/,c,r,R,d,x,h,j,k,l,p)+rebind(esc)",
        "--bind=esc:disable-search+toggle-sort+clear-query+hide-input+change-prompt(> )+unbind(esc)+rebind(/,c,r,R,d,x,h,j,k,l,p)",
        "--bind=j:down,k:up",
        "--bind=load:transform(/usr/lib/agent-fleet/ui cursor)+unbind(load)",
        "--bind=enter:execute-silent(/usr/lib/agent-fleet/ui show --slot main {1})",
        "--bind=left-click:execute-silent(/usr/lib/agent-fleet/ui show --slot main {1})",
        "--bind=double-click:execute-silent(/usr/lib/agent-fleet/ui show --slot main {1})",
        "--bind=c:execute-silent(/usr/lib/agent-fleet/ui create-tab)",
        "--bind=r:execute-silent(/usr/lib/agent-fleet/ui rename-tab {1})",
        "--bind=R:execute-silent(/usr/lib/agent-fleet/ui refresh {1})+reload-sync(/usr/lib/agent-fleet/ui items)",
        "--bind=x:execute-silent(/usr/lib/agent-fleet/ui archive {1})+reload-sync(/usr/lib/agent-fleet/ui items)",
        "--bind=l:execute-silent(/usr/lib/agent-fleet/ui fold open {1})+transform-header(/usr/lib/agent-fleet/ui header)+reload-sync(/usr/lib/agent-fleet/ui items)",
        "--bind=h:execute-silent(/usr/lib/agent-fleet/ui fold close {1})+transform-header(/usr/lib/agent-fleet/ui header)+reload-sync(/usr/lib/agent-fleet/ui items)",
        "--bind=p:execute-silent(/usr/lib/agent-fleet/ui toggle python)+transform-header(/usr/lib/agent-fleet/ui header)+reload-sync(/usr/lib/agent-fleet/ui items)",
        "--bind=tab:execute-silent(tmux select-window -t fleet@muster:history)",
        "--bind=shift-tab:execute-silent(tmux select-window -t fleet@muster:history)",
        "--preview=/usr/lib/agent-fleet/ui preview {1} $FZF_PREVIEW_COLUMNS $FZF_PREVIEW_LINES",
        "--preview-window=down,45%,nowrap,follow,border-none",
    ]
    os.execvp(command[0], command)


def header():
    return hot.fetch("header").removesuffix("\n")


def footer():
    hints = ("Enter open  c create  r rename  R refresh  x archive  l open fold  h close fold  p python")
    width = max(1, shutil.get_terminal_size((100, 24)).columns - 2)
    return textwrap.fill(hints, width=width, break_long_words=False,
                         break_on_hyphens=False)


def select():
    path = RUNTIME / "muster.sock"
    if not path.exists():
        return
    # A reload arriving alongside the placement discards it, so assert the
    # position again once that reload has settled.
    for attempt in range(2):
        if attempt:
            time.sleep(.3)
        subprocess.run(
            ["curl", "-fsS", "--max-time", "2", "--unix-socket", str(path),
             "-XPOST", "-d", "transform(/usr/lib/agent-fleet/ui cursor)", "http://localhost"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )


def history():
    command = [
        "fzf", "--track", "--delimiter=\t", "--with-nth=2..",
        f"--color={FZF_COLOUR}",
        "--id-nth=1", "--layout=reverse", "--no-sort", "--no-multi",
        "--header=History  Enter open  Tab live",
        "--bind=enter:execute-silent(/usr/lib/agent-fleet/ui open-history {1})+reload-sync(/usr/lib/agent-fleet/ui history-rows)",
        "--bind=tab:execute-silent(tmux select-window -t fleet@muster:live)",
        "--bind=shift-tab:execute-silent(tmux select-window -t fleet@muster:live)",
    ]
    os.execvp(command[0], command)
