import os
import shutil
import shlex
import socket
import subprocess
import textwrap
import time

from .config import RUNTIME
from .daemon import request
from . import hot, proc


FZF_COLOUR = "16,fg:-1,bg:-1,fg+:-1,bg+:8,hl:3,hl+:3,info:4,prompt:2,pointer:1,marker:1,spinner:6,header:4,gutter:-1,border:8"


def prepare_socket(path):
    if not path.exists():
        return
    with socket.socket(socket.AF_UNIX) as client:
        try:
            client.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
            return
    raise RuntimeError(f"Muster listener already exists: {path}")


def register():
    result = subprocess.run(
        ["/usr/bin/tmux", "-N", "display-message", "-p", "-t", "=fleet@muster:",
         "#{socket_path}\t#{pid}\t#{session_id}"],
        capture_output=True, text=True, check=True)
    socket_path, pid, session_id = result.stdout.rstrip("\n").split("\t")
    started = proc.start_time(pid)
    width = shutil.get_terminal_size((100, 24)).columns
    reply = request("\t".join(
        ("muster-register", socket_path, pid, str(started), session_id, str(width))))
    if reply != "OK\n":
        raise RuntimeError(reply.strip() or "Muster registration failed")


def muster():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock = RUNTIME / "muster.sock"
    prepare_socket(sock)
    daemon_socket = shlex.quote(str(RUNTIME / "fleet.sock"))
    fold_open = ("if [ {3} -gt 0 ]; then printf 'fold\\topen\\t%s\\t%s\\t%s\\n' "
                 f"{{1}} {{2}} \"$FZF_COLUMNS\" | /usr/bin/nc -U {daemon_socket}; fi")
    fold_close = ("if [ {3} -gt 0 ]; then printf 'fold\\tclose\\t%s\\t%s\\t%s\\n' "
                  f"{{1}} {{2}} \"$FZF_COLUMNS\" | /usr/bin/nc -U {daemon_socket}; fi")
    toggle_python = ("printf 'toggle\\tpython\\t%s\\n' \"$FZF_COLUMNS\" | "
                     f"/usr/bin/nc -U {daemon_socket}")
    resize = ("printf 'resize\\t%s\\n' \"$FZF_COLUMNS\" | "
              f"/usr/bin/nc -U {daemon_socket}")
    archive = ("printf 'archive\\t%s\\t%s\\t%s\\n' {1} {2} "
               f"\"$FZF_COLUMNS\" | /usr/bin/nc -U {daemon_socket}")
    refresh = ("printf 'refresh\\t%s\\t%s\\t%s\\n' {1} {2} "
               f"\"$FZF_COLUMNS\" | /usr/bin/nc -U {daemon_socket}")
    command = [
        "fzf", "--listen", str(sock), "--track", "--disabled", "--no-input", "--ansi",
        f"--color={FZF_COLOUR}",
        "--no-unicode", "--pointer=>", "--gutter= ",
        "--no-scrollbar", "--no-hscroll",
        "--delimiter=\t", "--with-nth=4..", "--id-nth=1",
        "--layout=reverse", "--no-sort", "--no-multi", "--info=inline", "--border=none",
        f"--header={header()}",
        f"--footer={footer()}",
        "--footer-border=bottom",
        "--bind=start:unbind(esc)",
        "--bind=/:enable-search+toggle-sort+show-input+change-prompt(Search: )+unbind(/,c,r,R,d,x,h,j,k,l,p,left,right)+rebind(esc)",
        "--bind=esc:disable-search+toggle-sort+clear-query+hide-input+change-prompt(> )+unbind(esc)+rebind(/,c,r,R,d,x,h,j,k,l,p,left,right)",
        "--bind=j:down,k:up",
        "--bind=load:transform(/usr/lib/agent-fleet/ui cursor)+unbind(load)",
        f"--bind=resize:transform({resize})",
        "--bind=focus,result-final:execute-silent(exec /usr/lib/agent-fleet/fleet-open project main {1})",
        "--bind=enter:execute-silent(exec /usr/lib/agent-fleet/fleet-open focus main {1})",
        "--bind=double-click:execute-silent(exec /usr/lib/agent-fleet/fleet-open focus main {1})",
        "--bind=c:execute-silent(/usr/lib/agent-fleet/ui create-tab)",
        "--bind=r:execute-silent(/usr/lib/agent-fleet/ui rename-tab {1})",
        f"--bind=R:transform({refresh})",
        f"--bind=x:transform({archive})",
        f"--bind=l:transform({fold_open})",
        f"--bind=h:transform({fold_close})",
        f"--bind=right:transform({fold_open})",
        f"--bind=left:transform({fold_close})",
        f"--bind=p:transform({toggle_python})",
        "--bind=tab:execute-silent(/usr/bin/tmux -N select-window -t fleet@muster:history)",
        "--bind=shift-tab:execute-silent(/usr/bin/tmux -N select-window -t fleet@muster:history)",
    ]
    os.execvp(command[0], command)


def header():
    return hot.fetch("header").removesuffix("\n")


def footer():
    hints = ("Enter view  c create  r rename  R refresh  x archive  l open fold  h close fold  p python")
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
        "--header=History  Enter open  s search  Tab live",
        "--bind=enter:execute-silent(/usr/lib/agent-fleet/ui open-history {1})+reload-sync(/usr/lib/agent-fleet/ui history-rows)",
        "--bind=s:execute(/usr/lib/agent-fleet/ui search-history)",
        "--bind=tab:execute-silent(/usr/bin/tmux -N select-window -t fleet@muster:live)",
        "--bind=shift-tab:execute-silent(/usr/bin/tmux -N select-window -t fleet@muster:live)",
    ]
    os.execvp(command[0], command)


def search_history(query):
    command = [
        "fzf", "--track", "--delimiter=\t", "--with-nth=2..",
        f"--color={FZF_COLOUR}", "--id-nth=1", "--layout=reverse",
        "--no-sort", "--no-multi", "--header=Search history  Enter open",
    ]
    rows = subprocess.run(
        ["/usr/lib/agent-fleet/ui", "search-history-rows", query],
        text=True, capture_output=True, check=True).stdout
    selected = subprocess.run(command, input=rows, text=True, capture_output=True)
    if selected.returncode == 0:
        from .actions import open_history
        open_history(selected.stdout.split("\t", 1)[0])
