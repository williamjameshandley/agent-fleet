import json
import os
import re
import socket
import subprocess
from pathlib import Path


NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def path(name):
    if not NAME.fullmatch(name):
        raise RuntimeError(f"invalid workstation name {name!r}")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / f"fleet-workstation-{name}.sock"


def request(name, message):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path(name)))
        client.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())
        reply = json.loads(client.makefile().readline())
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply.get("result")


def dispatch(message):
    if not isinstance(message, dict):
        raise RuntimeError("invalid workstation request")
    operation = message.get("operation")
    if operation == "focus":
        slot = message.get("slot")
        if not NAME.fullmatch(slot or ""):
            raise RuntimeError(f"invalid viewer slot {slot!r}")
        result = subprocess.run(
            ["i3-msg", f'[instance="fleet-{slot}"] focus'],
            text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "i3 focus failed")
        return None
    if operation == "prompt":
        prompt = message.get("prompt")
        values = message.get("values")
        fixed = message.get("fixed")
        if not isinstance(prompt, str) or not isinstance(values, list) or \
                not all(isinstance(value, str) for value in values) or \
                not isinstance(fixed, bool):
            raise RuntimeError("invalid prompt request")
        command = ["rofi", "-dmenu", "-p", prompt, "-location", "2",
                   "-theme", "rofi"]
        if fixed:
            command.extend(("-i", "-no-custom"))
        result = subprocess.run(command, input="\n".join(values) + "\n",
                                text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(f"rofi exited with status {result.returncode}")
        return result.stdout.strip()
    raise RuntimeError(f"unknown workstation operation {operation!r}")


def serve(socket_path):
    socket_path = Path(socket_path)
    socket_path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen()
        try:
            while True:
                connection, _ = server.accept()
                with connection:
                    try:
                        message = json.loads(connection.makefile().readline())
                        reply = {"result": dispatch(message)}
                    except (json.JSONDecodeError, RuntimeError) as error:
                        reply = {"error": str(error)}
                    connection.sendall(
                        (json.dumps(reply, separators=(",", ":")) + "\n").encode())
        finally:
            socket_path.unlink(missing_ok=True)
