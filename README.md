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
retain one shared selection while the user moves. Each viewer slot keeps one
presentation client per host it has opened, so returning to a warm host switches
the existing tmux client and UI window without rebuilding SSH or terminal state.
Closing or moving a workstation detaches only its display; the Lovelace-resident
viewer state remains. A multi-screen deck
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

Fleet is three mechanisms composed together. A workstation is only a display and
input endpoint; the persistent Fleet machinery lives on Lovelace.

```
Boltzmann terminal
   │
   │ ordinary SSH terminal connections
   ▼
Lovelace
 ├─ source tmux server            /tmp/tmux-1000/default
 │    ├─ fleet@muster
 │    ├─ fleet@events
 │    └─ native Lovelace sessions
 │
 ├─ Fleet daemon                  fleet.service
 │    ├─ observes Lovelace locally
 │    ├─ observes Newton over SSH
 │    ├─ observes Turing over SSH
 │    └─ observes Noether and Boltzmann over SSH
 │
 └─ presentation tmux server      /tmp/tmux-1000/agent-fleet-ui
      └─ fleet@main
           ├─ controller window
           ├─ warm Lovelace presentation
           ├─ warm Newton presentation
           └─ one warm presentation for each other visited host
```

### Workstation attachment

On Boltzmann, `mod+v` executes `fleet-view`. The launcher creates or focuses two
Ghostty windows: Muster runs `fleet-muster`, and Main runs `fleet-viewer main`.
Both commands SSH to Lovelace; neither runs the Fleet state machine on
Boltzmann.

Muster attaches an ordinary terminal client to `fleet@muster` on Lovelace's
source tmux server. Main attaches another ordinary terminal client to
`fleet@main` on the separate `agent-fleet-ui` tmux server. The source server owns
valuable native sessions and Muster. The UI server owns disposable presentation
windows, so rebuilding `fleet@main` need not terminate a native session. UI
windows contain nested tmux clients attached to the authoritative source
servers; they are not copies or mirrors of source windows.

### Observation and authority

Lovelace alone runs `fleet.service`. It maintains one long-lived,
non-interactive SSH event stream per configured host. The host helper combines
tmux control-mode lifecycle notifications, polling of Alan's observation graph and
transcript filesystem events, then publishes disposable snapshots. Cursor
motion, sorting and preview never run SSH.

Lovelace is observed locally; Newton, Turing, Noether and Boltzmann are observed
through persistent SSH processes initiated by Lovelace. Each host collector owns
a tmux control-mode connection to that host's `fleet@events` and observes tmux
sessions, windows and panes, foreground processes, Alan actors,
transcript/activity state and usage information. The collectors publish complete
host observations to the daemon, which is the sole authority for the current
global projection.

Native tmux servers and Alan runtimes remain authoritative. The daemon's global
model is an in-memory projection that can be rebuilt from them; Fleet has no
persistent JSON topology database.

Host observation and projection are implemented primarily in
`agent_fleet/daemon.py`.

### Muster and fzf

Fzf renders and navigates Muster rows. It is not authoritative and does not
perform remote attachment. Each row carries hidden fields containing the
canonical source or actor key and the view revision in addition to its visible
columns.

Cursor movement invokes `fleet-open project main KEY`; Enter invokes
`fleet-open focus main KEY`. `PROJECT` asks the resident Main controller to show
the exact source. `FOCUS` first ensures Main shows that source, then asks the
workstation to focus its Main window. The controller acknowledges cursor movement
immediately and collapses queued cursor intents to the newest one, so navigation
does not wait for every intermediate row to open.

Muster rendering and bindings live in `agent_fleet/ui.py`.

### Main presentation

`fleet@main` has a resident Python controller. It owns one `Attachment` and one
control-mode client to the UI tmux server. A Lovelace presentation runs the
equivalent of:

```
tmux attach-session -t <exact native session>
```

A Newton presentation runs the equivalent of:

```
ssh -tt newton tmux attach-session -t <exact native session>
```

The target is not a human-readable name. Fleet resolves and verifies the host,
tmux socket, server PID, server start time and session ID. Alan actors additionally
resolve to their native `fleet@alan-…` session.

The first open of a host creates one presentation window and nested native tmux
client. Moving between sources on that host uses `switch-client` on the existing
native client.
Moving to another already-warm host selects that host's retained UI window. The
steady-state path is therefore:

```
cursor movement
  → PROJECT intent
  → switch existing native tmux client
  → select existing UI window
```

It must not start Python, establish SSH, allocate a PTY, create a tmux client or
create a presentation process on every cursor movement. A single native tmux
client cannot move between independent tmux servers, which is why `fleet@main`
retains one presentation per host:

```
fleet@main
 ├─ presentation A → nested client on Lovelace
 ├─ presentation B → SSH → nested client on Newton
 └─ presentation C → SSH → nested client on Turing
```

