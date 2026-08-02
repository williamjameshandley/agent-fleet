import os
import shlex
import subprocess

import loop
from jupyter_console.app import ZMQTerminalIPythonApp

from . import alan


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


def attach(actor, descriptor):
    name = "fleet@alan-" + alan.runtime_name(actor)
    target = "=" + name
    exists = subprocess.run(
        ["tmux", "has-session", "-t", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists.returncode:
        if descriptor["kind"] in {"claude", "codex"}:
            raise RuntimeError(
                f"{descriptor['kind'].capitalize()} evaluator terminal is unavailable: {actor}"
            )
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", descriptor["cwd"],
             shlex.join(["fleet", "actor-view", actor])],
            check=True,
        )
        subprocess.run(["tmux", "set-option", "-t", name, "status", "off"],
                       check=True)
        subprocess.run(["tmux", "set-option", "-t", name, "mouse", "on"],
                       check=True)
    os.execvp("tmux", ["tmux", "attach-session", "-t", target])


def close(actor):
    if not actor.startswith("llm-"):
        raise RuntimeError("Fleet owns only bare-model actor presentations")
    name = "fleet@alan-" + alan.runtime_name(actor)
    target = "=" + name
    result = subprocess.run(
        ["tmux", "kill-session", "-t", target], text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if result.returncode and result.stderr.strip() != f"can't find session: {name}":
        result.check_returncode()


def refresh(actor):
    descriptor = next((item for item in alan.actors()
                       if item["addr"] == actor), None)
    if descriptor is None:
        raise RuntimeError(f"Alan actor disappeared: {actor}")
    native = descriptor.get("native") or {}
    if descriptor["kind"] not in {"claude", "codex"} or not native.get("id"):
        raise RuntimeError("refresh requires a durable Claude or Codex identity")
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
