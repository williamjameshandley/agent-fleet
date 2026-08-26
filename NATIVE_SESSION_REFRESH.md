# Native session refresh

## Purpose

Refresh replaces the Alan channel and MCP processes of one running Claude or
Codex conversation while preserving the conversation itself. It is a surgical
lifecycle operation on an exact Fleet attachment, not fleet reconstruction,
conversation migration, or recovery policy.

## Authority and identity

Alan owns actor lifecycle and the append-only operation stream. The provider
owns conversation history. tmux owns the live terminal. Fleet owns the exact
tagged attachment and composes exact terminal closure with Alan's existing
resume control.

A successful refresh preserves:

- the Alan actor address;
- the provider kind and provider conversation UUID;
- the provider's complete conversation history;
- the Fleet name, working directory, and viewers as presentation state.

The old and new tmux sessions, panes, provider processes, Alan channel, and MCP
processes are expected to differ. Names, row positions, process IDs, and tmux
indices are not conversation identity.

## Preconditions

Refresh is an explicit Fleet maintenance action on one adopted Claude or Codex
actor for which:

- Fleet can resolve one exact live provider attachment;
- Fleet observes both Alan and the provider idle;
- the actor has one provider UUID and no ambiguous duplicate provider terminal;
- the actor resolves one live provider attachment with the expected UUID.

Fleet checks these preconditions before closing the selected terminal, and the
tmux authority revalidates the exact tagged source at mutation time. Failure
before closure leaves the terminal and actor untouched and reports the
conflicting evidence. Neither Alan nor Fleet guesses by name, recency, or
process shape. The operator must not submit a provider-terminal prompt
concurrently with this maintenance action: Claude and Codex expose no
transactional terminal-input quiescence protocol, so refresh does not claim to
arbitrate an Enter already written to the PTY.

## Mechanism

1. Fleet records the actor address, provider kind and UUID, exact tmux source
   identity, and the viewers currently displaying it.
2. Fleet's host authority re-resolves and closes only that exact tmux session.
   It does not retire the Alan actor and does not send keys.
3. Fleet waits on its existing projection-change condition until the old exact
   attachment is absent and the actor is unavailable. This is event-driven
   observation, not polling or a new readiness state.
4. Fleet invokes Alan's existing `resume` control on the same actor address.
   Claude restoration explicitly selects `Resume full session as-is`; Codex
   resumes the full native thread.
5. Fleet waits until the same actor address is attached to the same provider kind
   and UUID through a new exact tmux source, then reopens only the viewers
   recorded in step 1.

Each failed step stops visibly with the observed state. A failure after exact
closure may leave the actor unavailable; its provider history remains the
recovery authority and the ordinary explicit restore action remains available.
Refresh does not automatically retry, fall back to another conversation, fork
an identity, reconstruct the fleet, or retry through a session name.

## Scope boundaries

The first implementation supports Claude and Codex only. Grok or another
provider requires its own real lifecycle evidence before it can share this
operation.

Refresh does not submit terminal input. Text staged at a provider prompt is
discarded with the old terminal and must occur in neither provider history nor
Alan input.

Refresh is not a general concurrent-input protocol. Preventing a terminal Enter
that races exact closure would require mediating all PTY submission through a new
input owner. That is outside this operation.

## Discriminating acceptance

Run the following test through the real Alan, tmux, provider, channel, MCP, and
Fleet boundaries for both Claude and Codex. Mocks may cover deterministic
projection behavior but cannot satisfy this acceptance test.

1. Create a controlled conversation and establish earlier context that cannot
   be reconstructed from the final exchange alone.
2. Record its Alan address, provider UUID, exact tmux identity, provider process,
   channel process, MCP process, open viewers, and provider-native history.
3. Stage a unique sentinel at the terminal prompt without submitting it.
4. Invoke refresh through Fleet.
5. Prove the old exact tmux source and old channel/MCP processes are gone.
6. Prove the new attachment has the same Alan address and provider UUID, a new
   exact tmux source, and new channel/MCP processes.
7. For Claude, capture the actual restoration interaction selecting
   `Resume full session as-is`. For both providers, prove that the exact
   pre-refresh provider-history prefix remains present and unchanged after
   refresh; for Codex, also prove that the resumed native thread identifier is
   unchanged. Then ask through Alan for the earlier context as an end-to-end
   behavioral check.
8. Verify the staged sentinel occurs in neither provider history nor the Alan
   operation stream.
9. Send a fresh Alan prompt, observe the provider reply, and verify that only the
   previously open viewers were reopened.
10. While a controlled provider turn is observably active, invoke refresh and
    prove it fails without mutation and the turn completes. Do not present this
    as arbitration of a simultaneous terminal Enter.

Preserve the raw identities, restoration interaction, process evidence,
provider-history comparison, prompts, replies, and transcript checks. A process
start, mocked projection, summary-based answer, or matching display name is
insufficient evidence.
