import argparse
import asyncio
import json
import sys
import threading

from . import actions, ui, viewer, workstation
from .commander_client import run as commander
from .daemon import Fleet, projection
from .protocol import encode
from .quota import read as quota_read, update as quota_update
from .tmux import capture, event_stream, inventory, mutate
from .config import hosts
from .transcripts import history as transcript_history, resume, verify as transcript_verify
from .alan import (actors as alan_actors, retire as alan_retire, resume as alan_resume,
                   rename as alan_rename, present as alan_present)


def events(args):
    lock = threading.Lock()
    consumer = threading.Event()

    def emit(message):
        with lock:
            print(message, flush=True)

    def requests():
        try:
            for line in sys.stdin:
                request = json.loads(line)
                try:
                    text = capture(request["key"], request["columns"], request["lines"])
                    response = {"preview": request["preview"], "text": text}
                except RuntimeError as error:
                    response = {"preview": request["preview"], "error": str(error)}
                emit(json.dumps(response, separators=(",", ":")))
        finally:
            consumer.set()

    threading.Thread(target=requests, daemon=True).start()
    for sessions in event_stream(args.host, consumer):
        usage = quota_read() if args.host == hosts()[0] else {}
        emit(encode(sessions, usage))


def snapshot(args):
    usage = quota_read() if args.host == hosts()[0] else {}
    print(encode(inventory(args.host), usage))


def main():
    parser = argparse.ArgumentParser(prog="fleet")
    commands = parser.add_subparsers(required=True)

    def command(name, fn):
        item = commands.add_parser(name)
        item.set_defaults(fn=fn)
        return item

    for name, fn in (("events", events), ("snapshot", snapshot)):
        item = command(name, fn)
        item.add_argument("--host", required=True)
    command("serve", lambda _: asyncio.run(Fleet().serve()))
    command("projection", lambda _: print(projection(), end=""))
    command("quota", lambda _: quota_update())
    command("rows", lambda _: ui.rows())
    command("items", lambda _: ui.rows(include_header=False))
    command("header", lambda _: print(ui.header()))
    command("cursor", lambda _: print(ui.cursor(), end=""))
    command("muster", lambda _: ui.muster())
    command("history-ui", lambda _: ui.history())
    command("history-rows", lambda _: actions.history())
    item = command("transcripts", lambda a: print(json.dumps(transcript_history(a.limit))))
    item.add_argument("--limit", type=int, default=100)
    item = command("transcript-check", lambda a: transcript_verify(a.agent, a.session))
    item.add_argument("agent", choices=("claude", "codex"))
    item.add_argument("session")
    item = command("resume", lambda a: resume(a.agent, a.session, a.name))
    item.add_argument("agent", choices=("claude", "codex"))
    item.add_argument("session")
    item.add_argument("name")
    item = command("open-history", lambda a: actions.open_history_report(a.key))
    item.add_argument("key")
    item = command("refresh", lambda a: actions.refresh_command(a.key, a.all_sessions))
    target = item.add_mutually_exclusive_group(required=True)
    target.add_argument("key", nargs="?")
    target.add_argument("--all", dest="all_sessions", action="store_true")
    item = command("refresh-local", lambda a: actions.refresh_local(a.key))
    item.add_argument("key")
    item = command("refresh-check", lambda a: actions.refresh_check(a.key, a.native_id))
    item.add_argument("key")
    item.add_argument("native_id")
    item = command("arrive", lambda a: actions.arrive(a.profile, a.available))
    item.add_argument("profile", choices=("laptop", "home", "office"))
    item.add_argument("--available", action="store_true")
    command("context", lambda _: actions.context())
    command("commander-context", lambda _: actions.commander_context())
    command("commander", lambda _: commander())
    item = command("mutate", lambda a: mutate(a.key, a.operation, a.arguments))
    item.add_argument("key")
    item.add_argument("operation")
    item.add_argument("arguments", nargs="*")
    item = command("workstation", lambda a: workstation.serve(a.socket))
    item.add_argument("--socket", required=True)
    item = command("viewer", lambda a: viewer.serve(a.slot))
    item.add_argument("--slot", default="main")
    item = command("viewer-status", lambda a: print(viewer.exchange(a.slot, "STATUS")))
    item.add_argument("slot", nargs="?", default="main")
    item = command("show", lambda a: viewer.show(a.key, a.slot))
    item.add_argument("key")
    item.add_argument("--slot")
    item = command("attach", lambda a: viewer.attach(a.key))
    item.add_argument("key")
    command("create", lambda _: actions.create())
    command("create-tab", lambda _: actions.create_tab())
    item = command("rename-tab", lambda a: actions.rename_tab(a.key))
    item.add_argument("key")
    item = command("alan-rename", lambda a: alan_rename(a.addr, a.label))
    item.add_argument("addr")
    item.add_argument("label")
    command("alan-actors", lambda _: print(json.dumps(alan_actors())))
    item = command("alan-retire", lambda a: alan_retire(a.addr))
    item.add_argument("addr")
    item = command("alan-resume", lambda a: print(alan_resume(a.addr)))
    item.add_argument("addr")
    item = command("alan-present", lambda a: print(json.dumps(alan_present(a.addr))))
    item.add_argument("addr")
    command("next-waiting", lambda _: actions.next_waiting())
    for name, fn in (("rename", actions.rename), ("archive", actions.archive_report),
                     ):
        item = command(name, lambda a, fn=fn: fn(a.key))
        item.add_argument("key")
    item = command("preview", lambda a: actions.preview(a.key, a.columns, a.lines))
    item.add_argument("key")
    item.add_argument("columns", type=int, nargs="?", default=0)
    item.add_argument("lines", type=int, nargs="?", default=0)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
