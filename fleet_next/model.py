from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ServerRef:
    host: str
    socket: str
    pid: int
    started: int
    kind: str = "tmux"

    @property
    def key(self):
        if self.kind == "alan":
            return f"alan:{self.host}"
        return f"{self.host}:{self.socket}:{self.pid}:{self.started}"


@dataclass(frozen=True, order=True)
class SessionRef:
    server: ServerRef
    session_id: str

    @property
    def key(self):
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
    attention: str
    agent_name: str = ""
    reported_state: str = ""
    summary: str = ""
    recency: int = 0
    transcript_id: str = ""
    attachment: dict | None = None
    human_activity: int = 0

    @property
    def agent(self):
        if self.agent_name:
            return self.agent_name
        command = self.command.rsplit("/", 1)[-1]
        if command in {"claude", "codex", "gemini"}:
            return command
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
        return "waiting"


def key_host(key):
    return key.split(":", 2)[1] if key.startswith("alan:") else key.split(":", 1)[0]
