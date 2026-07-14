import argparse
import asyncio
import json
import sys
import threading

from . import actions, ui, viewer
from .daemon import Fleet
from .protocol import encode
from .quota import read as quota_read, update as quota_update
from .tmux import capture, event_stream, inventory, mutate
from .config import RUNTIME, hosts


def events(args):
    lock = threading.Lock()

    def emit(message):
        with lock:
            print(message, flush=True)

    def requests():
        for line in sys.stdin:
            request = json.loads(line)
            try:
                text = capture(request["key"])
                response = {"preview": request["preview"], "text": text}
            except RuntimeError as error:
                response = {"preview": request["preview"], "error": str(error)}
            emit(json.dumps(response, separators=(",", ":")))

    threading.Thread(target=requests, daemon=True).start()
    for sessions in event_stream(args.host):
        usage = quota_read() if args.host == hosts()[0] else {}
        emit(encode(sessions, usage))


def snapshot(args):
    usage = quota_read() if args.host == hosts()[0] else {}
    print(encode(inventory(args.host), usage))


def main():
    parser = argparse.ArgumentParser(prog="fleet-next")
    commands = parser.add_subparsers(required=True)

    def command(name, fn):
        item = commands.add_parser(name)
        item.set_defaults(fn=fn)
        return item

    for name, fn in (("events", events), ("snapshot", snapshot)):
        item = command(name, fn)
        item.add_argument("--host", required=True)
    command("serve", lambda _: asyncio.run(Fleet().serve()))
    command("quota", lambda _: quota_update())
    command("rows", lambda _: ui.rows())
    command("items", lambda _: ui.rows(include_header=False))
    command("header", lambda _: print(ui.header()))
    command("muster", lambda _: ui.muster())
    command("history-ui", lambda _: ui.history())
    command("history-rows", lambda _: actions.history())
    item = command("resurrect", lambda a: actions.resurrect(a.key))
    item.add_argument("key")
    item = command("arrive", lambda a: actions.arrive(a.profile, a.available))
    item.add_argument("profile", choices=("laptop", "home", "office"))
    item.add_argument("--available", action="store_true")
    command("context", lambda _: actions.context())
    item = command("mutate", lambda a: mutate(a.key, a.operation, a.arguments))
    item.add_argument("key")
    item.add_argument("operation")
    item.add_argument("arguments", nargs="*")
    command("signal", lambda _: (RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True),
                                  (RUNTIME / "fleet.changed").touch()))
    item = command("viewer", lambda a: viewer.serve(a.slot))
    item.add_argument("--slot", default="main")
    item = command("show", lambda a: viewer.show(a.key, a.slot))
    item.add_argument("key")
    item.add_argument("--slot")
    item = command("dismiss", lambda a: viewer.request(a.slot, ""))
    item.add_argument("--slot", default="main")
    item = command("attach", lambda a: viewer.attach(a.key))
    item.add_argument("key")
    command("create", lambda _: actions.create())
    for name, fn in (("rename", actions.rename), ("done", actions.done),
                     ("preview", actions.preview),
                     ("dismiss-source", actions.dismiss_source)):
        item = command(name, lambda a, fn=fn: fn(a.key))
        item.add_argument("key")
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
