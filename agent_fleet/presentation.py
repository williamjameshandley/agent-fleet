import os
import shlex
import subprocess
from pathlib import Path

import loop
from jupyter_console.app import ZMQTerminalIPythonApp

from . import alan


def available(actor, descriptor, session_names):
    if descriptor["kind"] not in {"claude", "codex", "python", "llm"}:
        return False
    name = "fleet@alan-" + alan.runtime_name(actor)
    matches = session_names.count(name)
    if matches:
        return matches == 1
    cwd = descriptor.get("cwd")
    if not cwd or not Path(cwd).is_dir():
        return False
    if descriptor["kind"] == "python":
        return (alan.native_dir(actor) / "kernel.json").is_file()
    return descriptor["kind"] == "llm"


class PythonConsole(ZMQTerminalIPythonApp):
    actor = None

    def handle_sigint(self, *args):
        if self.shell._executing:
            loop.control(self.actor, "interrupt")
        else:
            super().handle_sigint(*args)


def python_console(actor, connection_file):
    console = PythonConsole.instance()
    console.actor = actor
    console.initialize(["--existing", str(connection_file)])
    console.start()


def target(actor, descriptor):
    name = "fleet@alan-" + alan.runtime_name(actor)
    exact = "=" + name
    exists = subprocess.run(
        ["/usr/bin/tmux", "-N", "has-session", "-t", exact],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists.returncode:
        if descriptor["kind"] in {"claude", "codex"}:
            raise RuntimeError(
                f"{descriptor['kind'].capitalize()} evaluator terminal is unavailable: {actor}"
            )
        subprocess.run(
            ["/usr/bin/tmux", "-N", "new-session", "-d", "-s", name, "-c", descriptor["cwd"],
             shlex.join([
                 "/usr/bin/python", "-c",
                 "import sys; from agent_fleet.presentation import run; run(sys.argv[1])",
                 actor,
             ])],
            check=True,
        )
        subprocess.run(["/usr/bin/tmux", "-N", "set-option", "-t", name, "mouse", "on"],
                       check=True)
    subprocess.run(["/usr/bin/tmux", "-N", "set-option", "-t", name, "status", "on"],
                   check=True)
    return exact


def attach(actor, descriptor):
    exact = target(actor, descriptor)
    os.execvp("/usr/bin/tmux", ["/usr/bin/tmux", "-N", "attach-session", "-t", exact])


def close(actor):
    if not actor.startswith("llm-"):
        raise RuntimeError("Fleet owns only bare-model actor presentations")
    name = "fleet@alan-" + alan.runtime_name(actor)
    target = "=" + name
    result = subprocess.run(
        ["/usr/bin/tmux", "-N", "kill-session", "-t", target], text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if result.returncode and result.stderr.strip() != f"can't find session: {name}":
        result.check_returncode()


def refresh(actor):
    descriptor = next((item for item in alan.actors()
                       if item["addr"] == actor), None)
    if descriptor is None:
        raise RuntimeError(f"Alan actor disappeared: {actor}")
    if descriptor["kind"] not in {"claude", "codex"}:
        raise RuntimeError("refresh requires a Claude or Codex actor")
    if descriptor["state"] != "waiting":
        raise RuntimeError(f"refresh requires a waiting actor: {actor}")
    alan.retire(actor)
    alan.resume(actor)


def run(actor):
    descriptor = next((item for item in alan.actors()
                       if item["addr"] == actor), None)
    if descriptor is None:
        raise SystemExit(f"Alan actor disappeared: {actor}")
    if descriptor["state"] in {"retired", "unavailable"}:
        raise SystemExit(f"Alan actor is {descriptor['state']}: {actor}")

    if descriptor["kind"] == "python":
        python_console(actor, alan.native_dir(actor) / "kernel.json")
        return
    if descriptor["kind"] != "llm":
        raise SystemExit(f"{descriptor['kind']} has no Fleet-owned presentation")

    while True:
        try:
            text = input("> ")
        except EOFError:
            print()
            return
        if not text:
            continue
        result = loop.send(actor, {"kind": "message", "text": text})
        try:
            output = alan.wait_output(result["input"])
        except KeyboardInterrupt:
            loop.control(actor, "interrupt")
            output = alan.wait_output(result["input"])
        print(output.get("value", output.get("error", output["status"])), flush=True)
