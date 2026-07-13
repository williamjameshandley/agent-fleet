# agent-fleet — project philosophy

Awareness and switching for many terminal AI-agent sessions in tmux, across
hosts, operable with one hand or none. These rules are binding on every
change; a patch that violates one is wrong even if it works.

## The app is tmux

- **Never build an interface.** No TUI loops, no curses, no screen drawing,
  no window choreography. fleet *prints text* and *invokes tmux*; everything
  the user sees is a tmux pane, client, popup, or mode. The screen is owned
  by `watch` (the writer's heartbeat), `fzf` (the muster column: one
  persistent `--listen` process; the poller and selection hooks push
  reload/pos to its socket — reloads keep the cursor's screen position),
  `choose-tree` (the on-demand picker), and tmux itself (layout,
  key-tables, copy-mode). If a change needs fleet to redraw something,
  the design is wrong.
- **tmux is the fleet.** The fleet@main session's windows ARE the agent
  sessions; window order is the ring; every motion is a native tmux
  command. External code never blocks a keypress — the poller keeps windows
  created, labelled, and stamped (`@fleet_*` options are the state bus tmux
  formats render). The one asynchronous exception is a best-effort curl to
  keep the separate fzf cursor aligned after tmux has already moved.
- **The view is the thing itself.** A fleet window is the real window
  (linked, flagship rows) or holds a real nested tmux client attached to
  the real session (ship rows) — never a preview or re-render. The muster's
  optional row preview may use `capture-pane`; it is not a fleet window.
- Modality is tmux's: the conn is a key-table (`bind -T fleet`), the same
  machinery as copy-mode. Multi-screen is multi-client + session groups;
  `client-focus-in` makes the mouse-focused station the default command and
  voice target. Persistence is a detached session on an always-on host.

## Composition over implementation

Each job belongs to the existing tool that does it well: `ssh`
(+ControlMaster) is all transport; agent state comes from the vendor's own
registry, transcripts, and pane title, never lifecycle hooks; `pacman` deploys;
systemd user units run what must persist without a terminal. What remains in
`fleet` is glue only — the state reader, the cross-host merge/numbering
join, and verbs that each expand to a few tmux invocations. If a proposed
function does more than glue, find the tool that already does it.

## State and truth

- No daemon, DB, or message bus. One **writer** reconciles and stamps the
  `fleet@main` tmux session; that session is the sole live manifest and
  commands never route through a cached JSON snapshot. Rows are
  agent-window-level (`host:window_id`, the immutable id); the pennant
  number IS the fleet window index — urgency first, then newest transcript
  event within each state — recomputed at cadence and meaningful only as
  currently displayed. Reordering pauses while the conn is armed: the
  ring never shifts under fingers, and the muster says when ordering is held.
  `@fleet_state_changed` records transitions and global
  `@fleet_focused_station` records focus; both remain inside tmux. Agent state stays pane-level; panes
  group into their window's row by urgency (a working sibling never
  masks a waiting pane).
- Bells are only the unacknowledged-event hop channel; they never change row
  state. A failed poll leaves that host's existing windows visibly stale;
  only a successful inventory can remove vanished panes.
- All fleet hosts (first line: the flagship itself) are the ssh aliases in `~/.config/agent-fleet/hosts` — the alias is
  the canonical key everywhere; `hostname` output is informational.
- `@` in a tmux session name marks a grouped Fleet view (`fleet@<screen>`);
  user sessions never contain it and such sessions are never listed.
- A managed agent session has one window and one agent. Session lifecycle is
  tmux-native; dormant history is derived from agent transcripts, not stored
  in a Fleet catalogue.

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
  an event name, a target, a manifest field), enumerate the known cases and
  crash on anything else. No `default` branches that silently fall through,
  no `try: except: pass`, no `or []` over missing data. Scope: values this
  code parses — not fingerprinting every artifact it can reach.
- **Boundaries translate errors, don't hide them.** ssh, tmux, and LLM
  boundaries convert failures into loud user-visible exits, never silent
  drops or substituted defaults. The sole best-effort boundary is the
  asynchronous fzf cursor push: no open muster means nothing needs updating,
  and it must never delay native navigation. Poll, reconciliation, quota and
  command failures remain visible.
- **Reviewers check principles, not just plans.** A reviewer given this file
  MUST flag plan rows or code shapes that violate these principles even if
  a plan approved them. The plan can be wrong; the principles ground it.

## Failure posture

Crash loudly on drift: missing registration, unknown screen, unresolvable
target → print and exit non-zero. No fallback heuristics ("most recent
client" is banned), no retries, no arbitrary timeouts. The 2 s poll cadence
is the documented human-glance constant; the asynchronous fzf cursor push has
a 200 ms ceiling; bounded startup handshakes must be loud on expiry. Refusal
beats guessing: a spoken command that doesn't resolve does nothing, visibly.

## Voice

Voice only wins where keys can't reach: prose dictation, and the zero-hands
tier — which stays behind the empirical wake-word gate (log-only dry-run,
thresholds swept offline). Spoken *submit* is the one consequential command:
it ships only in the form the false-accept data justifies. Every utterance
is ledgered. A mouse click is enough to choose the default station; naming a
station aloud is the hands-free override, not a tax on normal dictation.
Dictation uses literal `send-keys` and never implies Enter.

## Verify on the machine, not from memory

The repo history is a museum of API beliefs that died on contact — including
one false conviction that survived two sessions: "tmux ≥3.7 sanitises glyphs"
was actually the tmux *client* sanitising non-ASCII because non-interactive
ssh shells carry no locale (export a UTF-8 LANG in remote scripts; the
"verification" had pushed the glyph through the same locale-less path it was
testing). Real facts: `#{q:}` + shlex for formats, never tab separators;
`display -c` routes output and does NOT set format context;
`switch-client` resets a client's key-table; `run-shell` stdout hijacks the
pane into view mode (`run-shell -C` is nevertheless required for Escape because
a direct bound `select-window -t '#{@fleet_origin}'` does not expand the option);
alert flags
are per-winlink (acknowledging in fleet leaves the user's own flag);
`session_bell_flag` is bugged first-window-only (use per-window flags);
Claude pane titles are task summaries (identify agent panes by
`pane_current_command`); Codex hook config parses but fires nothing
(0.144.1). Before relying on any tmux/agent behaviour, run it on the
installed version and read the log.
