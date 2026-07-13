# agent-fleet

Awareness and switching for a fleet of terminal AI-agent sessions in tmux
across hosts, operable with one hand or none. Agents are peers — claude
and codex today, more by registering their command and poll reader —
and tmux is the *first* interface: anything that can read the manifest
could be another.

**The vocabulary is nautical.** The **flagship** (an always-on host)
commands **ships of the fleet** (agent hosts); independently focused viewers
are **stations** (`--screen` remains the CLI spelling).
Rows carry **pennant numbers**; the published snapshot is the
**manifest**; the roll-call column is the **muster**; taking keyboard
command of the fleet is **the conn**. Reserved for the future: *task
force* (ad-hoc cross-host session group), *squadron* (per-host group),
*flotilla* (a session's subagents).

## How state is read — no hook

Each agent's state comes from what that agent already exposes;
nothing is instrumented, and there is no lifecycle hook to install,
trust, or restart:

- **claude** exposes session identity and blocked state through
  `claude agents --json`; its pane title supplies live working/waiting state
  and the summary, and its last timestamped transcript event supplies recency.
- **codex** writes no title but keeps its rollout JSONL open; the poll
  finds it through the pane's process tree (`/proc/<pid>/fd`) and reads
  the tail (`task_started`/`task_complete` for state, the last
  `agent_message` for a summary).

A new agent adds a branch to `cmd_poll` describing what *it*
writes — there is no default agent.

## The pieces

- **`/usr/bin/fleet`** — **tmux is the fleet**: `fleet@main` is a tmux
  session on the flagship whose windows ARE the agent sessions (linked
  windows for flagship rows; persistent ssh clients onto ship-side shadow
  sessions for remote rows), so stepping, jumping, and picking are native
  tmux commands — nothing external sits in a keypress path. Verbs:
  - `fleet up --screen S` — create the grouped fleet session; `main` also
    owns the single writer window. It owns no agent rows.
  - `fleet muster --write` — the single writer (in `fleet@main:0` under
    `watch`): it runs `fleet _poll` on every host in
    `~/.config/agent-fleet/hosts` (ssh aliases; **first line is the
    flagship itself**; the library reads each host's transcripts
    locally), orders rows by urgency and then transcript-event recency — the
    fleet window index IS the pennant number, recomputed at cadence and
    meaningful only as displayed; reordering pauses while the conn is
    armed — publishes the manifest entirely as tmux options, and reconciles
    the fleet windows. There is no JSON state file in the command path.
  - `fleet conn` — one-hand stepping mode (a tmux key-table, armed on the
    fleet session's clients, or by pressing Enter): bare `j/k` step
    windows, `n/p` hop unacknowledged alerts, `l` last, digits jump,
    `Esc` back to origin, any other key exits. Every motion is native.
  - `fleet station list/show/clear/swap/focus` — arrange real Fleet windows
    among grouped tmux sessions. Each station has an independent selected
    window; clearing selects window 0 and never terminates an agent. tmux's
    `client-focus-in` hook makes the station clicked in Ghostty the default
    target for `switch`, `scroll`, `say`, and commander actions.
  - `fleet context` — print the current manifest, station placement, focused
    station, and state-transition times for a commander. It creates no state
    file; the JSON exists only on stdout.
  - `fleet type [--screen STATION] TEXT...` — insert literal dictation into
    the focused or named station without submitting it. Mouse-first speech
    therefore needs no station name; fully hands-free speech may name one.
  - `fleet create` — the muster's `c`: fzf pickers for host, agent,
    directory, and name make one session, one window, one agent.
  - `fleet list / info / latest` — the log book: one interface over both
    agents' transcript stores (import `fleet` to compose `sessions`,
    `events`, `texts`, `info`).
  - `fleet switch / next / enter / scroll / rename / say / type` — switching,
    off-screen approvals, scrollback, and the spoken-command resolver.
- **the muster column** — a persistent `fzf --listen` process on
  `$XDG_RUNTIME_DIR/agent-fleet-muster.sock`
  (`fleet muster-ui`) fed by `fleet muster --rows`; the poller and
  selection hooks push `reload`/`pos` to its Unix socket, so the
  cursor tracks stepping at keypress speed. Enter jumps the fleet view
  and arms the conn; a live `capture-pane` preview shows the row's tail.
  Its header shows Claude Code and Codex account-window consumption and
  time to reset. The writer refreshes those every five minutes through
  `fleet-usage`: Claude's account endpoint and codex-proxy's cached quota
  (which creates no new OpenAI request). Results live in tmux options.
- **`/usr/lib/agent-fleet/wake-dryrun`** (+ user unit) — log-only
  openWakeWord scorer on the mic machine: the empirical gate for
  hands-free operation.

The compact glyphs use the Font Awesome 7 Brands Claude/OpenAI marks and Solid
host/status symbols: code for Lovelace, an apple for Newton, a microchip for
Turing, an atom for Boltzmann, and infinity for Noether.

Source the packaged native bindings from the ordinary tmux configuration:

```
source-file /usr/share/agent-fleet/tmux.conf
```

## Development cycle

The repo is deployed as an Arch package to every host; the loop is:

```
env -u VIRTUAL_ENV PATH=/usr/bin:/bin makepkg -sif --noconfirm   # build + install on the flagship
scp *.pkg.tar.zst <ship>:/tmp/ && ssh <ship> 'sudo pacman -U …'   # ships run fleet _poll, so they need it too
```

After a change that alters the fzf muster's bindings/layout, the running
column must be re-spawned to pick them up — bounce it through its own
socket (the launcher loop respawns it):

```
curl -s --unix-socket "$XDG_RUNTIME_DIR/agent-fleet-muster.sock" \
  -XPOST -d abort http://localhost
```

Run `python -m unittest discover -s tests -v` before packaging.

Verify against the live fleet, not from memory: the single writer runs in
`fleet@main:0`, so a crash shows there and the tmux manifest's
`@fleet_snapshot_ts` stops advancing (`age` in the muster). Read what each agent writes before
claiming how it behaves — the constitution's binding rules, including
*verify on the machine* and *state from what each agent writes, not
hooks*, are in `CLAUDE.md`.
