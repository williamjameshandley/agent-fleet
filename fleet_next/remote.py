import os
import subprocess
import shlex

from .config import hosts
from .daemon import snapshot
from .protocol import decode, decode_message
from .tmux import inventory


def inventory_host():
    groups = []
    local = os.uname().nodename
    for host in hosts():
        if host == local:
            groups.append(inventory(host))
            continue
        result = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", host,
                                 shlex.join(("fleet-next", "snapshot", "--host", host))],
                                text=True, capture_output=True, check=True)
        groups.append(decode(result.stdout))
    return groups


def find(key, live=True):
    sessions, _, unavailable = decode_message(snapshot())
    for session in sessions:
        if session.ref.key == key:
            if live and session.ref.server.host in unavailable:
                raise SystemExit(f"{session.ref.server.host} is disconnected; refusing action")
            return session
    raise SystemExit(f"session disappeared: {key}")
