import subprocess
import os
import shlex
from uuid import uuid4


def send(destination, text):
    name = "alan-" + uuid4().hex
    command = [
        "tmux", "load-buffer", "-b", name, "-",
        ";", "paste-buffer", "-b", name, "-d", "-t", destination.pane_id,
        ";", "send-keys", "-t", destination.pane_id, "Enter",
    ]
    if destination.host != os.uname().nodename:
        command = ["ssh", "-T", "-o", "BatchMode=yes", destination.host,
                   shlex.join(command)]
    subprocess.run(command, input=text, text=True, check=True)
    subprocess.run(["xdotool", "windowactivate", "--sync", str(destination.window_id)], check=True)
