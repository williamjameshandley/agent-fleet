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
