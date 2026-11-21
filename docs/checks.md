# Check catalog

Every issue has: `id` (stable hash), `check_id`, `severity` (critical / warning / info),
`sheet`, `cell`, a one-sentence `description`, `evidence` (formulas and values, wrapped as
untrusted text), `confidence` (high / medium / low), optional `suggested_fix`, and
`affected_count`.

Issues are sorted by severity, then sheet order, then row and column, so the same file
always produces the same report.

| ID  | Check | Severity | Confidence |
|-----|-------|----------|------------|
| C01 | Hardcoded constant inside a formula | warning | medium |
| C02 | Inconsistent formula in a row or column | critical | high / medium / low |
| C03 | Reference to an empty cell | warning | high |
| C04 | Error values (root cause, propagation counted) | critical (info/warning when handled) | high |
| C05 | Circular reference | critical (warning if iterative calc on) | high |
| C06 | Hardcoded value in a formula row | critical | high / medium / low |
| C07 | Incomplete SUM range | warning | high / medium |
| C08 | External workbook link | warning | high |
| C09 | Volatile or fragile functions | info | high |
| C10 | Possible unit or scale mismatch | info | low (by design) |
| C11 | Hidden logic | warning | high |
| C12 | Unused input | info | medium |
| C13 | Stale or missing cached values | warning (info if partial) | high |
| C14 | Numbers stored as text | warning | high |

Repeated patterns are grouped: the same formula shape in 500 cells is one issue with
`affected_count: 500`, anchored at the first cell, with up to three cells as evidence.
Workbook-level issues (C08 unused links, C13) use the cell label `(workbook)`.

### Shared heuristics

- **Year / period labels**: an integer constant next to a consecutive integer (2026 next to
  2027, period 3 next to 4), or next to a `=B1+1` roll-forward header, is a label. C06, C07,
  C12 and driver analysis ignore it.
- **ID columns** (C14): the nearest text header above the cell matches id, code, zip,
  postal, phone, account, sku, ref, number, no., #, iban, isin, ticker or key.
- **Row label**: the first non-empty cell to the left, if it is text.

-----|-------|----------|--------|
| C01 | Hardcoded constant inside a formula | warning | week 2 |
| C02 | Inconsistent formula in a row or column | critical | **implemented** |
| C03 | Reference to an empty cell | warning | week 2 |
| C04 | Error values | critical | **implemented** |
| C05 | Circular reference | critical | **implemented** |
| C06 | Hardcoded value in a formula row | critical | week 2 |
| C07 | Incomplete SUM range | warning | week 2 |
| C08 | External workbook link | warning | week 2 |
| C09 | Volatile or fragile functions | info | week 2 |
| C10 | Possible unit or scale mismatch | info | week 2 |
| C11 | Hidden logic | warning | week 2 |
| C12 | Unused input | info | week 2 |
| C13 | Stale or missing cached values | warning | week 2 |
| C14 | Numbers stored as text | warning | week 2 |

---

## C02 Inconsistent formula (critical)

**Detection.** For every contiguous run of formula cells in a row, and in a column, each
formula is rendered in relative R1C1 form (`=B3*C3` in D3 becomes `=RC[-2]*RC[-1]`).
Whitespace and function-name case are ignored. If one pattern is shared by at least 3 cells
and at least 60% of the run, each cell with a different pattern is an outlier.

**Suggested fix (FR-30).** The nearest consistent neighbour's formula, translated to the
outlier's position (`=E2*E3` for an outlier in E4 whose neighbours read `=D2*D3`).

**False-positive policy.**
- A cell at either end of a run is not part of the comparison when it is a *total*: it uses
  an aggregate (SUM, SUBTOTAL, AVERAGE, …) over a range covering two or more cells of the
  same run.
- The first cell of a run is skipped when the majority pattern refers to the previous cell
  along the run (`C[-1]` in a row, `R[-1]` in a column). A roll-forward formula cannot apply
  to the opening period, so opening balances are expected to differ.
- Any other outlier at the edge of a run is reported with **low** confidence.
- Confidence is **high** when the majority holds at least 80% of the run, otherwise medium.
- A cell flagged both by its row and by its column is reported once, with the higher
  confidence.

## C04 Error values (critical)

**Detection.** Cells whose cached value is `#REF!`, `#DIV/0!`, `#N/A`, `#VALUE!`, `#NAME?`,
`#NUM!`, `#NULL!` (and the newer `#SPILL!`, `#CALC!`, …). Errors are reported once at their
**root**, the cell whose precedents are error-free. Cells the error propagates to are counted
in `affected_count` and the first five are shown as evidence. A literal `#REF!` inside a
formula is always reported, even when the cached value is masked or missing.

**False-positive policy.**
- `#N/A` that every dependent handles with IFERROR / IFNA / ISNA / ISERROR is **info**.
- A deliberate `=NA()` with nothing depending on it (chart gaps) is **info**.
- Any other error fully masked downstream by IFERROR is **warning**: the model still has a
  broken calculation, it is just hidden.
