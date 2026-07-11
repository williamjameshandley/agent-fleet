# claude-fleet

Awareness and switching for many terminal AI-agent (Claude Code) sessions in
tmux, across several hosts, designed for one-handed / no-handed operation.

Two pieces:

- **`/usr/lib/claude-fleet/hook`** — wired into the agent's lifecycle hooks on
  every agent host; writes one JSON state file per session
  (`$XDG_STATE_HOME/claude-fleet/<session_id>.json`):
  `UserPromptSubmit`→working, `Stop`→waiting,
  `Notification`/`PermissionRequest`→needs-action, `SessionEnd`→gone.
- **`/usr/bin/fleet`** — everything else, resolved against a single snapshot:
  - `fleet panel --screen S [--write]` — narrow always-on panel. Exactly one
    panel runs `--write`: it polls every host in
    `~/.config/claude-fleet/hosts` (ssh aliases, one per line), merges hook
    state with the live tmux pane inventory, allocates stable numbers, and
    publishes `~/.cache/claude-fleet/state.json`. Panels without `--write`
    only render.
  - `fleet switch <n|name>` / `fleet next` / `fleet enter [n]` /
    `fleet scroll up|down [n]` / `fleet rename <n> <name>` /
    `fleet pick` (fzf) / `fleet list`.
  - Display actions target a registered *screen*: a viewing terminal records
    its tmux client tty in `~/.cache/claude-fleet/clients/<screen>` at attach
    time. Remote sessions are viewed through per-(host,screen) bridge
    sessions named `host@screen` — `@` is reserved for fleet-created
    sessions, which are never listed as rows.

State detection is hook-driven (event truth from the agent itself); agent
panes (by `pane_current_command`) without hook state render with a dim `t`
marker — visible rollout fallback, not silent drift.
