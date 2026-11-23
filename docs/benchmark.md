# Benchmark

The benchmark is a deliverable, not an extra (spec §10.2). It is fully synthetic: every
workbook is generated from fixed seeds. No client or employer data is used.

```bash
python bench/run_bench.py            # committed corpus, ~10 s, writes bench/results/
python bench/run_bench.py --rebuild  # regenerate the corpus first (needs LibreOffice)
```

Latest results: [bench/results/results.md](../bench/results/results.md)
(machine-readable: `results.json`). The first run, before the fixes listed below, is kept in
[results_initial_run.md](../bench/results/results_initial_run.md).

## Corpora

| Corpus | Size | How it is built |
|--------|------|-----------------|
| Clean models | 24 | 6 families (DCF, budget with an Excel table, loan schedule, sales forecast, cap table, quarterly P&L) x 4 seeds; sizes, names, inputs and layouts vary. Recalculated headlessly by LibreOffice so cached values are real. |
| Seeded errors | 345 | From each clean model, up to 3 variants per error type, each with exactly one error at a recorded cell (`bench/corpus/seeded/manifest.json`). |
| Injection traps | 40 | 20 vectors x 2, direct phrasing in several languages. |
| Held-out paraphrases | 20 | 20 vectors x 1, injections written in natural paraphrase. **Never used to tune the scanner.** |
| Benign look-alikes | 54 | 18 vectors x 3: "Note: ignore row 5", "You must enter a positive number", "System: SAP export", hidden helper sheets, ordinary links. |

Seeded error types:

| Check | Mutation |
|-------|----------|
| C01 | An absolute reference to a numeric input is replaced by its value (`*(1+Inputs!$B$3)` becomes `*(1+0.083)`) |
| C02 | One relative reference in a pattern row is pointed at the wrong row |
| C04 | A formula is divided by zero, or one reference becomes `#REF!` |
| C05 | A formula is made to add `0*` one of its own dependents |
| C06 | A formula in a pattern row is overwritten with its cached value |
| C07 | A SUM range is shortened by one cell at the end |

Locations are chosen among interior cells of consistent formula runs (and SUM totals for
C07). A variant is discarded if its mutation accidentally creates a cycle, so each file has
exactly one known error.

## Scoring

- **Recall**: a seed is detected when an issue of the seeded check names a seeded cell (as
  its anchor or in its evidence).
- **Precision**: false positives are issues that are not in the clean parent's report and do
  not touch the seeded cell; every issue on a clean model is a false positive.
- **Co-detections**: another check also flagging the seeded cell (an overwritten formula is
  also an inconsistent formula). Reported, not counted as false positives.
- **Consequences**: findings on a direct input of the seeded cell that the parent did not
  have (an input left unused after its formula was overwritten). Reported separately.
- **Injection**: a file is detected when the scanner reports risk medium or higher;
  thresholds low and high are reported too. Benign files flagged at medium or higher are
  false positives.

## Results summary (v0.1.0)

| Check | Seeded | Recall | Precision |
|-------|-------:|-------:|----------:|
| C01 hardcoded constant | 45 | 100% | 100% |
| C02 inconsistent formula | 57 | 94.7% | 100% |
| C04 error value | 69 | 100% | 100% |
| C05 circular reference | 45 | 100% | 100% |
| C06 hardcoded value in formula row | 69 | 95.7% | 100% |
| C07 incomplete SUM | 60 | 100% | 100% |

No issues and no scanner findings on any of the 24 clean models.

| Injection set | Files | Flagged at medium or higher |
|---------------|------:|----------------------------:|
| Traps (direct phrasing) | 40 | 97.5% |
| Held-out paraphrases | 20 | **0%** (50% at low) |
| Benign look-alikes (false positive rate) | 54 | 9.3% |

Runtime: median 14 ms per model (load + 14 checks + scan), p95 32 ms.

## Changes made after the first run

The first run is published unchanged in `results_initial_run.md`. It exposed three defects,
each fixed with a regression test:

1. **C04 precision 45%.** Cells downstream of a circular reference also show `#VALUE!`,
   and C04 reported each as an independent root error (83 false positives). Errors flowing
   out of a cycle are now attributed to C05.
2. **C02 in columns.** A column of stacked P&L line items reached a 4-of-6 majority after a
   seeded constant split it, producing 2 false positives. Columns now need a 75% majority
   (rows still 60%).
3. **Scanner exfiltration pattern.** "Please send questions to finance@example.com" was
   rated medium. The pattern now needs something being sent (model, file, data...).

After an external review two more changes were made, neither tuned to the held-out set:
C06 no longer counts the seed of a roll-forward row as a pasted value (a 5-period row with
one paste was skipped as a data block; no benchmark number changed), and any text in a hidden
sheet, row or column is now at least low risk because of its location (paraphrases flagged
at low: 40% to 50%; benign look-alikes at low: 42.6% to 50%; medium and high unchanged).

The scoring also gained the "consequences" category above (2 C12 findings were true side
effects of overwritten formulas, not false positives).

**What was deliberately not changed:** the held-out paraphrase set. Writing patterns for
those 20 sentences would turn 0% into a meaningless 100%.

## Reading these numbers honestly

- **Same author, same assumptions.** The seeded errors, the clean models and the checks were
  written by the same person. Synthetic errors are cleaner than real ones. Real-world recall
  and precision will be lower; treat these numbers as a regression baseline, not a claim
  about real models.
- **Small sets.** 20 paraphrases and 54 benign files are small; one file moves a rate by
  2-5 points.
- **Short rows are invisible to C02/C06.** Both require 3 consistent neighbours (spec), so
  a P&L with 4 quarters cannot be checked row-wise. That is where all C02 and C06 misses
  come from.
- **Paraphrases defeat the scanner.** This is the central finding. Phrase heuristics catch
  the injections people copy from blog posts and miss instructions written naturally. The
  untrusted envelope is the defence that does not depend on detection.
- **Agent experiment** (does an LLM get misled with and without SheetGuard?): planned in
  [experiment-plan.md](experiment-plan.md), with a paired corpus (`bench/corpus/agent/`) and
  the harness in `bench/agent/`. It has **not been run** yet. No numbers are claimed.
