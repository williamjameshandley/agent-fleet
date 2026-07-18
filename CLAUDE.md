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

- Fleet never invokes `kill-window` or `unlink-window`, and never destroys a
  session implicitly. Explicit user-approved archive records the vendor
  conversation identity in recoverable History before closing the live tmux
  session, and refuses to close if resurrection cannot be established. Restore
  resumes the full vendor conversation rather than requesting compression.
  There is no permanent purge.
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
- No persistent JSON state. Lovelace owns the sole disposable in-memory
  projection and the global `fleet@muster` and `fleet@main` sessions. Actual
  named-viewer placement remains workstation-local and comes from i3.
- Verify installed tmux, SSH, fzf and agent behavior experimentally. In
  particular, control observers attach with `ignore-size`, shell-bound remote
  arguments use `shlex.join`, and tmux `#{q:}` fields are parsed with `shlex`.

## Voice and Commander

Commander proposes typed, non-destructive actions over canonical sources and
slots; deterministic Fleet code validates and executes them. Alan composition
is specified in `VOICE_COMPOSER.md`: speech edits a visible draft and only the
local `Alan, send` control sends its visible snapshot and presses Enter. The
composer archives recoverable state but never becomes tmux topology authority.
mdgtd and shared keyboard/mouse control remain later integrations.

Commander transcript search is a composable Python API over Claude and Codex
JSONL, optionally exposed by thin CLI commands. Do not introduce MCP servers or
a bespoke tool protocol; this repository follows the post-MCP approach used by
Alan Home and Alan Work.
