#!/usr/bin/python3
"""Snapshot/restore the tmux claude fleet across a reboot.

snapshot: for every tmux pane, record session/window/pane topology, cwd,
running command, and — for panes running claude — the session id, mapped
from the project's transcript files in ~/.claude/projects by recency.

restore: recreate the same tmux sessions/windows/panes at the same cwds
and launch `claude --resume <id>` in each claude pane (plain shells for
the rest).

Known limitation: claude panes sharing a cwd are paired to transcripts by
recency, so pane<->session pairing within such a group can swap; all
sessions still resume.

Call :func:`snapshot` before reboot and :func:`restore` afterwards.
"""
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path

DEFAULT = Path.home() / "fleet-snapshot.json"


TMUX_ENV = {k: v for k, v in os.environ.items() if k != "TMUX"}
# TMUX is stripped so a run from inside a tmux pane can never target the
# calling server implicitly: $TMUX overrides TMUX_TMPDIR and socket selection.


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True,
                          env=TMUX_ENV).stdout


def project_sessions(cwd):
    """Recency-ranked session ids for a working directory."""
    slug = cwd.replace("/", "-").replace(".", "-")
    proj = Path.home() / ".claude" / "projects" / slug
    if not proj.is_dir():
        return []
    files = sorted(proj.glob("*.jsonl"), key=lambda f: f.stat().st_mtime,
                   reverse=True)
    return [f.stem for f in files]


def snapshot(path=DEFAULT):
    fmt = "#{session_name}\t#{window_index}\t#{window_name}\t" \
          "#{pane_index}\t#{pane_current_path}\t#{pane_current_command}\t" \
          "#{pane_pid}"
    panes = []
    warnings = []
    for line in sh("tmux", "list-panes", "-a", "-F", fmt).splitlines():
        sess, widx, wname, pidx, cwd, cmd, pid = line.split("\t")
        entry = {"session": sess, "window": int(widx), "window_name": wname,
                 "pane": int(pidx), "cwd": cwd, "command": cmd}
        panes.append(entry)
    # assign sessions per cwd group: n claude panes in a cwd get that
    # project's n most recent transcripts (order within a group is by
    # tmux position vs recency -- cosmetic swaps possible, all resumed)
    groups = defaultdict(list)
    for p in panes:
        if p["command"] == "claude":
            groups[p["cwd"]].append(p)
    for cwd, members in groups.items():
        ids = project_sessions(cwd)
        if len(members) > 1:
            warnings.append(f"{len(members)} claude panes share {cwd}; "
                            "pane<->session pairing within this group is by recency")
        for p, sid in zip(members, ids):
            p["claude_session"] = sid
        p_extra = members[len(ids):]
        for p in p_extra:
            warnings.append(f"more claude panes than transcripts in {cwd}")
    path.write_text(json.dumps(panes, indent=1))
    n_claude = sum(1 for p in panes if p.get("claude_session"))
    missing = [p for p in panes
               if p["command"] == "claude" and not p.get("claude_session")]
    for p in missing:
        warnings.append("no session id for claude pane "
                        f"{p['session']}:{p['window']}.{p['pane']}")
    return {"panes": panes, "sessions": len({p["session"] for p in panes}),
            "captured": n_claude, "path": str(path), "warnings": warnings}


def restore(path=DEFAULT):
    panes = json.loads(path.read_text())
    panes.sort(key=lambda p: (p["session"], p["window"], p["pane"]))
    existing = set(sh("tmux", "list-sessions", "-F",
                      "#{session_name}").splitlines())
    seen_windows = set()
    for p in panes:
        target = f"{p['session']}:{p['window']}"
        if p["session"] not in existing:
            subprocess.run(["tmux", "new-session", "-d", "-s", p["session"],
                            "-c", p["cwd"], "-x", "220", "-y", "50"],
                           check=True, env=TMUX_ENV)
            subprocess.run(["tmux", "move-window", "-s",
                            f"{p['session']}:0" if p["window"] != 0 else target,
                            "-t", target], check=False, env=TMUX_ENV)
            existing.add(p["session"])
            seen_windows.add(target)
        elif target not in seen_windows:
            subprocess.run(["tmux", "new-window", "-d", "-t", target,
                            "-c", p["cwd"]], check=False, env=TMUX_ENV)
            seen_windows.add(target)
        else:
            subprocess.run(["tmux", "split-window", "-d", "-t", target,
                            "-c", p["cwd"]], check=True, env=TMUX_ENV)
        subprocess.run(["tmux", "rename-window", "-t", target,
                        p["window_name"]], check=False, env=TMUX_ENV)
        if p.get("claude_session"):
            pane = f"{target}.{p['pane']}"
            subprocess.run(["tmux", "send-keys", "-t", pane,
                            f"claude --resume {p['claude_session']}",
                            "Enter"], check=True, env=TMUX_ENV)
    return {"panes": panes, "restored": len(panes)}
