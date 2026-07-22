# agent-fleet

Agent Fleet is a fast switchboard for attachable shell and standalone vendor
terminals plus Alan Python, Codex and Claude actors spread across several machines.

## Experience

Muster is one persistent fzf list on Lovelace. Working and needs-action sessions rise to
the top, waiting work follows by meaningful transcript recency, and shelved work
stays visible at the bottom. Its cursor is keyed by the canonical source ID, so
sorting cannot turn one row into another.

Enter attaches the global `fleet@main` viewer to the selected native session:
tmux for terminals and Alan Claude, Jupyter Console for Alan Python, and the
native remote client for Alan Codex. Muster and Main are attachable from every workstation and therefore
retain one shared selection while the user moves. A multi-screen deck
focuses an already open source or uses a free slot; a full deck never evicts
anything implicitly. `mod+v` returns to the always-visible Muster through i3.

Fleet never links source windows into a mirror. `d` marks an attention loop done
in a tmux option or, for Alan actors, by appending a `fleet_attention` event to
the local passive `fleet` mailbox; it does not end the session. Viewer dismissal only detaches
that viewer. Explicit archive will preserve vendor conversation identity before
closing a session; permanent purge is not part of Fleet.

## Alan voice composer

`alan-composer.service` is the Boltzmann-only development service for the
hands-free prompt composer specified in [VOICE_COMPOSER.md](VOICE_COMPOSER.md).
It owns continuous tagged microphone capture and currently supports manual
activation with `alan-composer open`; `alan-composer recover` reopens the most
recent sent or cancelled draft as a new composition. Wake activation is enabled only after a
custom `Alan` model passes the recorded positive and household false-accept
tests.

## Architecture

Lovelace alone runs `fleet-next.service`. It maintains one long-lived,
non-interactive SSH event stream per configured host. The host helper combines
tmux control-mode lifecycle notifications, Alan's actor watch stream and
transcript filesystem events, then publishes disposable snapshots. Navigation,
sorting and preview never run SSH.
Opening a remote source in Main or a named workstation viewer creates the one
unavoidable long-lived interactive SSH attachment with `BatchMode=yes`.

Pane previews use `capture-pane -eN`, reconstruct the terminal grid with
libvterm, and apply tmux's `screen_write_preview` cursor-centred crop. Wide
panes are clipped as terminal cells rather than wrapped as text.

Tmux identity is:

```
host + tmux socket + server PID + server start time + $session_id
```

Names, row positions and window indices are presentation. The daemon keeps its
projection only in memory and exposes it through a mode-0600 runtime socket.
Tmux topology remains entirely in tmux. Alan actors retain their own addresses
and expose explicit attachment descriptors; Fleet does not infer them from
processes or names. There is no Fleet JSON state file or database.

The host adapter combines tmux process discovery with the composable
`fleet_next.transcripts` readers for Claude and Codex JSONL.

## Commands

```
fleet-muster                    attach the global Lovelace Muster
fleet-viewer main               attach the global Lovelace Main
fleet-viewer SLOT               run a workstation-local named slot
fleet-next show SOURCE          focus/open a source
fleet-next next-waiting         advance main to the next waiting source
fleet-next show SOURCE --slot S explicit replacement
fleet-next dismiss --slot S     detach a viewer only
fleet-next create               create Claude, Codex or shell work inside Muster
fleet-next rename SOURCE        rename the native source
fleet-next done SOURCE          shelve its attention loop
fleet-view                      laptop 50:50 launcher
fleet-deck                      home multi-screen launcher
fleet-commander                 persistent Claude Commander session
fleet-snapshot snapshot [file]  record pane topology + claude session ids
fleet-snapshot restore [file]   recreate panes and `claude --resume` each
```

`fleet-snapshot` is an explicit, user-invoked reboot bridge: it writes a
disposable snapshot file (default `~/fleet-snapshot.json`) before a planned
reboot and replays it afterwards. It is not live topology authority — tmux
remains the source of truth. Claude panes sharing a cwd are paired to
transcripts by recency, so pairing within such a group can swap; all sessions
still resume.

Host aliases come from `~/.config/agent-fleet/hosts`. Routing and credentials
belong to OpenSSH configuration. Machine labels are ASCII (`N L B T OE`), so
Fleet has no icon-font dependency.

The packaged Alan socket configuration is `/etc/agent-fleet/alan-socket` and
may be overridden per user by `~/.config/agent-fleet/alan-socket` or explicitly
with `LOOP_SOCKET`. An unavailable Alan socket removes Alan rows but does not
invalidate healthy tmux inventory on the same host.

Alan lifecycle actors are Python, Codex and Claude (plus Alan's direct `llm`
type, which has no Fleet attachment). There is no native Alan Gemini actor.
Already-running standalone Gemini terminals remain ordinary legacy tmux rows.

## Development

```
python -m pytest
env -u VIRTUAL_ENV PATH=/usr/bin:/bin makepkg -sif --noconfirm
```

Cutover and package updates must not restart a tmux server or mutate a source
session.
