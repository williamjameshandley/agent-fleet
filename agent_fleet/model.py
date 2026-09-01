from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ServerRef:
    source: str
    socket: str
    pid: int
    started: int
    kind: str = "tmux"

    @property
    def host(self):
        return self.source.rsplit("@", 1)[-1]

    @property
    def key(self):
        if self.kind == "alan":
            return f"alan:{self.source}"
        return f"{self.source}:{self.socket}:{self.pid}:{self.started}"


@dataclass(frozen=True, order=True)
class SessionRef:
    server: ServerRef
    session_id: str

    @property
    def key(self):
        if self.server.kind == "alan":
            return f"alan:{self.server.source}:{self.session_id}"
        return f"{self.server.key}:{self.session_id}"


@dataclass(frozen=True)
class Session:
    ref: SessionRef
    name: str
    created: int
    activity: int
    attached: int
    windows: int
    command: str
    title: str
    cwd: str
    agent_name: str = ""
    reported_state: str = ""
    summary: str = ""
    recency: int = 0
    transcript_id: str = ""
    human_activity: int = 0
    evaluation: str = ""
    evaluation_started: int = 0
    transcript_path: str = ""
    worked: bool = True
    attachment: SessionRef | None = None
    attachment_ambiguous: bool = False
    evaluator: str = ""
    managed: bool = False
    hibernation: str = "unsupported"

    @property
    def agent(self):
        if self.agent_name:
            return self.agent_name
        command = self.command.rsplit("/", 1)[-1]
        if command in {"claude", "codex", "gemini", "grok"}:
            return command
        if command == "agy":
            return "antigravity"
        return "shell"

    @property
    def state(self):
        if self.reported_state:
            return self.reported_state
        if self.agent == "shell":
            return "waiting"
        title = self.title.lower()
        if self.agent == "claude" and any(x in title for x in ("✳", "working", "thinking")):
            return "working"
        if self.agent == "codex" and any(x in title for x in ("working", "thinking")):
            return "working"
        if self.agent == "grok" and any(
                x in title for x in ("waiting for response", "responding", "thinking")):
            return "working"
        return "waiting"


def key_host(key):
    source = key.removeprefix("alan:").split(":", 1)[0]
    return source.rsplit("@", 1)[-1]


def key_source(key):
    return key.removeprefix("alan:").split(":", 1)[0]


def key_actor(key):
    value = key.removeprefix("alan:")
    source, separator, actor = value.partition(":")
    if not separator or "@" not in source or not actor:
        raise ValueError("invalid Alan identity")
    return actor
