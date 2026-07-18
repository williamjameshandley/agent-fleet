from dataclasses import dataclass, field, replace
from enum import StrEnum
from time import time
from uuid import uuid4


class Mode(StrEnum):
    RECORDING = "recording"
    PAUSED = "paused"
    CLOSED = "closed"


@dataclass(frozen=True)
class Destination:
    key: str
    host: str
    session_id: str
    pane_id: str
    label: str
    window_id: int


@dataclass(frozen=True)
class Composition:
    id: str = field(default_factory=lambda: str(uuid4()))
    created: float = field(default_factory=time)
    mode: Mode = Mode.RECORDING
    draft: str = ""
    destination: Destination | None = None
    queued: int = 0

    def append(self, text):
        separator = " " if self.draft and not self.draft.endswith((" ", "\n")) else ""
        return replace(self, draft=self.draft + separator + text)

    def pause(self):
        return replace(self, mode=Mode.PAUSED)

    def resume(self):
        return replace(self, mode=Mode.RECORDING)

    def close(self):
        return replace(self, mode=Mode.CLOSED)


CONTROLS = {"pause", "resume", "send", "cancel"}


def classify(text, opening=False):
    words = text.strip().lower().replace(",", "").rstrip(".!?").split()
    if not words or words[0] != "alan":
        return "dictation", text.strip()
    rest = " ".join(words[1:])
    if rest in CONTROLS:
        return "control", rest
    value = text.strip()[len(words[0]):].lstrip(" ,")
    return ("dictation", value) if opening else ("instruction", value)
