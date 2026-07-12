# agent-fleet

Awareness and switching for a fleet of terminal AI-agent sessions
(Claude Code today; any agent whose lifecycle hooks fire) in tmux across
hosts, operable with one hand or none. tmux is the *first* interface —
anything that can read the manifest could be another.

**The vocabulary is nautical.** The **flagship** (an always-on host)
commands **ships of the fleet** (agent hosts); viewers are screens.
Sessions carry **pennant numbers**; the published snapshot is the
**manifest**; the roll-call panel is the **muster**; taking keyboard
command of the fleet is **the conn**. Reserved for the future: *task
force* (ad-hoc cross-host session group), *squadron* (per-host group),
*flotilla* (a session's subagents).

Three pieces, one package:

- **`/usr/lib/agent-fleet/hook`** — wired into the agent's lifecycle hooks
  on every ship and the flagship; writes one state file per session
  (working / waiting / needs-action, gone on end).
- **`/usr/bin/fleet`** — the verbs, all resolved against the manifest:
  - `fleet muster [--write] [--color] --screen S` — one-shot roll-call
    print. Exactly one muster runs `--write` (in `@frame-main`'s panel
    pane, under `watch`): it polls every host in
    `~/.config/agent-fleet/hosts` (ssh aliases; **first line is the
    flagship itself**), merges hook state with live pane inventories,
    allocates pennant numbers, publishes the manifest. `watch` owns the
    refresh; fleet never loops or draws.
  - `fleet frame --screen S` — builds/repairs the screen's frame: a tmux
    session `@frame-S` with the muster pane beside a **view pane holding a
    real nested tmux client** — the ranger layout where the preview *is*
    the live session. The frame's prefix is `C-q`, so `C-a` reaches the
    session you're looking at.
  - `fleet conn` — Tier-1 stepping mode (a tmux key-table): bare `j/k`
    walk the waiting sessions live, `n/p` everything, `l` last, `i` stay,
    `Esc` home.
  - `fleet switch / next / last / enter / scroll / rename / pick / say` —
    switching, off-screen approvals, scrollback, the fzf picker, and the
    spoken-command resolver (rules first, then an LLM at the URL in
    `~/.config/agent-fleet/llm`, allowlisted; spoken submit stays disabled
    until the wake-word dry-run data picks its form).
- **`/usr/lib/agent-fleet/wake-dryrun`** (+ user unit) — log-only
  openWakeWord scorer on the mic machine: the empirical gate for
  hands-free operation.

State detection is hook-driven (event truth from the agent itself); agent
panes without hook state render with a dim `t` marker — visible rollout
fallback, never silent drift. See `CLAUDE.md` for the binding design
rules.
