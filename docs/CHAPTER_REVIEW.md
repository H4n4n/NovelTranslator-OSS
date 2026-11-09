# Chapter review

`chapter_review.py` reads finished chapters against their source and lists what a careful
bilingual editor would fix: meaning errors, dropped passages, breaks of the novel's rules,
and names or terms that contradict the glossary. It uses the production model,
`deepseek/deepseek-v4.1-flash` on Command Code, and never changes a chapter. Fixes go in
afterwards, after a person or Claude has read them, with `translate.py --apply-corrections`.

```powershell
python chapter_review.py --novel <id> --chapters 11-50 --budget-usd 2
```

| Option | Default | Meaning |
|---|---|---|
| `--chapters` | required | One chapter or a range; only committed chapters are read |
| `--budget-usd` | required | The most this run may spend |
| `--parallel` | 32 | Chapters reviewed at once |
| `--reasoning` | `high` | `low` is faster and cheaper, `high` catches more |
| `--no-verify` | off | Skip the second request that checks the findings |
| `--run` | the range | Folder name under `output/review/` |

It reads only, so it can run while a translation is going.

## What it does per chapter

1. **Free checks:** source script left in the English, fewer dialogue lines than the source,
   far fewer paragraphs, glossary terms in the source with no rendering in the English.
2. **Find:** one request with the numbered source and English paragraphs, the novel's
   `translation_addendum.txt`, and the glossary entries that occur in the chapter.
3. **Local filter:** a finding is kept only if its English occurs exactly once in the
   chapter, the fix changes it, and the source words it quotes are in the chapter source.
4. **Verify:** one more request, only when findings survived, shows each one with its
   paragraphs and asks to keep it, improve the fix, or drop it.
5. **Output** in `output/review/<run>/`:
   - `chapters/NNNN.json`: findings, verdicts and what was dropped and why;
   - `report.md`: fixes with the riskiest first (omissions, meaning, terms, rules), free
     flags, and dropped findings;
   - `candidates_<first>_<last>.json`: the kept fixes in the `--apply-corrections` format.
     Each is tied to source words found in that chapter, preferably in no other, written with
     the chapter file's line endings, grown to whole words, and dry-run so it changes its own
     chapter exactly once and no other chapter.

## Safety and cost

- Every request goes through the paid request journal: a recorded answer is replayed, never
  sent again. A request with no recorded answer is sent once more on the next run, as in
  production. Rerunning the same command costs nothing for chapters already reviewed.
- Definite provider errors are retried with backoff and logged.
- Each request reserves its worst case (an explicit completion ceiling) before it is sent,
  so the requests in flight together never pass `--budget-usd`. A refused chapter is listed
  as not reviewed and is reviewed on the next run.
- `pause.flag` and the novel's `off_peak_only` hold every request, as in a translation run.
- It writes only under `output/review/`.

A chapter whose English changed since its review is reviewed again; if its journal was
already written for the old English, review it under a new `--run`.