- Error values on cells that are part of a circular reference are left to C05.
- If the workbook has no cached values at all, C04 cannot see errors; C13 reports that.

## C05 Circular reference (critical)

**Detection.** Strongly connected components of the dependency graph (iterative Tarjan),
looking through range nodes, so `=SUM(A1:A10)` in A5 is a cycle. One issue per component,
anchored at its first cell, listing up to 10 cells.

**False-positive policy.** If the workbook has iterative calculation enabled, the cycle is
probably intentional (interest on average cash balance, for example) and is reported as
**warning**.

## C01 Hardcoded constant inside a formula (warning)

**Detection.** Numeric literals in a formula that has at least one reference, other than the
allow-list: 0, 1, 2, -1, 12, 100, 1000 and common date/time parts (4, 7, 24, 30, 31, 52, 60,
360, 365, 366, 3600, 86400). Percent literals (`5%`) count.

**Fix (FR-31).** The formula with the literal replaced by a suggested named input
(`=A1*(1+5%)` becomes `=A1*(1+Input_5)` after creating `Input_5 = 5%`).

**False-positive policy.** Allow-list is configurable. Structural arguments are ignored
(ROUND digits, VLOOKUP/HLOOKUP column and mode, INDEX/MATCH/XLOOKUP positions, OFFSET sizes,
LEFT/RIGHT/MID lengths, LARGE/SMALL k, SUBTOTAL/AGGREGATE function numbers, DATE/TIME parts,
EDATE/EOMONTH months, TEXT formats...). Cells a defined name points at are named inputs and
are ignored. A formula with no references (`=250000`) is an input written as a formula.

## C03 Reference to an empty cell (warning)

**Detection.** A direct single-cell reference to a cell with no value and no formula (a cell
holding `""` is not empty).

**False-positive policy.** References inside SUM/COUNT-style aggregates, IS* tests,
COUNTBLANK, N, T, LEN, CONCAT/TEXTJOIN are ignored; ranges are never flagged; missing sheets
are left to the graph's partial flags.

## C06 Hardcoded value in a formula row (critical)

**Detection.** In a contiguous run of numeric cells (row or column), at least 3 formulas
share one R1C1 pattern (and at least 60% of the run's formulas) and a constant sits among
them. Runs where constants exceed one cell in four are data blocks and skipped.

**Fix (FR-30).** The nearest consistent neighbour's formula for this position.

**False-positive policy.** First cell skipped for roll-forward patterns (seed value), year
and period labels skipped, constant must touch a pattern formula; high confidence when
formulas surround it on both sides, low at the run's edge.

## C07 Incomplete SUM range (warning)

**Detection.** A one-dimensional range in SUM stops exactly one cell short of its block: the
cell just past either end is the same kind (numeric constant, or a formula with the block's
R1C1 pattern) and the block ends right after it.

**Fix.** The formula with the range widened (`=SUM(I2:I6)` becomes `=SUM(I2:I7)`).

**False-positive policy.** Only adjacent, same-type blocks; the extra cell must not be used
elsewhere in the formula (`=SUM(B2:B9)+B10`); a block that continues further is a deliberate
partial sum; a year label before the start is ignored. High confidence at the end side,
medium at the start side.

## C08 External workbook link (warning)

**Detection.** Formulas or defined names referencing another workbook, and external-link
parts no formula uses (DDE flagged). Never resolved, opened or fetched (S-7, S-9).

## C09 Volatile or fragile functions (info)

**Detection.** NOW, TODAY, RAND, RANDBETWEEN (change on every recalculation), INDIRECT,
OFFSET (references built at run time; traces through them are marked partial, and driver
analysis returns a partial result).

## C10 Possible unit or scale mismatch (info, low confidence by design)

**Detection.** Constants formatted as percentages whose magnitude is above 1 (25 shown as
2500%); SUMs over constants whose magnitudes differ by 10^5 or more, or whose number formats
use different thousands scales (`#,##0,` vs `#,##0`).

## C11 Hidden logic (warning)

**Detection.** Formulas or numeric inputs in hidden / very hidden sheets, hidden rows or
hidden columns that other cells depend on. One issue per hidden sheet, row or column.
**Policy.** Report, do not judge intent.

## C12 Unused input (info)

**Detection.** Numeric constants nothing depends on, grouped by vertical run. A sheet with no
formulas that nothing refers to is reported once. Labels, text and year/period labels are
ignored; workbooks without formulas are skipped.

## C13 Stale or missing cached values (warning)

**Detection.** No formula has a cached value (written by a script, never calculated), or
manual calculation mode. Some-but-not-all formulas uncached is info.
**Policy.** Driver analysis never uses cached values; it always recalculates.

## C14 Numbers stored as text (warning)

**Detection.** Text constants that read as numbers (`1,234.50`, `$99`, `(250)`, `12%`,
`100 EUR`) that a formula uses. ID/code columns (header heuristic) and leading-zero codes
(`00123`) are ignored. Fix shows the numeric value.
