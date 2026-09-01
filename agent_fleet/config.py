import os
import json
import re
from dataclasses import dataclass
from pathlib import Path


CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "agent-fleet"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agent-fleet"
HUB = "lovelace"

# Language-actor kinds Muster create offers. Alan's spawn is the authority on
# which kinds are installed; an unconfigured kind is refused there visibly.
KINDS = ("claude", "codex", "grok", "antigravity", "llm")
COMPONENT = re.compile(r"[a-z0-9][a-z0-9_.-]*")


@dataclass(frozen=True)
class RuntimeSource:
    host: str
    principal: str
    public_socket: str
    tmux_socket: str

    @property
    def key(self):
        return f"{self.principal}@{self.host}"

    def environment(self):
        return {"LOOP_SOCKET": self.public_socket,
                "FLEET_TMUX_SOCKET": self.tmux_socket}


def ssh_environment():
    """Return an environment pinned to the user's stable SSH agent socket."""
    return {**os.environ,
            "SSH_AUTH_SOCK": f"/run/user/{os.getuid()}/gnupg/S.gpg-agent.ssh"}


def runtime_sources():
    path = CONFIG / "runtime-sources.json"
    value = json.loads(path.read_text())
    if not isinstance(value, list) or not value:
        raise ValueError("runtime-sources.json must contain a non-empty list")
    fields = {"host", "principal", "public_socket", "tmux_socket"}
    sources = []
    for item in value:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("invalid runtime source")
        if any(not isinstance(item[name], str) or not item[name]
               for name in fields):
            raise ValueError("invalid runtime source")
        if not COMPONENT.fullmatch(item["host"]) or not COMPONENT.fullmatch(item["principal"]):
            raise ValueError("invalid runtime source identity")
        for name in ("public_socket", "tmux_socket"):
            if not Path(item[name]).is_absolute():
                raise ValueError("runtime source paths must be absolute")
        sources.append(RuntimeSource(**item))
    keys = [source.key for source in sources]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate runtime source")
    return sources


def tmux_command(*arguments):
    socket = os.environ.get("FLEET_TMUX_SOCKET")
    return ["/usr/bin/tmux", "-N", *(("-S", socket) if socket else ()), *arguments]


def machine(host):
    names = {"newton": "N", "lovelace": "L", "boltzmann": "B",
             "turing": "T", "noether": "Œ"}
    return names.get(host.lower(), host[:1].upper())
