import json
import os
import shlex
import subprocess

import loop

from . import alan


def python_console(connection_file):
    os.execvp(
        "jupyter-console",
        ["jupyter-console", "--existing", str(connection_file)],
    )


def codex_console(actor, descriptor):
    native = alan.native_dir(actor)
    thread_id = (native / "thread_id").read_text()
    socket = alan.codex_socket(actor)
    os.chdir(descriptor["cwd"])
    if descriptor["capabilities"] == "full":
        os.execvp(
            "codex",
            ["codex", "resume", "--remote", f"unix://{socket}", thread_id,
             "--no-alt-screen"],
        )
        return

    cage = os.environ.get("LOOP_CODEX_CAGE", "/usr/lib/alan/alan-codex-cage")
    environment = {
        **os.environ,
        "LOOP_SOCKET": str(alan.actor_socket(actor)),
        "LOOP_CAPABILITIES": json.dumps("read"),
        "LOOP_CWD": descriptor["cwd"],
    }
    os.execve(
        cage,
        [
            cage,
            "--client",
            actor,
            str(native),
            str(socket.parent),
            str(alan.codex_gateway(actor)),
            str(socket),
            thread_id,
        ],
        environment,
    )


def attach(actor, descriptor):
    name = "fleet@alan-" + alan.runtime_name(actor)
    exists = subprocess.run(
        ["tmux", "has-session", "-t", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists.returncode:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", descriptor["cwd"],
             shlex.join(["fleet", "actor-view", actor])],
            check=True,
        )
        subprocess.run(["tmux", "set-option", "-t", name, "status", "off"],
                       check=True)
        subprocess.run(["tmux", "set-option", "-t", name, "mouse", "on"],
                       check=True)
    os.execvp("tmux", ["tmux", "attach-session", "-t", name])


def run(actor):
    descriptor = next((item for item in alan.actors()
                       if item["addr"] == actor), None)
    if descriptor is None:
        raise SystemExit(f"Alan actor disappeared: {actor}")
    if descriptor["state"] in {"retired", "unavailable"}:
        raise SystemExit(f"Alan actor is {descriptor['state']}: {actor}")

    if descriptor["kind"] == "python":
        python_console(alan.native_dir(actor) / "kernel.json")
        return
    if descriptor["kind"] == "codex":
        codex_console(actor, descriptor)
        return

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
