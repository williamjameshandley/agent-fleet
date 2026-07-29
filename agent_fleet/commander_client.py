import json
import queue
import threading
import uuid

import loop

from . import alan
from .commander import validate_proposal
from .daemon import commander_context


def tail(addr, after, wait_ms=0):
    return loop.tail(addr, after=after, limit=100, wait_ms=wait_ms)


def current_end(addr):
    return loop.tail_end(addr)


def related(envelope, root):
    return envelope["id"] == root or envelope.get("parent") == root


def render(envelope, request):
    payload = envelope["payload"]
    if payload["kind"] in {"message", "llm_text"}:
        text = payload["text"]
        try:
            proposal = json.loads(text)
            validate_proposal(proposal, request)
        except (json.JSONDecodeError, TypeError, ValueError):
            print(text, flush=True)
        else:
            print(json.dumps(proposal, indent=2, ensure_ascii=False), flush=True)
    elif payload["kind"] == "error":
        print(f'{payload["of"]}: {payload["reason"]}', flush=True)
    return payload["kind"] in {"finished", "error"}


def watch(addr, after, output, stop):
    try:
        while not stop.is_set():
            for envelope in tail(addr, after, 30_000):
                after = envelope["idx"]
                output.put(envelope)
    except Exception as error:
        output.put(error)


def exchange(text):
    snapshot = json.loads(commander_context())
    request = {"kind": "commander_request", "version": 1,
               "request_id": str(uuid.uuid4()), "text": text, "snapshot": snapshot}
    requester = alan.peer()
    requester_after = current_end(requester)
    result = alan.commander_request(request)
    root = result["envelope_id"]

    output = queue.Queue()
    stop = threading.Event()
    watcher = threading.Thread(target=watch,
                               args=(requester, requester_after, output, stop), daemon=True)
    watcher.start()
    try:
        while True:
            envelope = output.get()
            if isinstance(envelope, Exception):
                raise envelope
            if related(envelope, root) and render(envelope, request):
                return
    finally:
        stop.set()


def run():
    while True:
        try:
            text = input("Commander> ").strip()
        except EOFError:
            print()
            return
        if text:
            exchange(text)
