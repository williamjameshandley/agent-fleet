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
  (working / waiting / needs-action, gone on end) and rings the pane's
  bell on actionable events, so attention rides tmux's native alert flags.
- **`/usr/bin/fleet`** — **tmux is the fleet**: `fleet@main` is a tmux
  session on the flagship whose windows ARE the agent sessions (linked
  windows for flagship rows; persistent ssh clients onto ship-side shadow
  sessions for remote rows), so stepping, jumping, and picking are native
  tmux commands — nothing external sits in a keypress path. The verbs:
  - `fleet up --screen S` — create the fleet session (window 0 = the
    muster roll call under `watch`) and set its options; owns no rows.
  - `fleet muster [--write] [--color]` — one-shot roll-call print.
    Exactly one muster runs `--write` (in `fleet@main:0`): it polls every
    host in `~/.config/agent-fleet/hosts` (ssh aliases; **first line is
    the flagship itself**), merges hook state with live pane inventories,
    orders rows by urgency then newest change — the fleet window index IS
    the pennant number, recomputed at cadence and meaningful only as
    displayed; reordering pauses while the conn is armed — publishes the
    manifest, and reconciles the fleet session's windows against it.
    `watch` owns the refresh; fleet never loops or draws.
  - `fleet conn` — Tier-1 stepping mode (a tmux key-table, armed on the
    fleet session's clients): bare `j/k` step windows, `n/p` hop
    unacknowledged alerts, `l` last, digits jump, `Esc` back to origin,
    any other key exits. Every motion is a native tmux command.
  - `fleet switch / next / enter / scroll / rename / say` — switching,
    off-screen approvals, scrollback, and the spoken-command resolver
    (rules first, then an LLM at the URL in `~/.config/agent-fleet/llm`,
    allowlisted; spoken submit stays disabled until the wake-word dry-run
    data picks its form). Picking is `choose-tree` over the fleet
    session's windows, annotated by the `@fleet_*` options the poller
    stamps.
- **`/usr/lib/agent-fleet/wake-dryrun`** (+ user unit) — log-only
  openWakeWord scorer on the mic machine: the empirical gate for
  hands-free operation.

State detection is hook-driven (event truth from the agent itself); agent
panes without hook state render with a dim `t` marker — visible rollout
fallback, never silent drift. See `CLAUDE.md` for the binding design
rules.
