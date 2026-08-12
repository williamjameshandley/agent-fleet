# 2026-08-11 estate cutover record

This is historical evidence for the named cutover, not an inventory of the
current installed estate.

The 2026-08-11 cutover installed one attributable Alan/Fleet baseline on
Lovelace, Newton, Turing, Noether and Boltzmann. Package installation preserved
the authoritative tmux server on every running host. Noether's post-reboot
server was replaced during cold-start acceptance before it contained a durable
provider session.

## Sources and packages

- Alan source: `1d25133bc75e786ea9fc3f43a12ab78d31eb977d`
- Alan package: `alan 1:3.0.0.a2.r1786473827.g1d25133-1`
- Alan package SHA-256:
  `bafd29b0c06ca56120772d6661af0679d1eb35970e580aa6a926ec9fbcee09bf`
- Installed `alan-native-session` SHA-256:
  `33736ac2151459eac9ad84d1c21a84ecc8d5a8498a66b4e1b48810db98bf8921`
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
| Noether | 42674 | 665 | 2026-08-11 16:29:22 BST |
| Boltzmann | 4089375 | 3327119 | 2026-08-11 01:22:36 BST |

Lovelace's central `fleet.service` remained PID 564993, active since
2026-08-11 06:24:31 BST, with `NRestarts=0`. Package installation did not
restart any Alan runtime, Fleet daemon or source tmux server. Noether's tmux
PID changed only during the explicit cold-start acceptance described below.

## Acceptance

The installed estate passed:

- literal attached-tmux-client launch and native-session adoption for Claude
  and Codex on all five hosts;
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
- the complete Python Loop suite: 73 passed, including the real nginx gateway
  boundary with all temporary state under each gateway's private runtime.

Noether was rebooted before its final acceptance. With no tmux server present,
the first literal native launch created the server and kept Alan's
`ALAN_NATIVE_*` state session-local rather than placing it in tmux's global
environment. A subsequently created ordinary tmux session inherited none of
that state. Authenticated Claude and Codex sessions each returned an exact test
reply, remained resident after their display client detached, and folded onto
their exact native tmux sessions in Fleet. Main created one retained Noether
presentation: its cold projection completed in 93 ms and subsequent same-host
projection completed in 13 ms while reusing the same nested tmux client.
