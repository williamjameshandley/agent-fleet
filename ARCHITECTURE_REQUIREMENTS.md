# Agent Fleet product specification

## Purpose

Fleet makes ongoing agent work immediately legible and reachable across a
laptop, a four-screen home desk and a six-work-screen office. Agents continue in
their source tmux servers while the user moves. Muster remains visible; choosing
a row drops into the real session for keyboard, mouse or voice input.

## Cognitive model

- Spatial stability is valuable during a working period. Once shown, a source
  remains in its slot until the user dismisses or explicitly replaces it.
- Active, needs-action, recent and intentionally unfinished work is most
  salient. An explicitly open loop should resurface after a move or next day.
- Failure to resurface important work is worse than showing too much.
- Visibility is evidence of an open loop, not a separate truth. `done` shelves
  attention without destroying the source. Displacement lowers salience but
  does not close a loop.
- Automation is conservative: it may suggest or fill empty capacity, never
  destroy or silently reorganize occupied work. There is no purge concept.

## Interaction

- Laptop: persistent Muster and one direct viewer in a 50:50 i3 layout.
  Selecting another row explicitly replaces that sole viewer attachment.
- Multi-screen: focus an already visible source, otherwise use a free fixed
  slot. At full capacity, refuse implicit replacement and ask for a destination.
- Muster supports keyboard and mouse selection, filtering, aligned compact
  status, usage, management and history. Working work is at the top; initial
  focus is the first waiting row.
- Create and rename mutate real tmux sessions. Dismiss mutates only a viewer.
  Mark done mutates only attention. Resurrection uses vendor transcript identity
  to create a new source session. Split was a one-off migration, not a command.
- Exact machine labels are `N`, `L`, `B`, `T`, `OE`; no icon font is required.

## State and identity

- Each tmux server is authoritative for live topology.
- Source identity includes SSH host alias, tmux socket, server PID/start
  generation and tmux object ID. Row positions, window indices and names are
  never joins.
- Agent state, summary, transcript recency and quota are derived and rebuildable.
  Agent state, attention state and viewer placement are separate domains.
- Fleet has no persistent JSON state or independently mutable catalogue.
  Disposable projections live in memory; attention/profile markers may live in
  tmux options; placement comes from i3 and viewer registrations.

## Performance and transport

- Topology changes are event driven through stock tmux control mode. Transcript
  changes are filesystem-event driven. A complete inventory occurs at start or
  reconnect, not on cursor movement.
- Every workstation keeps a persistent event/control SSH process per remote
  host. Navigation and preview never launch SSH. A newly opened remote viewer
  uses one long-lived interactive BatchMode attachment.
- SSH routes, ProxyJump/fallback and credentials belong to OpenSSH config.
- Control observers never link source windows and attach with `ignore-size`.
  Viewers use normal client geometry, which must be tested at every profile.

## Usage

Claude and Codex each show aligned 5-hour and weekly bars. Undefined is
`0%/0h`; 5-hour resets include `HH:MM`; weekly resets include weekday/date and
time. Lovelace is the sole credential/quota collector, respects vendor backoff
and exposes cached values. Workstation restarts must not multiply API requests.

## Commander and voice

Commander is a persistent agent, initially Claude Code behind a vendor-neutral
typed action contract. It is both a precise voice-operated pair of hands and a
conservative recommender. It may suggest replacement candidates at capacity but
does not act until instructed. Future mdgtd context may propose start-of-day
work.

Voice ultimately has three mutually exclusive channels over one capture
pipeline: literal focused-session dictation, Commander instructions and an
explicit Enter command. Literal dictation never submits. Future composition uses
a visible draft plus focused transcript/cwd/repository context for agentic edits;
sending remains explicit. Wake words ship only after false-accept testing.

## Future, not v1

- Agentic composition and Markdown GTD integration.
- Fast local-model Commander implementations.
- Optional shared keyboard/mouse control such as Deskflow.
- A graphical deck map or full TUI, only if the physical deck plus fzf proves
  insufficient.
