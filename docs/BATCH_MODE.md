# Source-first batch mode (opt-in)

Status: implemented behind `novel.json` → `"batch_mode": {"enabled": true}`; off by default.
Measured basis: `E9_batch_feasibility.md` in the planning repository.

## Why

A sequential chapter takes about 267 s commit to commit. About 174 s is the translation
call and about 80 s are state reading and glossary review, which chain each chapter to
the one before it. The provider accepted 18 full chapters at once with no slowdown and
no change in cost per chapter. The limit is the chain, not the provider.

Translating chapters in parallel without help lost 12–20 points of term consistency,
almost all on terms first introduced inside the wave (a unit renamed in chapter 52 and
then called three different things in 53–54). Every one of those terms is in the source
before translation, so batch mode reads the source first.

## Pipeline

One batch covers chapters N..N+B-1 (B = `batch_mode.chapters`, default 50; it never
crosses the novel's final chapter). Every provider request goes through the existing
GOAT path: durable request journal, retry policy, budget, pause flag.

1. **Source pre-pass (parallel, source only).** One request per chapter, journal kind
   `batch_source_prepass`. It returns candidate terms (exact source key, proposed
   English, category, one source sentence) and a fact sheet: who appears with title and
   role, renames, deaths, moves, changes in forms of address (전하→폐하), time markers, and
   material source problems. It reads no English, so it does not depend on the previous
   chapter's translation.
2. **Consolidation.**
   - *Glossary.* Candidates are deduplicated locally by exact source key. Keys already in
     the glossary are dropped, so locked and existing terms never change. One-character
     keys are dropped too: they also occur inside ordinary words (Korean 원, 격), and
     plural markers (들, 们) are stripped. The pre-pass proposes at most 15 terms a
     chapter, under the same term rules as sequential state reading, and treats genre
     words (knight, king, mana) as ordinary vocabulary. In the first rehearsal, without
     these rules, a batch settled 333 new terms where sequential settled about 80 for
     the same chapters, most of them common words. Before the decision, each candidate
     in most of the batch's chapters is marked `widespread` (usually a common word), and
     a Korean key counts only where it starts a word: 각하 ("Your Excellency") inside
     생각하다 ("to think") is not a use. A key found mostly inside longer words is marked
     `inside_other_words`, and both the decision and the pre-translation audit are told
     that a key which is also an ordinary word or a homonym (지배 "rule", 비약 "leap")
     must be contextual, with the ordinary meaning among its renderings, or rejected, so
     its English is never forced onto ordinary uses. For an existing glossary,
     `glossary_homonym_report.py` lists such keys read-only, locked ones first, for the
     owner to decide. Candidates proposed with the same English as another candidate or an existing
     entry are listed together as likely variants to merge. Keys that differ only by
     spaces are merged locally into one entry, and so are keys settled on exactly the
     same English and category (an existing entry, else the most widespread form).
     Shared names: a candidate that contains another term or an established entry (궤른 in
     궤른 가문) is shown with that term's English, which the shared part must keep; a form
     that only adds a house word, title or full name to an existing name, and whose
     English follows from its parts, is rejected as derivable. Endings that several terms
     render with competing words (군단 as Corps and as Legion, 가문 as House and as family)
     are listed so the decision picks one. After the decision, any term whose English drops
     a contained term's English, and any ending still rendered two ways, goes on the review
     list. In the terms-only rehearsal, 48 of 89 batch-only terms were such variant forms. A key must
     occur literally in some batch chapter's source; its first such chapter is where it
     enters the glossary at commit time. One request (`batch_glossary_decision`) decides
     every remaining key at once from counts, proposed renderings and example sentences,
     with renames listed in chapter order. Each decision echoes its source key; one
     filed under the wrong id is ignored. Each carries a confidence; `low` confidence,
     and keys whose proposals disagreed, go on the review list (the run continues).
     The same request settles the English heading of every batch chapter, following
     the last committed heading, and writes the memory as of the batch's last chapter.
     Candidates are split into decision requests of at most 60, all sent at once.
   - *Timeline.* Local and ordered. Fact sheets are applied chapter by chapter to a table
     of people (latest title, role, location, alive) plus a list of events. Chapter k's
     context packet is the committed continuity at batch start plus everything the
     sheets of N..k-1 established, trimmed to a fixed size, people present in chapter k
     first. Chapter k therefore knows the story as of k-1 although k-1 is not
     translated yet.
3. **Parallel translation.** Each request uses the chapter's normal `chapter_translation`
   journal slot, so pending recovery, provenance checks and the filter-refusal retry
   treat it exactly like a sequential translation. Each chapter gets the
   production translation request with: the existing glossary plus the batch decisions,
   filtered to its own source as today; its context packet as continuity; the settled
   heading of chapter k-1 as the previous heading; and the last 600 characters of k-1's
   source as context only. Requests go through a rolling pool of at most
   `batch_mode.max_parallel` (default 32): the next starts as soon as any one finishes,
   so a slow chapter holds up only itself. The first is sent alone to warm the prompt
   cache, which fills while the provider reads the prompt, so the rest follow 30 seconds
   later rather than after its answer. The first pre-pass request is the exception: the
   rest wait for its answer, so a broken pre-pass costs one request, not fifty. Only the
   timing changes; each chapter gets exactly the same request either way. In the first
   rehearsal, waves of 16 that each waited for their slowest request (up to 6 minutes
   for a translation) took 13 minutes for the pre-pass and 22 for translation.
4. **Post-pass checks (local, free).** For every settled batch term, each chapter whose
   source has the key must use the decided rendering (any approved rendering for a
   contextual term). If a chapter uses a different proposal for the same key, that
   variant is replaced, source-anchored, and logged. A chapter still missing at least
   three settled terms, and at least 30% of those its source contains, or (Korean only,
   as the commit check) at least three fewer lines of speech than the source, is
   translated once more with the same request; the first
   translation moves to `backup/superseded_translations/` and its cost stays counted.
   Anything still missing is flagged for review. The usual commit QA, repairs and
   checks run later at commit, unchanged.
5. **Ordered commits.** For k = N..N+B-1 in order, through the normal lifecycle loop and
   the unchanged `process_chapter`. Three steps take their answer from the batch
   instead of a new request: translation replays the recorded batch answer (with the
   check repairs), and `process_chapter` saves it to `output/pending/` exactly as a
   fresh translation; state extraction returns the memory after chapter k and the
   batch terms first seen in k as `new_terms`; glossary review returns the batch
   decision for each candidate. The state and review answers still go through their
   saved checkpoint identities, so a crash mid-commit replays exactly. If a periodic
   audit corrected a term after the batch translated chapter k, the correction is
   applied to k at commit, source-anchored and under the retroactive safety rule, as
   a sequential chapter would already have it. Periodic audits, trial stops,
   retroactive corrections and checkpoint backups run after commits as today.

Every batch is built from a fixed snapshot of the glossary (`base_glossary.json`),
saved when it starts, so all of its requests see the same glossary even while earlier
chapters are being saved. No batch extends past a trial's last chapter.

**Pre-translation term audit.** Between settling the terms and translating, the batch's
settled terms go through the periodic audit's own prompt and source-backed targets
(the same source contexts and chunks of 20), all chunks sent together (`batch_term_audit`).
Its corrections, rejections, contextual renderings and merges change the settled terms,
so a wrong rendering is fixed before any of the batch's chapters is translated with it,
not after. A chunk that fails leaves its terms as settled and is flagged. The changes
go on the review list. The periodic audit still runs after the chapters are saved, as
the backstop for problems across batches, queued corrections and retroactive fixes. In
the rehearsal, the periodic audits of a batch glossary corrected 5 terms and merged 7
variants that the batch decision had let through.

**Batches end on audit boundaries.** A batch ends at the last periodic-audit boundary
in its range when that still leaves at least half a batch (with `audit_interval` 50,
chapters 53–100, then 101–150), so each audit covers whole batches and never lands
while one is half-saved.

**The last batch.** A batch never extends past the novel's final chapter, so the last one
is as short as the novel requires. If fewer than 10 chapters (and fewer than a quarter
batch) would be left after a batch, it takes them too, rather than leaving a short
batch with the same fixed steps, or one chapter on the sequential path: a novel
ending at 351 gets 301–351, not 301–350 and then 351 alone.

## Overlapping batches (`batch_mode.overlap`, opt-in)

Batch B, the next 50 chapters, starts in the background as soon as batch A's terms are
settled and audited, while A is still translating. Everything B needs from A exists at
that point, so B does not wait for A's English:

- *Glossary:* B's snapshot is the current glossary plus A's audited terms. A's terms are
  therefore established for B: never decided again, sent to every B chapter with A's
  English, and followed by new related names through the shared-name rule.
- *Story memory:* B starts from A's end-of-batch memory.
- *Boundary:* B's first chapter gets A's settled heading for its last chapter and the end
  of that chapter's source.
- *Audit corrections:* if a periodic audit corrects a term after B was translated, B's
  chapters get the correction at their commit (only real corrections: not an alias
  merge, and not an English that only adds words to the old one).
- *A term the commit step left out:* if one of A's terms never reached the glossary, the
  first B chapter that uses it adds it.

B starts batch C the same way once its own terms are audited, so reading and settling
one batch overlaps translating the one before. At most three batches are unfinished at
once (one being saved, two prepared ahead); if saving falls behind, the next one starts
when its chapters come due. All of them share one limit of `max_parallel` requests in
flight and one budget, and a batch prepared ahead leaves $1.50 free for audits.
Chapters are saved strictly in order through the same path. Only the main thread writes
`progress.json`: a batch prepared ahead records its helper costs in its manifest, which
count toward the run budget, and the main thread accounts them when its chapters come
due. A failure in a batch prepared ahead is raised when its first chapter is due. A
request with no recorded answer blocks the shared budget, as it does in a sequential run,
so the whole run stops cleanly and the request gets its one recorded resend on the next
start. When a run ends, no new batch starts and the run waits for those in flight.

In the first overlap rehearsal the next batch started only after the previous one had
translated, which saved just the 5-minute audit per batch (52 minutes for 98 chapters).
Starting after the term audit instead is expected to bring the steady state from about
25 to about 12–13 minutes per 50 chapters.

## Off-peak only (`off_peak_only`, opt-in)

`novel.json` `"off_peak_only": true` makes a Command Code run wait locally, with zero
requests, during the weekday peak windows (01:00–04:00 and 06:00–10:00 UTC), when the
rate card is twice the off-peak price. A request started off-peak is priced off-peak.
It applies to sequential and batch runs alike.

## Recovery

`data/batch/<first chapter>/manifest.json` records the batch range, status, timings and per-chapter
results. Each request's exact payload is saved under `requests/` before it is sent, so a
restart rebuilds the identical journal identity and replays recorded answers without a
new call. A crash anywhere re-enters at the first unfinished phase:

- pre-pass and translation: recorded answers replay; only unanswered chapters are sent;
- consolidation: the decision replays from its journal record;
- commit: the next chapter's pending translation is reused as in sequential mode; a crash
  after the last commit but before the batch is marked complete is finished on restart.

Unanswered requests follow the normal ambiguous-request rule (one recorded resend).

The first pre-pass request is sent alone. If its answer is unusable, nothing else is
sent: the batch is abandoned and that chapter runs the sequential path, and the next
chapter starts a new batch. A later chapter whose pre-pass is unusable ends the batch
before it. A chapter whose batch translation comes back unusable (cut off, refused)
is set aside with its cost counted and, at its turn, takes the normal sequential
translation request, still with the batch's terms in its glossary and with batch
state and review; it is flagged. A batch never covers a chapter that already has a
pending, committed or journaled translation. An unfinished batch is finished even
if batch mode is later switched off, so no paid batch translation is discarded.
Batch translations not yet committed count toward the novel's budget.

## Budget

Batch helper requests send an explicit completion ceiling of 64k tokens, so each
reserves its real bound instead of the whole context window. Translation requests
already have one. Reasoning counts toward the ceiling: in the first rehearsal a 16k
pre-pass ceiling was used up by reasoning alone and returned no text. Such an answer
is counted and treated as unusable; it does not block the run. The GOAT budget is now safe for parallel use: each in-flight
request reserves its own bound under a lock and releases it when it is accounted, so the
sum of reservations never exceeds the remaining budget. The pool starts a request only
when the worst case of everything in flight still fits; if not even one request fits,
the run stops with `ALLOWANCE_EXHAUSTED` as today.

At peak rates a translation or a batch helper request reserves about $0.08, so 32 in
flight hold about $2.60 aside, not 32 × $1.50.

The plan's weekly usage quota still applies. Batch mode spends it faster per hour, not
more per chapter.

## Risks and guards

- *One wrong decision reaches 50 chapters.* Low-confidence and disputed keys are flagged
  with their chapter. The periodic audit and safe retroactive corrections still apply.
- *Story-state drift over long batches.* The sheets are source-only and ordered, so state
  does not drift with translation errors. Rehearsal measures it with a sampled read.
- *Heading drift.* Settled once per batch.

## Not changed

Pending recovery, the request journal, retries and the ambiguous resend rule, budget
limits, commit QA and its repairs, the commit protocol, glossary audit, retroactive
correction safeguards, checkpoint backups, pause flag, and the sequential path when
batch mode is off.

## Rehearsal

`batch_rehearsal_compare.py` compares two copies of a rehearsal novel translated over the
same chapters, one sequential and one batch, for term consistency on terms first seen
in the range, cost, and wall time, and writes a sample list for the story-state read.
Paid runs happen only in the rehearsal area and only after the owner approves the spend.
Each copy keeps the novel's id, which its audit history is bound to, so each lives in its
own novels folder.

```powershell
$Out = 'D:\NovelTranslator_Dev\rehearsal\batch_rehearsal'
python batch_rehearsal_compare.py prepare --novels-root D:\NovelTranslator_Dev\rehearsal\novels --out $Out --novel rogue_check --chapters 50
$env:NOVELTRANSLATOR_NOVELS_ROOT = "$Out\seq\novels";   python translate.py --novel rogue_check --start --budget-usd 4 --trial 50
$env:NOVELTRANSLATOR_NOVELS_ROOT = "$Out\batch\novels"; python translate.py --novel rogue_check --start --budget-usd 4 --trial 50
python batch_rehearsal_compare.py compare --out $Out --novel rogue_check
```

`prepare` copies the novel (no provider calls) and refuses a folder outside a rehearsal
root; `--arm terms` makes a terms-only copy (`batch_mode.terms_only`), which reads the
source and settles terms, then stops without translating, and `terms` compares its
terms with the sequential copy's glossary. `compare` writes `reports/batch_rehearsal_<novel>.md`
and `.json` under `--out`. Results of the first rehearsal are in the planning repository,
`E9_batch_rehearsal.md`. The terms metric is the share of uses, in chapters from the range, of
terms first seen in the range that use the rendering the arm's own glossary settled
on; the batch arm is also scored against the sequential arm's renderings.
