# Greenfield implementation plan and status

The accepted design is a thin Python/fzf/tmux application, not a full TUI and
not a refactor of the numbered `fleet@main` mirror.

## Implemented in parallel

- `fleet_next/`: typed server/session identities and NDJSON host protocol.
- Host event adapter: complete tmux inventory, control-mode topology events,
  transcript filesystem events and the verified vendor transcript reader.
- Workstation collector: in-memory projection, one persistent SSH stream per
  remote host and a protected Unix query socket.
- Stable fzf Muster: `--track` and `--id-nth` use canonical source identity;
  ordering never renumbers tmux objects.
- Persistent viewer wrappers: exact direct attachment, generation revalidation,
  BatchMode SSH, fixed slot registration and non-destructive dismissal.
- Laptop and home launchers, persistent Muster and initial persistent Claude
  Commander.
- Create, rename and mark-done actions. There is deliberately no destructive
  Fleet action.
- Arch packaging and a systemd user collector unit.

## Cutover gates

1. Package and install beside legacy Fleet on Lovelace, Newton, Turing,
   Boltzmann and Noether.
2. Verify event-to-Muster updates, stable cursor selection and disconnect state
   under real SSH failures.
3. Verify laptop 50:50 i3 launch, direct local/remote attachment and focus.
4. Verify multi-screen free-slot/full-slot behavior and tmux geometry with
   simultaneous differently sized clients.
5. Add the History/resurrection tab and central cached usage header without
   multiplying vendor API calls.
6. Add explicit profile/arrival initialization and conservative open-loop
   ranking; never continuously rearrange occupied slots.
7. Add typed Commander context/actions. Voice, composition and mdgtd remain
   gated follow-ons.
8. Stop the legacy writer and replace `mod+v` only after the parallel soak.
   Retain rollback until all live sources have been checked by canonical ID.

No gate restarts a tmux server, migrates a live PTY or kills a source object.
