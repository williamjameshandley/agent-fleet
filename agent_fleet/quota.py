import os
import subprocess
import sys
import time

from .config import RUNTIME, hosts
from .usage import read as usage


def tmux(*args):
    return subprocess.run(["tmux", *args], text=True, capture_output=True)


def tmux_check(*args):
    return subprocess.run(["tmux", *args], text=True, capture_output=True, check=True)


def read():
    values = {}
    for agent in ("claude", "codex"):
        result = tmux("show-options", "-gv", f"@fleet_{agent}_usage")
        if result.returncode == 0 and result.stdout.strip():
            values[agent] = result.stdout.strip()
    return values


def update():
    if os.uname().nodename != hosts()[0]:
        raise RuntimeError("quota collection runs only on the first fleet host")
    errors = []
    for agent in ("claude", "codex"):
        option = f"@fleet_{agent}_retry_after"
        retry = tmux("show-options", "-gv", option).stdout.strip()
        if retry and int(retry) > time.time():
            tmux_check("set-option", "-g", f"@fleet_{agent}_usage", "unavailable")
            continue
        try:
            value = usage(agent)
        except RuntimeError as error:
            message = str(error)
            if "retry-at=" in message:
                retry_at = message.rsplit("retry-at=", 1)[1].split()[0]
                tmux_check("set-option", "-g", option, retry_at)
            tmux_check("set-option", "-g", f"@fleet_{agent}_usage", "unavailable")
            errors.append(message)
            continue
        tmux_check("set-option", "-g", f"@fleet_{agent}_usage", value)
        tmux("set-option", "-gu", option)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    (RUNTIME / "quota.changed").touch()
    for error in errors:
        print(error, file=sys.stderr)
    return errors
