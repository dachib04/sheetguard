# ADR 0001: Formula engine for the Evaluator

- Status: accepted
- Date: 2026-11-21
- Spec references: §4.2, FR-21, FR-22, risk "Formula engine does not support functions used in real models"

## Context

The Evaluator recalculates a target cell after perturbing one input (driver
analysis, FR-21) on a copy of the model. It must never guess: if a function on
the path is unsupported the result is marked partial (FR-22). The spec names
two candidates, `formulas` and `pycel`, and asks for a choice by *measured*
coverage in week 1.

## What we measured

Environment: Python 3.13, openpyxl 3.1.5, formulas 1.3.4, pycel 1.0b30.
Scripts: `bench/generators/engine_probe.py`.

**1. Static function coverage** against a 92-function list of what appears in
real DCF, budget, loan and forecast models:

| Engine   | Covered | Notable gaps |
|----------|---------|--------------|
| formulas | 85 / 92 | OFFSET, INDIRECT, SUBTOTAL, AGGREGATE, LAMBDA, SEQUENCE (LET works via the parser) |
| pycel    | 60 / 92 | PMT, IPMT, PPMT, FV, RATE, NPER, IRR, XIRR, XNPV, XLOOKUP, XMATCH, COUNTA, MEDIAN, STDEV, SQRT, EXP, FILTER, SORT, UNIQUE … |

**2. Whole-workbook evaluation on the three reference models** (value of the
model output vs the cached value written by the spreadsheet application):

| Model  | Target       | Cached        | formulas                 | pycel |
|--------|--------------|---------------|--------------------------|-------|
| loan   | Schedule!F61 | ≈ 0 (9.7e-11) | ≈ 0 (-5.9e-09), 1.2 s    | fails: `PMT` not implemented |
| dcf    | Model!B12    | 2,086,798.866 | 2,086,798.866 (exact), 0.8 s | fails: incompatible with openpyxl 3.1 defined names |
| budget | Summary!B4   | 0.69767       | fails: cannot parse `BudgetTable[Total]` | 0.69767, 0.2 s |

Re-calculation after a +10% input change: 13–71 ms with `formulas`, 14 ms with pycel (where it worked).

**3. Per-formula compilation in `formulas`** (`Parser().ast(f)[1].compile()`):
works for every formula on all three models once structured references and
defined names are rewritten to A1; about 0.75 ms per formula; unsupported
functions are identifiable before evaluation.

## Decision

Use **`formulas` as a per-formula function library, driven by SheetGuard's own
dependency graph**, not as a whole-workbook engine:

1. `DepGraph` already resolves defined names (with sheet-scope shadowing),
   structured table references, 3D references and whole-column clipping. The
   Evaluator walks only the sub-graph between the perturbed input and the target.
2. Each formula on that path is rewritten to plain A1 (names and table refs
   replaced by the ranges the graph resolved) and compiled with `formulas`.
3. Before evaluating, every function on the path is checked against the
   implemented set. INDIRECT and OFFSET (dynamic references), and any
   unimplemented function, make the result `partial` with the function listed
   (FR-22). We never fall back to the cached value or an approximation.
4. Compiled functions are cached per relative-R1C1 pattern, so a schedule with
   10,000 identical rows compiles a handful of formulas, not 10,000.

`formulas` is an optional extra (`pip install sheetguard[eval]`). Without it,
`top_drivers` returns a clear "evaluator not installed" error, and every other
tool works.

## Consequences

- Good: broadest measured coverage (all financial functions we tested), exact
  results on the reference models, one source of truth for references (our
  graph), and FR-22 is enforced by us rather than trusted to a third party.
- Good: recalculating only the path keeps driver analysis fast on large models.
- Cost: we own the A1 rewriting of names/table refs; it is unit-tested against
  the same fixtures as the graph.
- Cost: `formulas` pulls in `numpy` and `schedula` (heavier install). Accepted
  because it is an optional extra.
- Risk: input order of compiled functions is alphabetical, not argument order;
  the Evaluator must bind inputs by name. Covered by a unit test.

## Rejected

- **pycel**: 25 fewer functions on the list, misses the core financial set
  (PMT, IRR, XIRR, NPER, RATE), and failed on 2 of 3 reference models.
- **`formulas` whole-workbook mode**: cannot parse structured references, and
  recalculates far more than the path we need.
- **Writing our own evaluator**: months of work to match Excel semantics; not a
  4-week project.

## Addendum (week 2, implementation)

- Placeholders are `_sg0`, `_sg1`... An underscore prefix means they can never be read as cell
  addresses (`SGR10` would be column SGR, row 10).
- Verified: every non-cyclic formula in the 7 recalculated reference workbooks re-evaluates to
  its cached value (tolerance 1e-9 relative), covered by `test_recalculation_matches_cached_values`.
- `formulas` needs real reference objects for ROW/ROWS/COLUMN/COLUMNS and returned `#VALUE!`
  for them, a silently wrong answer. We resolve these four from the reference's static shape
  (`ROWS(A2:A4)` -> 3, `ROW()` -> the cell's row) and treat CELL, AREAS, ISREF, FORMULATEXT and
  any other reference-shape use as unsupported (partial). Found by the cached-value test on the
  `structures` fixture.
- Driver analysis recomputes only the cells between the perturbed input and the target,
  reusing the baseline for everything else; tested against a full recalculation.
