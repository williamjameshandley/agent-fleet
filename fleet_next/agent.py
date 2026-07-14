import json
import subprocess
from dataclasses import replace
from pathlib import Path


PRIORITY = {"needs-action": 0, "working": 1, "waiting": 2, "finished": 3}
LEGACY = (Path(__file__).parents[1] / "fleet"
          if (Path(__file__).parents[1] / "fleet").exists()
          else Path("/usr/lib/agent-fleet/fleet-legacy"))


def observe(sessions):
    result = subprocess.run([str(LEGACY), "_poll"], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    try:
        panes = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"invalid agent snapshot: {error}") from error
    by_session = {}
    counts = {}
    for pane in panes:
        counts[pane["tmux_sid"]] = counts.get(pane["tmux_sid"], 0) + 1
        current = by_session.get(pane["tmux_sid"])
        if current is None or PRIORITY[pane["state"]] < PRIORITY[current["state"]]:
            by_session[pane["tmux_sid"]] = pane
        elif pane["ts"] > current["ts"]:
            current["ts"] = pane["ts"]
    return [replace(session,
                    agent_name=("multiple" if counts[session.ref.session_id] > 1 else pane["agent"]),
                    reported_state=("needs-action" if counts[session.ref.session_id] > 1 else pane["state"]),
                    summary=(f"{counts[session.ref.session_id]} agent panes — management required"
                             if counts[session.ref.session_id] > 1 else pane["title"]),
                    recency=pane["ts"],
                    transcript_id=("" if counts[session.ref.session_id] > 1 else pane["session_id"]))
            if (pane := by_session.get(session.ref.session_id)) else session
            for session in sessions]
