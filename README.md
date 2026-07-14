# agent-fleet

Agent Fleet is a fast, tmux-backed switchboard for Claude Code, Codex and shell
sessions spread across several machines. `fleet-next` is the greenfield
implementation; the original `fleet` remains installed during the cutover only
for its vendor transcript readers and rollback.

## Experience

Muster is a persistent local fzf list. Working and needs-action sessions rise to
the top, waiting work follows by meaningful transcript recency, and shelved work
stays visible at the bottom. Its cursor is keyed by the canonical source ID, so
sorting cannot turn one row into another.

Enter opens the selected real tmux session in a persistent Ghostty viewer. On a
laptop the single `main` viewer is the explicit destination. A multi-screen deck
focuses an already open source or uses a free slot; a full deck never evicts
anything implicitly. `mod+v` returns to the always-visible Muster through i3.

Fleet never links source windows into a mirror and has no delete, purge,
`kill-session`, `kill-window` or `unlink-window` action. `d` marks an attention
loop done in a tmux option; it does not end the session. Viewer dismissal only
detaches that viewer.

## Architecture

Each workstation runs `fleet-next.service`. It maintains one long-lived,
non-interactive SSH event stream per configured host. The host helper combines
tmux control-mode lifecycle notifications with transcript filesystem events and
publishes disposable snapshots. Navigation, sorting and preview never run SSH.
Opening a remote source creates the one unavoidable long-lived interactive SSH
attachment with `BatchMode=yes`.

Live identity is:

```
host + tmux socket + server PID + server start time + $session_id
```

Names, row positions and window indices are presentation. The daemon keeps its
projection only in memory and exposes it through a mode-0600 runtime socket.
Live topology remains entirely in tmux; there is no JSON state file or database.

The host adapter currently reuses `fleet _poll` for the already verified Claude
and Codex transcript semantics. It does not reuse the old manifest, writer,
reconciliation, numbered windows, SSH viewers or `fleet@main`.

## Commands

```
fleet-muster                    persistent local Muster
fleet-viewer main               persistent direct-attachment slot
fleet-next show SOURCE          focus/open a source
fleet-next show SOURCE --slot S explicit replacement
fleet-next dismiss --slot S     detach a viewer only
fleet-next create               create a real tmux session
fleet-next rename SOURCE        rename a real tmux session
fleet-next done SOURCE          shelve its attention loop
fleet-view                      laptop 50:50 launcher
fleet-deck                      home multi-screen launcher
fleet-commander                 persistent Claude Commander session
```

Host aliases come from `~/.config/agent-fleet/hosts`. Routing and credentials
belong to OpenSSH configuration. Machine labels are ASCII (`N L B T OE`), so
Fleet has no icon-font dependency.

## Development

```
python -m unittest discover -s tests -v
env -u VIRTUAL_ENV PATH=/usr/bin:/bin makepkg -sif --noconfirm
```

The old implementation and tests remain until the parallel soak proves direct
viewing on Boltzmann, Noether and Newton. Cutover must not restart a tmux server
or mutate a source session.
