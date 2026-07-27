import json
import os
from pathlib import Path
from time import time


ROOT = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-fleet/alan"


class Archive:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.events = self.root / "events.jsonl"

    def record(self, composition, event, **fields):
        item = {"ts": time(), "composition": composition.id, "event": event, **fields}
        with self.events.open("a") as stream:
            stream.write(json.dumps(item, separators=(",", ":")) + "\n")

    def latest(self):
        latest = None
        if self.events.exists():
            for line in self.events.read_text().splitlines():
                item = json.loads(line)
                if item["event"] in {"sent", "cancelled"}:
                    latest = item
        return latest
