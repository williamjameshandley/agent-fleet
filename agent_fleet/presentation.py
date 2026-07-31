import loop

from . import alan


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
        payload = ({"kind": "exec", "code": text}
                   if descriptor["kind"] == "python"
                   else {"kind": "message", "text": text})
        result = loop.send(actor, payload)
        try:
            output = alan.wait_output(result["input"])
        except KeyboardInterrupt:
            loop.control(actor, "interrupt")
            output = alan.wait_output(result["input"])
        print(output.get("value", output.get("error", output["status"])), flush=True)
