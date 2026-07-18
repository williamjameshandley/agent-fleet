# Alan voice composer

## Purpose

Alan is a hands-free prompt composer for the tmux pane visible when dictation
starts. Speech builds an editable draft in a fixed-height bar; only an explicit
`Alan, send` copies the visible draft to the selected pane and presses Enter.
The screen is the commit boundary.

## Interaction

- `Alan` opens the composer immediately on the focused screen. Speech may
  continue in the same utterance; audio pre-roll prevents clipped opening text.
- The composer is a full-width, fixed-height top bar matching Boltzmann's Rofi:
  Source Code Pro Light 10 at 144 DPI, Gruvbox, a 2 px border and 2 px padding.
  It is slightly transparent and never expands.
- The draft occupies most of the bar. Its viewport follows the newest text.
  A compact scrolling activity log shows decisions, context sources and errors.
- The bar always distinguishes recording, paused, transcribing, editing and
  unavailable states. It displays the selected machine, session, window and
  pane, or an unmistakable `NO DESTINATION`.
- Raw Nemotron text appears immediately. A tool-using agent then cleans the new
  segment while dictation continues. Changed spans are briefly highlighted.
  The first cleanup pass may be strong; settled text has a bias toward stability.
- High-confidence technical corrections and path resolutions may enter the
  draft. Ambiguous alternatives remain unchanged and appear in the log.
- An `Alan ...` utterance inside the composer is an editing or destination
  instruction. After it completes, ordinary speech is literal dictation again.
  The agent is responsive, not proactive about choosing a destination.
- Keyboard and mouse editing remain available.

## Local controls

The following commands are recognized locally and never depend on an agent:

- `Alan, pause` stops dictation but leaves the composer open. Ambient audio is
  still retained and the wake detector still accepts controls.
- `Alan, resume` resumes literal dictation.
- `Alan, cancel` closes and archives a recoverable composition.
- `Alan, send` snapshots the currently visible text, sends exactly that text to
  the selected tmux pane, presses Enter, closes the bar and restores focus.

Send never waits for audio, transcription, cleanup or editing work that is not
visible. Outstanding results are cancelled or archived and cannot mutate a sent
composition. If delivery fails, the bar stays open with the draft intact. An
unselected destination is a delivery failure, never an invitation to guess.

## Destination

At activation, i3 identifies the focused window and Fleet resolves it to a
canonical tmux pane. That destination is pinned even though the composer takes
focus. The editing agent may change it only after an explicit instruction.
Unknown focus does not prevent composition; it opens with `NO DESTINATION`.

Delivery addresses the tmux pane directly. i3 is used for placement, identity
and focus restoration, not for simulating typed text.

## Transcription and editing

The composer streams PCM through Alan Home's authenticated full-duplex audio
ingress. Lovelace's Nemotron renderer owns speech endpointing and returns committed
transcript events; Agent Fleet owns no VAD or recognition model. Network failure
does not stop recording: utterances queue locally in order and the bar shows a
persistent urgent status such as `TRANSCRIPTION UNAVAILABLE · 3 QUEUED`. Late results may
update an open composition but never reopen or alter a sent or cancelled one.

The editing agent prioritizes contextual strength and tool use over minimum
latency. It receives the draft, raw segment, destination, recent revisions and
context pointers. Its tools are read-only: it may search files, composition
history, Fleet/tmux metadata and a session conversation JSONL, but cannot change
the filesystem or execute consequential commands. The activity log exposes
sources and concise decisions, not private reasoning.

## Archive

During the initial study, all microphone audio on Boltzmann is retained locally,
including closed, recording, paused and unavailable periods. Raw household audio
is retained on Boltzmann and streamed only while the composer is listening.

Each composition records time-aligned audio references, raw transcripts, wake
boundaries, control classifications, draft revisions, agent instructions,
context reads, destinations and send/cancel outcomes in append-only JSONL. Sent
and cancelled drafts remain recoverable. Recovery opens a copy and never sends
silently. The archive supports later comparison with local transcription and a
counterfactual test of whether an agent could distinguish commands without the
spoken `Alan` boundary.

## Initial scope

- Develop and debug on Boltzmann before installing elsewhere.
- Compose prompts for known tmux-backed agent sessions; this is not general
  terminal voice control.
- Use a single microphone owner and one continuous capture pipeline.
- Start with a visible, testable prototype. Proactive destination selection,
  GTD context, local language models and whole-house audio policy are later work.
