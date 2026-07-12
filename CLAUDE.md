# claude-fleet — project philosophy

Awareness and switching for many terminal AI-agent sessions in tmux, across
hosts, operable with one hand or none. These rules are binding on every
change; a patch that violates one is wrong even if it works.

## The app is tmux

- **Never build an interface.** No TUI loops, no curses, no screen drawing,
  no window choreography. fleet *prints text* and *invokes tmux*; everything
  the user sees is a tmux pane, client, popup, or mode. The screen is owned
  by `watch` (panel refresh), `fzf` (picking), and tmux itself (layout,
  key-tables, copy-mode). If a change needs fleet to redraw something,
  the design is wrong.
- **The view is the thing itself.** The frame's view pane holds a real
  nested tmux client attached to the real session — never a preview,
  capture, or re-render.
- Modality is tmux's: fleet mode is a key-table (`bind -T fleet`), the same
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
  publishes the snapshot (`state.json`, `numbers.json`); everything else
  reads it. Numbers are pane-level (`host:pane_id`) and mean only what the
  panel currently shows.
- Hook state is event truth; panes without it render with a visible `t`
  marker — fallback must be *seen*, never silently absorbed. Bells override.
  Delete state only after a *successful* pane inventory (a failed poll is
  host-unusable, never "all panes died").
- Hosts are the ssh aliases in `~/.config/claude-fleet/hosts` — the alias is
  the canonical key everywhere; `hostname` output is informational.
- `@` in a tmux session name marks it fleet-created (frames, shadows);
  user sessions never contain it and such sessions are never listed.

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
  `~/.cache/claude-fleet` and `~/.local/state/claude-fleet` and move on.
- **Crash on drift, don't paper over.** When code parses a value (a state,
  an event name, a target, a snapshot field), enumerate the known cases and
  crash on anything else. No `default` branches that silently fall through,
  no `try: except: pass`, no `or []` over missing data. Scope: values this
  code parses — not fingerprinting every artifact it can reach.
- **Boundaries translate errors, don't hide them.** ssh, tmux, and LLM
  boundaries convert failures into loud user-visible exits, never silent
  drops or substituted defaults. The one sanctioned suppression is the
  documented empty-glob `2>/dev/null` in the poll.
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
`switch-client` resets a client's key-table; Claude pane titles are task
summaries (identify agent panes by `pane_current_command`); Codex hook
config parses but fires nothing (0.144.1). Before relying on any tmux/agent
behaviour, run it on the installed version and read the log.
