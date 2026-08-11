# Deployed estate

The 2026-08-11 cutover installed one attributable Alan/Fleet baseline on
Lovelace, Newton, Turing, Noether and Boltzmann while preserving each host's
authoritative tmux server.

## Sources and packages

- Alan source: `7b82ced763f20642fc0228ecfd3d0457d0545743`
- Alan package: `alan 1:3.0.0.a2.r1786453793.g7b82ced-1`
- Alan package SHA-256:
  `fc3dc319dae947e604af0e32fd18f4489d902108cd2011f5507ddc7c805b4586`
- Installed `alan-claude-confirm` SHA-256:
  `dfe08d06381bc85e768279b353b2681159a48bf555b42244c31829c366283cd3`
- Installed `alan-claude-gateway` SHA-256:
  `0539a75e29983598ccb6eac4a49937053680a1c4f1cc3747d8d873e0c707653e`
- Installed `alan-codex-gateway` SHA-256:
  `d987782a76bcc72e1d6dfacefdd14c51a4a7e38ef947e8dee29af7bc88f36c7d`
- Agent Fleet source: `67eb07fb77f3ad20bce02266e65384d5b663a274`
- Agent Fleet package: `agent-fleet 0.3.0.r1786425755.g67eb07f-1`
- Agent Fleet package SHA-256:
  `0dbe3a257ecb05e251c0a7cdaaa1c5da4af1bcc4f1de8be6b27b70b87d2b8827`
- Portable configuration source:
  `928d91f6404be96a1c98720a5dd785d5216b9ae5`

The package identities and installed wrapper hashes were measured on every host
after installation; they are identical on all five.

## Preserved authorities

| Host | tmux PID | `alan-loop.service` PID | Runtime active since |
| --- | ---: | ---: | --- |
| Lovelace | 930078 | 4054116 | 2026-08-11 01:22:52 BST |
| Newton | 2548 | 1032648 | 2026-08-11 01:22:35 BST |
| Turing | 482057 | 1843374 | 2026-08-11 01:22:35 BST |
| Noether | 25405 | 426133 | 2026-08-11 01:22:36 BST |
| Boltzmann | 4089375 | 3327119 | 2026-08-11 01:22:36 BST |

Lovelace's central `fleet.service` remained PID 564993, active since
2026-08-11 06:24:31 BST, with `NRestarts=0`. Package installation did not
restart any Alan runtime, Fleet daemon or authoritative tmux server.

## Acceptance

The installed estate passed:

- literal attached-tmux-client launch and native-session adoption for Claude
  and Codex on Lovelace, Newton, Boltzmann and Turing;
- exact Alan attachment folding in Fleet without duplicate native rows;
- A to B to Python to C reply routing back to B;
- cursor projection, Enter focus, fold/unfold, archive and successor selection;
- sixty alternating warm Lovelace/Newton projections with median 0.960 ms,
  p95 1.105 ms and maximum 1.665 ms, reusing the same remote SSH process;
- Agent Fleet's complete repository suite on Lovelace: 378 tests and seven
  subtests passed;
- Alan's Loop suite: 200 passed and one excluded; Cockpit: 34 passed; both
  component format and strict Credo checks passed; and the focused native
  launcher/confirmation suite: 27 passed;
- the complete Python Loop suite: 72 passed, including the real nginx gateway
  boundary with all temporary state under each gateway's private runtime.

Noether's native handoff and terminal-input path are proven, but its vendor
authentication remains a host prerequisite. Claude and Codex acceptance there
is complete only after their provider sign-ins succeed and each reaches a
usable TUI.
