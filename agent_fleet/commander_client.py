import json
import uuid

import loop

from . import alan
from .commander import validate_proposal
from .daemon import commander_context


def render(output, request):
    if output["status"] != "ok":
        print(output.get("error", output["status"]), flush=True)
        return
    text = output["value"]
    try:
        proposal = json.loads(text)
        validate_proposal(proposal, request)
    except (json.JSONDecodeError, TypeError, ValueError):
        print(text, flush=True)
    else:
        print(json.dumps(proposal, indent=2, ensure_ascii=False), flush=True)


def exchange(text):
    snapshot = json.loads(commander_context())
    request = {"kind": "commander_request", "version": 1,
               "request_id": str(uuid.uuid4()), "text": text, "snapshot": snapshot}
    actor = alan.commander_actor()
    result = loop.send(actor, {
        "kind": "prompt",
        "text": json.dumps(request, separators=(",", ":"), ensure_ascii=False),
    })
    accepted = loop.observe().nodes[result["result"]]
    output = alan.wait_output(accepted["input"])
    render(output, request)


def run():
    while True:
        try:
            text = input("Commander> ").strip()
        except EOFError:
            print()
            return
        if text:
            exchange(text)
