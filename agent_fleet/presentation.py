import os
import json
import shlex
import subprocess
from pathlib import Path

import loop
from jupyter_console.app import ZMQTerminalIPythonApp

from . import alan


def available(actor, descriptor, session_names):
    if descriptor["kind"] not in {"claude", "codex", "grok", "python", "llm", "antigravity"}:
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
    return descriptor["kind"] in {"llm", "antigravity"}


class PythonConsole(ZMQTerminalIPythonApp):
    actor = None

    def handle_sigint(self, *args):
        if self.shell._executing:
            loop.control(self.actor, "interrupt")
        else:
            super().handle_sigint(*args)


# A console shows only what it witnesses, so a session's past arrives as
# scrollback from its operation stream before the live prompt.
TRANSCRIPT_RECORDS = 400


def cell_record(native):
    """Resolve a console-entered cell's rendering from the evidence it names.

    An output which claims cell evidence must have it: a reference the file
    cannot satisfy is drift, not an absent rendering.
    """
    cells, line = native.get("cells"), native.get("cell")
    if cells is None and line is None:
        return None
    if cells is None or line is None:
        raise RuntimeError(f"incomplete cell reference: {native}")
    with open(cells, encoding="utf-8") as stream:
        for index, record in enumerate(stream):
            if index == line:
                return json.loads(record)
    raise RuntimeError(f"cell evidence {cells} has no line {line}")


def transcript(actor, records=TRANSCRIPT_RECORDS):
    """Render an actor's past as the lines a console would have shown."""
    session = loop.session(actor)
    total = len(session)
    start = max(0, total - records)
    if start:
        yield f"[{start} earlier operations not shown]"

    answered = False
    for record in session[start:total]:
        payload = record.get("payload") or {}

        if record["op"] == "evaluation":
            answered = False

        elif record["op"] == "control" and record.get("operation") == "reset":
            yield "── namespace reset: this kernel replaced the one before it ──"

        elif record["op"] == "input" and payload.get("kind") == "prompt":
            yield "In : " + payload["text"]

        elif record["op"] == "input" and payload.get("kind") == "error":
            yield "In : " + json.dumps(payload, separators=(",", ":"))

        # An Alan-driven cell's rendering travels back to its requester, while a
        # console-entered cell leaves its own evidence for the output to name.
        elif record["op"] == "send" and record.get("reply"):
            answered = True
            yield payload.get("text") or payload.get("error", "")

        elif record["op"] == "output":
            cell = cell_record(record.get("native") or {})
            if cell:
                for stream in ("stdout", "stderr"):
                    if cell[stream]:
                        yield cell[stream].rstrip("\n")
                if cell["result"] is not None:
                    yield "Out: " + cell["result"]
                if cell["error"]:
                    yield cell["error"]
            # An answered evaluation closes carrying the rendering its reply
            # already delivered; one with no requester carries it alone.
            elif answered:
                pass
            elif record.get("value") is not None:
                yield "Out: " + record["value"]
            elif record.get("error"):
                yield record["error"]


def python_console(actor, connection_file):
    for line in transcript(actor):
        print(line, flush=True)
    print(f"── live: {connection_file} ──", flush=True)
    console = PythonConsole.instance()
    console.actor = actor
    console.initialize([
        "--existing",
        str(connection_file),
        "--ZMQTerminalInteractiveShell.include_other_output=True",
    ])
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
        if descriptor["kind"] in {"claude", "codex", "grok"}:
            raise RuntimeError(
                f"{descriptor['kind'].capitalize()} evaluator terminal is unavailable: {actor}"
            )
        if descriptor["kind"] == "llm":
            command = shlex.join(["/usr/bin/alan", actor])
        else:
            command = shlex.join([
                "/usr/bin/python", "-c",
                "import json,sys; from agent_fleet.presentation import run; "
                "run(sys.argv[1], json.loads(sys.argv[2]))",
                actor, json.dumps(descriptor, separators=(",", ":")),
            ])
        subprocess.run(
            ["/usr/bin/tmux", "-N", "new-session", "-d", "-s", name, "-c", descriptor["cwd"],
             command],
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
    if not actor.startswith(("llm-", "antigravity-")):
        raise RuntimeError("Fleet owns only conversational actor presentations")
    name = "fleet@alan-" + alan.runtime_name(actor)
    target = "=" + name
    result = subprocess.run(
        ["/usr/bin/tmux", "-N", "kill-session", "-t", target], text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if result.returncode and result.stderr.strip() != f"can't find session: {name}":
        result.check_returncode()


def run(actor, descriptor):
    if descriptor["kind"] == "python":
        python_console(actor, alan.native_dir(actor) / "kernel.json")
        return
    if descriptor["kind"] != "antigravity":
        raise SystemExit(f"{descriptor['kind']} has no Fleet-owned presentation")

    observations = loop.observe(stream=True, actor=actor)
    try:
        while True:
            try:
                text = input("> ")
            except EOFError:
                print()
                return
            if not text:
                continue
            result = loop.send(actor, {"kind": "prompt", "text": text})
            try:
                output = alan.wait_output(actor, result["result"], observations)
            except KeyboardInterrupt:
                loop.control(actor, "interrupt")
                output = alan.wait_output(actor, result["result"], observations)
            print(output.get("value", output.get("error", output["status"])), flush=True)
    finally:
        observations.close()
