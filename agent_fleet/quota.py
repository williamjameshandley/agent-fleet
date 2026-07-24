import os
import subprocess
import time

from .config import RUNTIME, hosts


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
        raise SystemExit("quota collection runs only on the first fleet host")
    for agent in ("claude", "codex"):
        option = f"@fleet_{agent}_retry_after"
        retry = tmux("show-options", "-gv", option).stdout.strip()
        if retry and int(retry) > time.time():
            continue
        result = subprocess.run(["fleet-usage", agent], text=True, capture_output=True)
        if result.returncode:
            if "retry-at=" in result.stderr:
                retry_at = result.stderr.rsplit("retry-at=", 1)[1].split()[0]
                tmux_check("set-option", "-g", option, retry_at)
            raise SystemExit(result.stderr.strip())
        tmux_check("set-option", "-g", f"@fleet_{agent}_usage", result.stdout.strip())
        tmux("set-option", "-gu", option)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    (RUNTIME / "quota.changed").touch()
