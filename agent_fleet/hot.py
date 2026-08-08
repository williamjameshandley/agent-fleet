"""Stdlib-only client for the Muster hot commands.

Spawned by fzf on every refresh, preview, and cursor placement; must not
import the heavy projection modules (rendering lives in the daemon).
"""

import shutil
import socket
import subprocess
import sys

from .config import RUNTIME


def fetch(message):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(RUNTIME / "fleet.sock"))
        client.sendall((message + "\n").encode())
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).decode()


def items():
    width = shutil.get_terminal_size((100, 24)).columns
    rows = fetch(f"items {width}").removesuffix("\n")
    if rows:
        sys.stdout.write(rows + "\n")


def header():
    result = subprocess.run(
        ["/usr/bin/tmux", "-N", "show-options", "-qv", "-t", "=fleet@muster:",
         "@fleet_viewer_error"], capture_output=True, text=True)
    error = result.stdout.strip()
    if error:
        sys.stdout.write(f"{error}\n")
    sys.stdout.write(fetch("header"))


def preview(key, columns, lines):
    sys.stdout.write(fetch(f"preview {key} {columns} {lines}"))


def active_main():
    with socket.socket(socket.AF_UNIX) as client:
        try:
            client.connect(str(RUNTIME / "viewer-main.sock"))
        except (FileNotFoundError, ConnectionRefusedError):
            return ""
        client.sendall(b"STATUS\n")
        return client.makefile().readline().strip()


def cursor():
    active = active_main()
    return fetch(f"cursor {active}" if active else "cursor").rstrip("\n")


def main(argv):
    command, arguments = argv[0], argv[1:]
    if command == "items":
        items()
    elif command == "header":
        header()
    elif command == "cursor":
        sys.stdout.write(cursor())
    elif command == "preview":
        try:
            key, *rest = arguments
            if len(rest) > 2:
                raise ValueError(rest)
            columns, lines = (int(value) for value in rest + ["0", "0"][len(rest):])
        except ValueError:
            print("usage: /usr/lib/agent-fleet/ui preview key [columns] [lines]",
                  file=sys.stderr)
            raise SystemExit(2)
        preview(key, columns, lines)
