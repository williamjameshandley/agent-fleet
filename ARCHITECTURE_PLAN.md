# Greenfield implementation plan and status

The accepted design is a thin Python/fzf/tmux application, not a full TUI.

## Implemented in parallel

- `agent_fleet/`: typed server/session identities and NDJSON host protocol.
- Host event adapter: complete tmux inventory, control-mode topology events,
  transcript filesystem events and the verified vendor transcript reader.
- Lovelace collector: the sole in-memory projection, one persistent stream per
  source host and a protected Unix query socket.
- Stable fzf Muster: `--track` and `--id-nth` use canonical source identity;
  ordering never renumbers tmux objects.
- Persistent viewer wrappers: exact direct attachment, generation revalidation,
  BatchMode SSH and fixed slot registration.
- Laptop and home launchers, global Lovelace Muster and Main, and persistent
  Commander.
- Create, rename, open and recoverable archive actions. Fleet has no purge.
- Arch packaging and a systemd user collector unit.

## Cutover gates

1. Package and install Fleet on Lovelace, Newton, Turing, Boltzmann and Noether.
2. Verify event-to-Muster updates, stable cursor selection and disconnect state
   under real SSH failures.
3. Verify laptop 50:50 i3 launch, direct local/remote attachment and focus.
4. Verify multi-screen free-slot/full-slot behavior and tmux geometry with
   simultaneous differently sized clients.
5. Add the History/open tab and central cached usage header without
   multiplying vendor API calls.
6. Add explicit profile/arrival initialization and conservative open-loop
   ranking; never continuously rearrange occupied slots.
7. Add typed Commander context/actions. Voice, composition and mdgtd remain
   gated follow-ons.
8. Replace `mod+v` only after all live sources have been checked by canonical
   ID.

## Alan 3 causal projection and native presentation

This is a clean replacement of the flat actor projection and generic actor
presenter. It adds no migration path, compatibility mode or Fleet-owned actor
metadata.

1. Derive an actor forest from the composed NetworkX operation graph. A root has
   no incoming causal `spawn` chain. A child `create` operation points back to the
   creator's `spawn` operation, whose stream identifies the immediate creator.
   Preserve nesting across hosts and test roots, multiple generations, missing
   hosts and coincident cross-host graph composition.
2. Project standalone native Claude/Codex sessions and direct-root Alan actors
   by default. Suppress actors identified by Fleet's existing infrastructure
   roles and every Python actor. Group revealed descendants beneath their causal
   root, with independent language and Python controls. The two choices are
   ephemeral state of the current Muster tmux session; do not change the collector
   protocol or store a catalogue.
3. Replace Python `actor-view` with `jupyter console --existing` against the live
   actor kernel. Prove on an installed actor that a console cell appears in
   IPython history and exactly one Alan `input → evaluation → output` chain, and
   that an Alan-sent cell remains visible in the same console and namespace.
   Since Jupyter Console cannot interrupt an `--existing` kernel, subclass only
   its executing-state SIGINT handler to call Alan's existing `control` operation;
   retain Jupyter's terminal, input, rendering and kernel implementation. Prove
   idle SIGINT still delegates upstream without emitting Alan control.
   Exercise both full and read capability grades without weakening the kernel
   boundary. Assert exact operation deltas and references for success,
   exception, interrupt, and Alan-originated execution without a duplicate
   native input.
4. Replace Codex `actor-view` with the native Codex TUI connected to the actor's
   existing app-server Unix socket. Extend the existing bridge's native
   `user_message`, turn and completion handling. A known Alan client ID observes
   an existing input; any other user message creates one input/evaluation held
   transiently by `turn_id`, and only its matching completion closes it.
   Uncorrelated errors close nothing. The app-server remains the serial boundary:
   retain the bridge's existing Alan-send queue and prove that a native client
   cannot start another active turn rather than adding a second queue. Prove
   normal completion, error, interrupt, restart and cross-host attachment at the
   installed boundary.
5. Give Claude one actor-specific native interactive session using its durable
   session ID and actor-scoped settings. Headless Alan evaluations run bare so
   interactive hooks cannot duplicate them. The interactive boundary may replace
   the generic presenter only after real tests prove that `UserPromptSubmit`,
   normal `Stop`, API failure, user interrupt and session exit each produce one
   complete Alan operation chain. Claude's documented omission of `Stop` on user
   interrupt is a known acceptance question: determine the native event that
   closes that turn, or amend the architecture explicitly rather than adding a
   guessed timeout, next-prompt repair or incomplete-evaluation fallback.
6. Remove the generic actor presenter for Codex, Python and any Claude path that
   passes the native lifecycle gate. Retain it only for the temporary bare-model
   interface. Update the constitution and README so the source interface, graph
   projection and folded controls are the documented product rather than hidden
   implementation knowledge.
7. Run unit, protocol and installed acceptance tests on Lovelace and one remote
   host. Test that cursor identity remains stable, native viewers survive
   ordinary Fleet refreshes, unavailable ancestry stays
   unguessed, and no viewer action creates a second actor or native conversation.
   Package the same Alan and Fleet commits across the alpha hosts; do not merge
   either pull request without separate authorization.

No gate restarts a tmux server, migrates a live PTY or kills a source object.
