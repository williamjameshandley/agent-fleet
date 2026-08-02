# Alan voice composer

The composer is Fleet's human interface for voice-authored prompts. Alan Home
owns capture, transcription, and spoken-intent decisions. It sends typed JSONL
events over `$XDG_RUNTIME_DIR/agent-fleet/alan-events.sock`; Fleet owns the
visible editable draft, destination, delivery, and composition archive.

Opening a composition pins the currently focused Fleet viewer's canonical tmux
pane. Unknown focus yields `NO DESTINATION`; Fleet does not guess. The window
remains keyboard-editable and grows to at most one third of the current screen.

## Local controls

Alan Home emits typed `dictate`, `edit`, `pause`, `resume`, `send`, and `cancel`
actions. Partial transcription arrives as replaceable `revision` events.

`send` snapshots the visible text, pastes exactly that snapshot into the pinned
tmux pane, presses Enter, archives the outcome, closes the composer, and restores
focus. Delivery failure leaves the draft open. `cancel` archives and closes it.
`pause` marks the composition paused and suppresses partial `revision` events
until `resume`.

Send never waits for audio, transcription, cleanup or editing work that is not
visible. A late edit result cannot alter a composition whose visible draft has
advanced or which has closed.

## Editing and archive

The editor runs Codex ephemerally with a read-only sandbox and returns a complete
replacement draft plus one activity-log sentence. Composition events are
append-only JSONL under `$XDG_STATE_HOME/agent-fleet/alan/events.jsonl` (or the
corresponding default beneath `~/.local/state`). Recovery creates a copy of the
most recent sent or cancelled draft and never sends it implicitly.
