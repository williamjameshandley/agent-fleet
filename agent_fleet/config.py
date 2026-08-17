import os
from pathlib import Path


CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "agent-fleet"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agent-fleet"
HUB = "lovelace"

# Language-actor kinds Muster create offers. Alan's spawn is the authority on
# which kinds are installed; an unconfigured kind is refused there visibly.
KINDS = ("claude", "codex", "grok", "antigravity", "llm")


def ssh_environment():
    """Return an environment pinned to the user's stable SSH agent socket."""
    return {**os.environ,
            "SSH_AUTH_SOCK": f"/run/user/{os.getuid()}/gnupg/S.gpg-agent.ssh"}


def hosts():
    path = CONFIG / "hosts"
    return [line.split("#", 1)[0].strip() for line in path.read_text().splitlines()
            if line.split("#", 1)[0].strip()]


def machine(host):
    names = {"newton": "N", "lovelace": "L", "boltzmann": "B",
             "turing": "T", "noether": "Œ"}
    return names.get(host.lower(), host[:1].upper())
