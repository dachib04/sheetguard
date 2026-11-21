# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-21

First public release (Phase 1).

### Added
- **SafeLoader** for `.xlsx` / `.xlsm`: format sniffing, zip-bomb checks on real
  decompressed sizes, rejection of any XML DTD/entity, file/cell/formula/sheet limits,
  active-content detection (VBA, XLM, OLE, ActiveX, DDE, connections), typed errors.
- **Workbook model and dependency graph** with defined names (sheet-scope shadowing),
  structured table references, 3D and external references, whole-column clipping,
  iterative cycle detection, likely-output detection.
- **Tracer**: bounded precedent/dependent trees with explicit truncation.
- **14 checks** (C01-C14) with documented false-positive policies and fix suggestions for
  C01, C02, C06, C07, C14.
- **Evaluator and driver analysis** on top of `formulas` (ADR 0001): path-only
  recalculation, partial results instead of guesses.
- **Prompt-injection scanner** over every vector in spec §7.2, with normalisation of
  zero-width characters and homoglyphs, and the `untrusted_text` envelope on every string
  that comes from a workbook.
- **MCP server** (`sheetguard-mcp`, stdio) with 8 read-only tools, pagination, a 20 KB
  response cap, an LRU workbook cache with idle timeout, per-call timeouts and structured
  errors.
- **CLI**: `audit` (text / JSON / HTML), `trace`, `drivers`, `outputs`, `schema`, `mcp`.
- **Reports**: deterministic JSON with a published JSON Schema; static HTML with no
  JavaScript and a Content-Security-Policy.
- **Benchmark**: 24 clean models, 345 seeded-error variants, 40 injection traps, 20 held-out
  paraphrased injections, 54 benign look-alikes; one-command reproduction.
- Dockerfile (non-root, hash-pinned dependencies), CI on Linux/macOS/Windows x Python
  3.11/3.12, release workflow (PyPI trusted publishing, GHCR).

### Fixed (found by the benchmark before release)
- C04 reported errors flowing out of a circular reference as independent root errors.
- C02 flagged stacked line items in P&L-style columns; columns now need a 75% majority.
- The injection scanner treated "send questions to an email address" as exfiltration.

### Fixed (found in external review before release)
- C06 missed a value pasted over a formula in short roll-forward rows (e.g. 5 periods): the
  row's seed was counted as a pasted value, so seed + one paste looked like a data block.
- The "never writes a file" security test could fail on a fresh install because Python
  writes `.pyc` caches during the first import; it now warms up before guarding.

### Changed
- Any text in a hidden sheet, row or column is now reported as at least low risk, whatever
  its wording. The HTML report lists the first 200 untrusted items; JSON and MCP keep all.

### Known limitations
- Injection detection does not generalise to paraphrased instructions it has no
  pattern for (0% at medium risk on the held-out set). The envelope, not the scanner, is
  the primary defence.
- C02 and C06 need at least 3 consistent neighbours, so rows with fewer than 4 formula
  periods are not checked.
- `.xls`, `.xlsb`, `.ods` and `.csv` are not supported.
