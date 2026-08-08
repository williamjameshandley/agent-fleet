# Agent Fleet constitution

Correct, minimal documentation is best. Omission is preferable to an
unsupported or obsolete claim. Incorrect documentation is worst.

Agent Fleet provides awareness and switching for attachable native sessions
across machines, operable with one hand and eventually none.

## The source is the view

- A viewer attaches to the requested tmux terminal session or the actor's native
  provider/kernel interface. Alan owns the exact Claude and Codex tmux terminals;
  Fleet attaches to and previews them directly. Python uses a Fleet-owned Jupyter
  projection and bare-model actors use Fleet's minimal conversational presenter.
  There is no Main
  mirror, linked observer, copied window, numbered join or parallel ordering.
- tmux owns terminal sessions, windows, panes and focus. Alan owns actor
  lifecycle and append-only operation streams. Fleet derives actor state and
  native-evidence references from Alan's observation graph and owns labels and
  presentation. Fleet has no topology DB.
- Canonical identity is a tagged source reference: host, socket, server PID,
  server start time and tmux object ID; or an Alan host-bound actor address. Names,
  indices and rows are never identity.
- Agent status and summaries are derived, disposable projections.
- fzf renders and selects stable IDs. It is not authoritative state.
- Muster defaults to standalone native sessions and the immediate children of
  Alan's Unix principal actors. Principals are not rows. Further descendants are
  derived from Alan's graph and recursively folded beneath their nearest visible
  creator. Each visible actor folds independently; Python has one global
  presentation toggle. Fleet stores no parent IDs.

## Safety and spatial behavior

- Fleet never invokes `kill-window` or `unlink-window`, and never destroys a
  session implicitly. Explicit user-approved archive records the vendor
  conversation identity in recoverable History before closing the live tmux
  session, and refuses to close if recovery cannot be established. Restore
  resumes the full vendor conversation rather than requesting compression.
  There is no permanent purge.
- Rename, create, open and archive target revalidated source identities.
- Existing occupied deck slots do not move or get reclaimed automatically.
  An empty slot may be filled; replacement is explicit. Failure to resurface an
  important open loop is worse than showing too much.
- Cursor motion, list reload, preview and focusing an open viewer never create
  SSH. Opening a remote viewer may create one persistent interactive attachment.
  Authentication is non-interactive and failures stay visible.

## Code

- Prefer clean composition of tmux, OpenSSH, fzf, Ghostty, i3 and systemd over
  custom UI machinery. Keep code lean and comments factual.
- Fleet is hard alpha. Make clean cutovers: delete superseded behavior and do
  not add migrations, compatibility modes, dual paths, fallbacks, or retained
  legacy machinery. If irreplaceable live sessions require preservation during
  deployment, perform one explicitly inventoried and revalidated operational
  surgery outside Fleet, record the evidence, and remove its temporary
  apparatus. Do not turn that surgery into product migration code.
- Do not add defensive fallbacks that guess identities or hide drift. Translate
  boundary failures into visible errors. Never retry by session name or recency.
- No persistent JSON state. Lovelace owns the sole disposable in-memory
  projection and the global `fleet@muster` and `fleet@main` sessions. Actual
  named-viewer placement remains workstation-local and comes from i3.
- `fleet@main` is a transparent routing session: its status and prefixes remain
  off. The attached source presentation owns the one visible tmux status line.
- Verify installed tmux, SSH, fzf and agent behavior experimentally. In
  particular, control observers attach with `ignore-size`, shell-bound remote
  arguments use `shlex.join`, and tmux `#{q:}` fields are parsed with `shlex`.

## Voice and Commander

Commander proposes typed, non-destructive actions over canonical sources and
slots; deterministic Fleet code validates and executes them. Alan composition
is described in `VOICE_COMPOSER.md`: speech edits a visible draft and only the
local `Alan, send` control sends its visible snapshot and presses Enter. The
composer archives recoverable state but never becomes tmux topology authority.
mdgtd and shared keyboard/mouse control remain later integrations.

Commander transcript search is a composable Python API over Claude and Codex
JSONL. Do not introduce MCP servers or a second command interface; this
repository follows the post-MCP approach used by Alan Home and Alan Work.