The first open of a remote host in a viewer slot creates one long-lived
interactive SSH attachment with `BatchMode=yes`. The slot retains that attachment
in its dedicated `agent-fleet-ui` tmux session. Warm source and host changes use
the existing daemon, host and UI control streams. Fzf invokes finite POSIX-shell
socket adapters for interactive operations; it does not start Python on cursor
motion, fold changes, resizing or selection. A warm switch creates no SSH
channel, PTY, tmux client, Python interpreter or presentation process.

Presentation lifecycle and switching are implemented in
`agent_fleet/viewer.py`.

### Workstation focus

Projection and workstation focus are separate. Scrolling changes what Main
presents. Enter must additionally focus the physical workstation's Main window.
Muster therefore establishes a reverse Unix socket while the workstation is
attached:

```
Lovelace workstation socket
       │ reverse SSH Unix socket
       ▼
workstation helper
       │
       └─ i3-msg ... focus
```

The helper only focuses a local Fleet window or displays a local rofi prompt. It
does not carry session data and is not involved in projecting a remote host. Enter
resolves the selected key, ensures Main has reached it, sends `focus` through the
workstation socket, and finally runs `i3-msg` on the workstation. Projection can
continue if the workstation socket disappears, but the final focus operation must
fail visibly.

The workstation helper is implemented in `agent_fleet/workstation.py`.

### Persistence and network boundaries

Closing a workstation removes its ordinary Muster and Main clients and its
reverse workstation-control socket. It must not remove either Lovelace tmux
server, `fleet@muster`, `fleet@main`, the Main controller, warm remote
presentations, the daemon, its host observers, or any native source session. A
later `mod+v`, including from another network, attaches new ordinary display
clients to the resident Muster and Main. A new display for a slot detaches the
previous ordinary display client, but must not replace the resident controller or
its warm source presentations.

There are two independent SSH directions:

```
workstation → Lovelace
    displays Muster/Main and supplies the reverse workstation-control socket

Lovelace → Newton/Turing/…
    observes source state and carries warm remote presentations
```

Losing the workstation connection must not break a Lovelace-to-source-host
attachment. Losing Lovelace-to-Newton connectivity can mark Newton unavailable,
terminate its presentation channel and invalidate its warm presentation; Fleet
must reconstruct that presentation after the source-host connection returns. If
Lovelace itself restarts, its global tmux servers and daemon are lost unless
restored separately, while native sessions on the source hosts remain.

Tmux previews use `capture-pane -eN`, reconstruct the terminal grid with
libvterm, and apply tmux's `screen_write_preview` cursor-centred crop. Live Alan
Claude and Codex previews capture their actor-owned tmux panes.

Tmux identity is:

```
host + tmux socket + server PID + server start time + $session_id
```

Names, row positions and window indices are presentation. The daemon keeps its
projection only in memory and exposes it through a mode-0600 runtime socket.
Fold state belongs to the exact current Muster tmux server generation and is
reset when that generation is replaced.
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
fleet-viewer SLOT               attach a named Lovelace-resident viewer slot
fleet-viewer --destroy SLOT     explicitly destroy one derived viewer slot
fleet-view                      laptop 50:50 launcher
fleet-deck                      home multi-screen launcher
fleet-commander                 persistent Claude Commander session
```

Semantic operations compose directly from Python:

```python
from agent_fleet.actions import archive, create, refresh, rename
from agent_fleet.daemon import snapshot
from agent_fleet.protocol import decode_message
from agent_fleet.viewer import show

sessions, usage, unavailable = decode_message(snapshot())
key = create(host, agent, name, cwd)
rename(key, new_name)
show(key, slot="main")
refresh(key)
archive(key)
```

Fleet accepts only its finite create, rename, archive, restore and refresh
operations; it publishes no general command surface. Recoverable provider
history is read directly from native transcripts and exact retained Alan actor
identity; ordinary tmux state has no inferred reconstruction path.

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

## Diagnostics

Fleet reports its authoritative lifecycle transitions directly to the user
journal. Inspect all structured Fleet events, one viewer slot, or the daemon's
complete service stream with:

```sh
journalctl --user -t agent-fleet
journalctl --user -t agent-fleet FLEET_COMPONENT=viewer FLEET_SLOT=main
journalctl --user -u fleet.service
```

Structured events cover daemon and host availability, viewer and presentation
lifecycle, completed projection paths, and failures at the boundary that owns
them. The service stream also contains native collector diagnostics on stderr.
Fleet does not record terminal bytes, pane content, previews, transcripts,
prompts, user input, or model output. The journal is diagnostic history, not
authoritative Fleet state: tmux servers and Alan runtimes remain authoritative,
and Fleet's in-memory projection is rebuilt from them.

## Development

```
python -m pytest
env -u VIRTUAL_ENV PATH=/usr/bin:/bin makepkg -sif --noconfirm
```

Package updates must not restart a tmux server or mutate a source session.
