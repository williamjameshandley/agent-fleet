# agent-fleet — project philosophy

Awareness and switching for many terminal AI-agent sessions in tmux, across
hosts, operable with one hand or none. These rules are binding on every
change; a patch that violates one is wrong even if it works.

## The app is tmux

- **Never build an interface.** No TUI loops, no curses, no screen drawing,
  no window choreography. fleet *prints text* and *invokes tmux*; everything
  the user sees is a tmux pane, client, popup, or mode. The screen is owned
  by `watch` (the writer's heartbeat), `tmux wait-for` (event-driven muster
  repaint: the after-select-window hook and the poller signal one latched
  channel), `choose-tree` (picking), and tmux itself (layout, key-tables,
  copy-mode). If a change needs fleet to redraw something, the design is
  wrong.
- **tmux is the fleet.** The fleet@main session's windows ARE the agent
  sessions; window order is the ring; every motion is a native tmux
  command. External code never sits in a keypress path — the poller merely
  keeps windows created, labelled, and stamped (`@fleet_*` options are the
  state bus tmux formats render).
- **The view is the thing itself.** A fleet window is the real window
  (linked, flagship rows) or holds a real nested tmux client attached to
  the real session (ship rows) — never a preview, capture, or re-render.
- Modality is tmux's: the conn is a key-table (`bind -T fleet`), the same
  machinery as copy-mode. Multi-screen is multi-client + session groups.
  Persistence is a detached session on an always-on host.

## Composition over implementation

Each job belongs to the existing tool that does it well: `ssh`
(+ControlMaster) is all transport; `jq` parses hook JSON; agent state comes
from the **vendor's own lifecycle hooks**, never scraping; `pacman` deploys;
systemd user units run what must persist without a terminal. What remains in
`fleet` is glue only — the hook→state mapper, the cross-host merge/numbering
join, and verbs that each expand to a few tmux invocations. If a proposed
function does more than glue, find the tool that already does it.

## State and truth

- Flat files, no daemon, no DB, no message bus, no sockets. One **writer**
  publishes the snapshot (`state.json`) and reconciles the fleet session
  against it; everything else reads one or the other. Rows are
  agent-window-level (`host:window_id`, the immutable id); the pennant
  number IS the fleet window index — position in the (urgency,
  newest-change) order, recomputed at cadence, meaningful only as
  currently displayed. Reordering pauses while the conn is armed: the
  ring never shifts under fingers. Hook state stays pane-level; panes
  group into their window's row by urgency (a working sibling never
  masks a waiting pane).
- Hook state is event truth; panes without it render with a visible `t`
  marker — fallback must be *seen*, never silently absorbed. Bells are only
  the unacknowledged-event hop channel; they never change row state.
  Delete state only after a *successful* pane inventory (a failed poll is
  host-unusable, never "all panes died").
- All fleet hosts (first line: the flagship itself) are the ssh aliases in `~/.config/agent-fleet/hosts` — the alias is
  the canonical key everywhere; `hostname` output is informational.
- `@` in a tmux session name marks it fleet-created (`fleet@<screen>`,
  ship shadows `fleet@w<N>`); user sessions never contain it and such
  sessions are never listed.

## Coding principles

These rules are themselves subject to "lean code". Don't apply them to
absurdity — if a rule pushes you toward hundreds of lines of validation for
hypothetical drift, you're using it wrong.

- **Lean code.** Minimum lines, minimum dependencies, minimum abstraction.
  Three similar lines beat one premature abstraction. Audit for dead code.
  This rule wins ties.
- **No over-engineering.** No features, config knobs, or abstractions until
  needed. No validation for failure modes that require an attacker who
  already has the keys.
- **No migration code.** Pre-alpha. No backwards-compatibility shims, no
  transition logic, no marker files. If old state files break, delete
  `~/.cache/agent-fleet` and `~/.local/state/agent-fleet` and move on.
- **Crash on drift, don't paper over.** When code parses a value (a state,
  an event name, a target, a snapshot field), enumerate the known cases and
  crash on anything else. No `default` branches that silently fall through,
  no `try: except: pass`, no `or []` over missing data. Scope: values this
  code parses — not fingerprinting every artifact it can reach.
- **Boundaries translate errors, don't hide them.** ssh, tmux, and LLM
  boundaries convert failures into loud user-visible exits, never silent
  drops or substituted defaults. No stderr suppression anywhere: the poll
  iterates the state glob explicitly, so even the empty case needs none.
- **Reviewers check principles, not just plans.** A reviewer given this file
  MUST flag plan rows or code shapes that violate these principles even if
  a plan approved them. The plan can be wrong; the principles ground it.

## Failure posture

Crash loudly on drift: missing registration, unknown screen, unresolvable
target → print and exit non-zero. No fallback heuristics ("most recent
client" is banned), no retries, no arbitrary timeouts (the one 2 s cadence
is the documented human-glance constant; bounded startup handshakes must be
loud on expiry). Refusal beats guessing: a spoken command that doesn't
resolve does nothing, visibly.

## Voice

Voice only wins where keys can't reach: prose dictation, and the zero-hands
tier — which stays behind the empirical wake-word gate (log-only dry-run,
thresholds swept offline). Spoken *submit* is the one consequential command:
it ships only in the form the false-accept data justifies. Every utterance
is ledgered.

## Verify on the machine, not from memory

The repo history is a museum of API beliefs that died on contact: tmux ≥3.7
sanitises control chars *and* glyphs in formats (`#{q:}` + shlex, never tab
separators); `display -c` routes output and does NOT set format context;
`switch-client` resets a client's key-table; `run-shell` stdout hijacks the
pane into view mode (why no run-shell may sit in a hot path); alert flags
are per-winlink (acknowledging in fleet leaves the user's own flag);
`session_bell_flag` is bugged first-window-only (use per-window flags);
Claude pane titles are task summaries (identify agent panes by
`pane_current_command`); Codex hook config parses but fires nothing
(0.144.1). Before relying on any tmux/agent behaviour, run it on the
installed version and read the log.
