# Agent Fleet constitution

Agent Fleet provides awareness and switching for tmux agent sessions across
machines, operable with one hand and eventually none.

## The source is the view

- A viewer attaches to the requested real tmux session. There is no Main
  mirror, linked observer, copied window, numbered join or parallel ordering.
- tmux owns live sessions, windows, panes and focus. Fleet has no topology DB.
- Canonical identity is host, socket, server PID, server start time and tmux
  object ID. Names, indices and rows are never identity.
- Agent status and summaries are derived, disposable projections. Attention is
  separate from agent state; `done` never means the tmux session was killed.
- fzf renders and selects stable IDs. It is not authoritative state.

## Safety and spatial behavior

- Fleet exposes no delete or purge and never invokes `kill-session`,
  `kill-window` or `unlink-window`.
- Dismiss affects only a viewer. Rename and create target revalidated source
  identities. Transcript resurrection creates a new source session.
- Existing occupied deck slots do not move or get reclaimed automatically.
  An empty slot may be filled; replacement is explicit. Failure to resurface an
  important open loop is worse than showing too much.
- Cursor motion, list reload, preview and focusing an open viewer never create
  SSH. Opening a remote viewer may create one persistent interactive attachment.
  Authentication is non-interactive and failures stay visible.

## Code

- Prefer clean composition of tmux, OpenSSH, fzf, Ghostty, i3 and systemd over
  custom UI machinery. Keep code lean and comments factual.
- Do not add defensive fallbacks that guess identities or hide drift. Translate
  boundary failures into visible errors. Never retry by session name or recency.
- No persistent JSON state. Runtime sockets and in-memory projections are
  disposable. Workstation epoch/profile state, when implemented, lives in the
  local `fleet@muster` tmux session; actual placement comes from i3.
- Verify installed tmux, SSH, fzf and agent behavior experimentally. In
  particular, control observers attach with `ignore-size`, shell-bound remote
  arguments use `shlex.join`, and tmux `#{q:}` fields are parsed with `shlex`.

## Voice and Commander

Commander proposes typed, non-destructive actions over canonical sources and
slots; deterministic Fleet code validates and executes them. Dictation inserts
literal text into the genuinely focused viewer and never implies Enter. Spoken
submit remains disabled until wake-word evidence justifies it. mdgtd, agentic
composition and shared keyboard/mouse control are later integrations, not Fleet
core state.
