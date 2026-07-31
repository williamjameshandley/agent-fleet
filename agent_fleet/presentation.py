import json
import os

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


def codex_console(actor, descriptor):
    native = alan.native_dir(actor)
    thread_id = (native / "thread_id").read_text()
    socket = alan.codex_socket(actor)
    os.chdir(descriptor["cwd"])
    if descriptor["capabilities"] == "full":
        os.execvp(
            "codex",
            ["codex", "resume", "--remote", f"unix://{socket}", thread_id],
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


def run(actor):
    descriptor = next((item for item in alan.actors()
                       if item["addr"] == actor), None)
    if descriptor is None:
        raise SystemExit(f"Alan actor disappeared: {actor}")
    if descriptor["state"] in {"retired", "unavailable"}:
        raise SystemExit(f"Alan actor is {descriptor['state']}: {actor}")

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
