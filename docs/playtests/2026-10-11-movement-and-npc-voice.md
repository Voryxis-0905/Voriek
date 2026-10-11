# Movement continuity and NPC voice — 11 October 2026

Movement changes are ready to publish. Prose changes improve one controlled NPC
dialogue sample, but repeated arrival scenery remains an open quality issue.

## Changes

- Player travel intent is resolved before narration, including arrival followed
  by waiting, negated secondary destinations, and explicit from/to journeys.
  Questions, plans and observations do not authorize a map journey. Current
  phrase recognition and its regressions cover English inputs.
- Route origin lookup accepts qualified room/corridor labels. The engine keeps
  a qualified local arrival endpoint supplied by the writer instead of replacing
  it with the destination zone. Every writer/checker rewrite passes the movement
  validation again before commit.
- A checkpoint outcome cannot replace the protagonist's confirmed position.
  Its other effects and NPC location changes still apply; the authored timeline
  stays unchanged and ignored player location effects are reported.
- Writer guidance now emphasizes an NPC's authored voice, a warranted answer,
  and attributed uncertainty. Quiet beats need no background checklist. Arrival
  guidance asks planner and writer to state an unchanged restriction once.
- NPC perception no longer receives the author's checkpoint plot description.
  Its guidance distinguishes witnessed acts, remembered information, hearsay,
  guesses, and actions that were requested but not performed.

## Verification

The isolated movement commit passed 256 backend tests. The combined release
candidate passed 258 backend tests and legacy compatibility checks. The full
working tree also passed 159 UI tests, build and lint. A simulated ten-turn
sequence checks mixed movement/non-movement, inventory, clock and save restore.

Real DeepSeek Flash/classic play used a disposable copy of the existing HSR test
save, with default psychology enabled. All three turns returned 200:

| Action | Saved endpoint | Duration |
| --- | --- | --- |
| Walk to Supply Zone, stop at Railway Platform, do not board | Supply Zone - Railway Platform | 121 seconds |
| Walk from Supply Zone to Base Zone; negate following anyone to Supply Zone | Base Zone | 47 seconds |
| Ask March what she saw and how she knows the platform is safe | Base Zone | 209 seconds |

After closing and reopening the API client, all three saves restored identical
characters, clock, checkpoint and chapters. The original player-world files
were unchanged. This was an API-client reopen, not a server-process restart or
browser playtest.

The live run exercised the final movement/checkpoint logic and the first prose
revision. The later arrival-specific prose/planner guidance was checked with
component probes and the backend suite, not another complete live turn.

## Prose evidence and limits

Matched writer calls reused the same recorded arrival payload and blueprint.
The old guidance produced 185 words; the new guidance produced 195. Both still
repeated missing boarding orders and unchanged scenery. The new sample included
the qualified location delta; this is state evidence, not proof of better prose.

A separate matched voice fixture used March's real authored profile with two
explicit sources: she witnessed a closed reception cordon and heard Arlan expect
boarding to reopen, without personally checking the platform. The old sample
mostly summarized her reply (176 words). The new sample used distinct direct
dialogue (169 words) and distinguished what she saw from what Arlan said. This
is one controlled sample, not evidence for every NPC or long-session quality.
Both samples added incidental physical details beyond the fixture.

A compact planner/writer probe was inconclusive: the old branch confused the
player's alias Rinn with another person after history was shortened, and the new
planner output hit its 4096-token limit and failed JSON parsing. Do not use this
probe as a quality comparison.

The starting test save already contained early event knowledge, including a
departure recorded as witnessed while its checkpoint was earlier. This release
does not migrate or relabel that historical data. The voice/source fixture is
separate to avoid treating contaminated save knowledge as fresh validation.
Long-session event provenance still needs an audit; the M0/M1 roadmap gates
remain open.

## API budget and artifacts

39 successful provider responses were recorded, including the truncated probe.
The session's conservative cumulative cost estimate was $0.183976, below the
$0.20 ceiling. The provider last reported approximately $0.26 remaining, from
$0.34 at session start; balance reporting can lag and differs from the
conservative peak-rate estimate.

Local evidence lives outside the repository in
`work/movement-prose-release-20261011/`: live turns, restore checks, source-code
hashes, matched writer outputs, compact probe results, and the shared budget
ledger. No API keys or player saves are included in this commit.
