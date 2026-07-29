# Alan voice composer

> **Status:** This is an older composer-specific design and is not the current
> cross-system source of truth. See
> `../alan-home/VOICE_REQUIREMENTS.md` for the settled Planck/Boltzmann voice
> requirements and the items that still require architectural review.

## Purpose

Alan is a hands-free prompt composer for the tmux pane visible when dictation
starts. Speech builds an editable draft in a growing top tiling window; only an explicit
`Alan, send` copies the visible draft to the selected pane and presses Enter.
The screen is the commit boundary.

## Interaction

- Alan Home opens the composer after deciding that speech is addressed and
  ready. Speech may continue in the same utterance.
- The composer is a full-width top tiling window matching Boltzmann's Gruvbox
  desktop. It grows with the visible paragraph up to one third of the screen.
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
- Alan Home distinguishes dictation, editing and control from conversation
  context and the composer mode. Fleet applies those typed actions and never
  classifies transcript strings. The agent is responsive, not proactive about
  choosing a destination.
- Keyboard and mouse editing remain available.

## Local controls

Alan Home emits the following explicit controls:

- `Alan, pause` stops draft updates but leaves the composer open. Audio capture
  and transcription continue on Lovelace.
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

The shared Alan Home satellite streams PCM through its authenticated full-duplex
audio ingress and forwards typed events through Fleet's local socket. Lovelace
owns endpointing, transcription, archive and semantic decisions. Agent Fleet
owns no microphone, VAD, wake or recognition model. Replaceable revisions may
coalesce; accepted actions remain ordered. Network state appears in the activity
log. Late results never reopen or alter a sent or cancelled composition.

The editing agent prioritizes contextual strength and tool use over minimum
latency. It receives the draft, raw segment, destination, recent revisions and
context pointers. Its tools are read-only: it may search files, composition
history, Fleet/tmux metadata and a session conversation JSONL, but cannot change
the filesystem or execute consequential commands. The activity log exposes
sources and concise decisions, not private reasoning.

## Archive

Lovelace owns selected utterance audio and transcript evidence. Fleet's
append-only JSONL contains composition revisions, agent instructions, context
reads, destinations and send/cancel outcomes. Sent and cancelled drafts remain
recoverable. Recovery opens a copy and never sends silently.

## Initial scope

- Develop and debug on Boltzmann before installing elsewhere.
- Compose prompts for known tmux-backed agent sessions; this is not general
  terminal voice control.
- Use Alan Home's single continuous capture and inference pipeline.
- Start with a visible, testable prototype. Proactive destination selection,
  GTD context, local language models and whole-house audio policy are later work.
