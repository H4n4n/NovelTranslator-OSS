# Historical audit baseline and completed-disposition lineage

New periodic finalization intents retain revision v1 and add the optional
`historical_proof` schema v1. Its exact fields bind novel ID, the complete
pre-edit chapter byte manifest, canonical immutable transaction plan hash and
paid checkpoint file hash. The enclosing intent hash also binds the audit
window, detached baseline/post state, rules, ownership policy, source hashes,
paid result, usage, operation identity and publication record. The normal
production lock owns intent capture and subsequent mutation.

Fresh plans use these BEFORE bytes. Already-preferred prose is unchanged
baseline text; no English substring is evidence of a previous edit. Restart
uses the original intent and plan. An intent-before-plan interruption may
publish the identical plan only while every chapter and publication artifact
remains BEFORE and no transaction state/result/reconciliation/backup survives.
After an edit, missing evidence blocks; current prose never becomes a new
baseline. Partial edits and publication must form their recorded ordered
prefixes. State, audit record and checkpoint backups precede progress, the
durable final marker.

`production_historical_lineage.py` composes the same deterministic span proof
with paid checkpoint/journal replay and outer publication/accounting evidence.
Ordinary inventory and production preflight reject unfinished intents.
Completed audit chains must reach current bytes, with matching successive
BEFORE/AFTER identities and chronological history/checkpoint linkage.

Completed uncertain-request dispositions keep exact original decoded-text hash
equality as the text fast path. Otherwise their original translation must bridge
to the first BEFORE backup through actual UTF-8 universal-newline decoding.
CRLF and Unicode are preserved in byte spans; a UTF-8 BOM remains U+FEFF, as in
the existing `Path.read_text(encoding="utf-8")` contract. A unique finalized
chain must then reach current chapter bytes. The disposition, original uncertain
journal, operator attestation, send claims and checkpoint receipt are unchanged.
The attestation remains an operator assertion, not provider-signed evidence.

The existing local audit planner first proves the exact pending audit's intent,
paid checkpoint/journals, transaction and publication prefix. Its private scoped
inventory route independently rechecks all audit obligations and original
dispositions, permitting only that unfinished audit tail. Another uncertainty
or pending audit still blocks. The executor rebuilds approval under the novel
lock, installs its existing transport/credential tripwires, completes local
publication and reruns strict ordinary inventory and production preflight. No
acceptance flag is installed on an ordinary runtime and no paid attempt is
authorized. Replaying completed finalization changes neither accounting nor
immutable evidence.

Legacy PR155 transactions can still be finalized from their intact immutable
plans and BEFORE backups. Legacy intents without `historical_proof` keep the
existing completion verifier but cannot regenerate a missing plan or authorize
new disposition lineage; those cases require separate review. Nothing migrates
or rewrites historical evidence automatically.

These are local consistency proofs under cooperative single-writer and atomic
file publication assumptions. They do not provide concurrent CAS, a signature
against wholesale hostile evidence replacement, or whole-disk atomicity. Missing
byte witnesses, source/policy drift, forks and unexplained prose fail closed.
Direct-provider acquisition and batching behavior are unchanged.
