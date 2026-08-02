# agent-fleet

Correct, minimal documentation is best. Omission is preferable to an
unsupported or obsolete claim. Incorrect documentation is worst.

Agent Fleet is a fast switchboard for attachable shell and standalone vendor
terminals plus Alan actors spread across several
machines.

## Experience

Muster is one persistent fzf list on Lovelace. Working and needs-action sessions rise to
the top, and waiting work follows by meaningful transcript recency. Its cursor is keyed by the canonical source ID, so
sorting cannot turn one row into another.

Enter attaches the global `fleet@main` viewer to the selected source. Alan owns
the exact Claude and Codex tmux terminals for those actors; Fleet attaches to
and previews those same panes. Python uses a Fleet-owned Jupyter Console and a
bare model uses Fleet's minimal line presenter, each derived from its actor
address. Provider-native transcripts remain evidence and supply History, not a
second live presentation.
Muster and Main are attachable from every workstation and therefore
retain one shared selection while the user moves. A multi-screen deck
focuses an already open source or uses a free slot; a full deck never evicts
anything implicitly. `mod+v` returns to the always-visible Muster through i3.

Fleet never links source windows into a mirror. `x` archives the selected LLM
session only after resolving its durable vendor identity; History opens the same
Alan actor or resumes the exact standalone vendor conversation. Permanent purge
is not part of Fleet.

## Alan voice composer

`alan-composer.service` presents the Boltzmann prompt composer specified in
[VOICE_COMPOSER.md](VOICE_COMPOSER.md). Alan Home owns capture, transcription
and spoken-intent decisions; Fleet receives typed composer events over a local
socket and reports its current closed/open/paused mode.

## Architecture

Lovelace alone runs `fleet.service`. It maintains one long-lived,
non-interactive SSH event stream per configured host. The host helper combines
tmux control-mode lifecycle notifications, polling of Alan's observation graph and
transcript filesystem events, then publishes disposable snapshots. Navigation,
sorting and preview never run SSH.
Opening a remote source in Main or a named workstation viewer creates the one
unavoidable long-lived interactive SSH attachment with `BatchMode=yes`.

Tmux previews use `capture-pane -eN`, reconstruct the terminal grid with
libvterm, and apply tmux's `screen_write_preview` cursor-centred crop. Live Alan
Claude and Codex previews capture their actor-owned tmux panes.

Tmux identity is:

```
host + tmux socket + server PID + server start time + $session_id
```

Names, row positions and window indices are presentation. The daemon keeps its
projection only in memory and exposes it through a mode-0600 runtime socket.
Tmux topology remains entirely in tmux. Alan actor identity, lifecycle, active
evaluation and native evidence come from Alan's operation graph. Fleet owns
labels as small ordinary files and stores no graph, lifecycle or catalogue
copy. There is no Fleet JSON state file or database.

The host adapter combines tmux process discovery with the composable
`agent_fleet.transcripts` readers for Claude and Codex JSONL.

## Process launchers and Python API

```
fleet-muster                    attach the global Lovelace Muster
fleet-viewer main               attach the global Lovelace Main
fleet-viewer SLOT               run a workstation-local named slot
fleet-view                      laptop 50:50 launcher
fleet-deck                      home multi-screen launcher
fleet-commander                 persistent Claude Commander session
```

Semantic operations compose directly from Python:

```python
from agent_fleet.actions import archive, create, refresh, rename
from agent_fleet.daemon import snapshot
from agent_fleet.protocol import decode_message
from agent_fleet.reboot import restore as restore_reboot
from agent_fleet.reboot import snapshot as snapshot_reboot
from agent_fleet.viewer import show

sessions, usage, unavailable = decode_message(snapshot())
key = create(host, agent, name, cwd)
rename(key, new_name)
show(key, slot="main")
refresh(key)
archive(key)
snapshot_reboot()
restore_reboot()
```

The reboot bridge writes a disposable snapshot file before a planned reboot
and replays it afterwards. It is not live topology authority—tmux remains the
source of truth. Fleet publishes no general semantic command.

Host aliases come from `~/.config/agent-fleet/hosts`. Routing and credentials
belong to OpenSSH configuration. Machine labels are single-cell (`N L B T Œ`),
so Fleet has no icon-font dependency.

Fleet consumes the canonical `loop` client, which resolves the personal Alan
socket from `LOOP_SOCKET` or the user's XDG state directory. An unavailable
Alan socket removes Alan rows but does not invalidate healthy tmux inventory on
the same host.

Fleet displays direct-root Alan actors by default. Spawned actors form recursive
folds beneath their nearest visible creator; each visible actor folds
independently. Python remains hidden unless its global presentation toggle is
enabled. Fleet creates user-facing Codex and Claude actors. There is no native
Alan Gemini actor. Already-running standalone Gemini terminals remain ordinary
legacy tmux rows.
Muster collects the creation fields and asks that host's Fleet process to call
Alan's canonical Python `spawn` operation. Fleet records the requested label.
Ordinary shell sessions remain directly available through tmux rather than
through Fleet's creator.

## Development

```
python -m pytest
env -u VIRTUAL_ENV PATH=/usr/bin:/bin makepkg -sif --noconfirm
```

Package updates must not restart a tmux server or mutate a source session.
