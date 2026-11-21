# Architecture

One core library, three thin front ends. No front end contains business logic.

```
   MCP server (stdio)        CLI (Typer)         Python API
           \                    |                    /
            +-------------- sheetguard.api ---------+
                                |
  SafeLoader -> Workbook model -> DepGraph -> Checks / Tracer / Drivers -> Report
       |                                                         ^
       +-> Injection scanner -> untrusted-content findings ------+
```

## Data flow

1. **`security.archive`** sniffs the real format and streams every zip member,
   measuring true decompressed sizes, rejecting DTDs/entities in any XML part,
   counting cells and recording active content. Nothing is parsed yet.
2. **`core.loader`** opens the same in-memory bytes with openpyxl twice:
   formulas + formatting first, then cached values. It builds the immutable
   **`core.model.Workbook`** and drops the openpyxl objects.
3. **`core.graph.DepGraph`** tokenises every formula (`core.refs`) and builds a
   graph of cell, range and defined-name nodes.
4. **Checks**, the **tracer**, and the **evaluator** / **drivers** query the graph.
5. Everything leaves as Pydantic models from **`report.schema`**, with every
   workbook-originated string wrapped by **`security.envelope`**.

## Key design decisions

| Decision | Why |
|----------|-----|
| Read the file into memory once, never write | S-1 read-only by construction; no TOCTOU gap between checks and parsing; S-10 satisfied trivially (no temp files at all) |
| Reject any `<!DOCTYPE`/`<!ENTITY` before parsing | OOXML never needs a DTD, so this closes XXE and entity bombs regardless of which XML parser runs later (defense in depth with defusedxml) |
| Measure real decompressed bytes | Zip headers can lie; S-4 must hold for hostile files |
| Range nodes instead of edge expansion | `SUM(A1:A50000)` is one edge, not 50,000; whole-column refs clipped to the used range (FR-10) |
| Defined names as graph nodes | Traces show `TaxRate` as a step; sheet-scope shadowing resolved once |
| Iterative Tarjan for cycles | Chains of thousands of cells cannot overflow the Python stack |
| BFS tracing with an `expanded` set | Nearest cells first under truncation; terminates on any graph (FR-12/13) |
| Frozen dataclasses for cells, Pydantic at the boundary | Millions of cells stay cheap; public I/O is validated and versioned |
| Own reference parser on top of openpyxl's tokenizer | Structured refs, 3D refs, external refs, quoted/unicode sheet names all classified explicitly |
| Formula evaluation: `formulas` per formula, driven by our graph | See [ADR 0001](adr/0001-formula-engine.md) |

## Module map

```
src/sheetguard/
  api.py              Analysis object: audit, trace, likely_outputs
  cli.py              Typer CLI, exit codes 0/1/2/3
  errors.py           Typed errors with stable codes
  core/refs.py        A1/R1C1, sheet quoting, reference classification
  core/model.py       Immutable workbook model
  core/loader.py      SafeLoader
  core/graph.py       DepGraph, cycles, likely outputs, upstream inputs
  core/tracer.py      Bounded precedent/dependent trees
  core/evaluator.py   Path-only recalculation on top of `formulas` (ADR 0001)
  core/drivers.py     Sensitivity analysis (FR-20..24)
  core/fixes.py       Text-only fix suggestions (FR-30..32)
  core/heuristics.py  Year/period labels, ID columns, row labels
  checks/             One module per check, registry in __init__
  security/archive.py Zip/XML pre-flight
  security/limits.py  All limits + cooperative deadline
  security/paths.py   Allowed-root path resolution
  security/envelope.py UntrustedText wrapper + annotation with scanner verdicts
  security/injection.py Prompt-injection scanner (§7.2)
  mcp/tools.py        The 8 tools: store (LRU + idle timeout), pagination, size fitting
  mcp/server.py       MCP wiring over stdio: timeouts, structured errors, entry point
  report/schema.py    Public versioned models
  report/json_report.py  Deterministic JSON report + JSON Schema export
  report/html_report.py  Static HTML report (autoescape + CSP, no JS)
```

## Performance (measured for v0.1.0)

`python bench/perf.py 1000 20000` builds synthetic workbooks (10 formulas per row, products
and SUMs) and runs load + all 14 checks + the injection scan, in a fresh process per size.
Python 3.13, Linux:

| Formula cells | Load + graph | Checks + scan | Total  | Peak RSS |
|---------------|--------------|---------------|--------|----------|
| 10,002        | 0.47 s       | 0.55 s        | 1.03 s | 76 MB    |
| 200,002       | 14.5 s       | 20.5 s        | 35.0 s | 649 MB   |

Targets (NF-1, NF-2): < 5 s, < 60 s, < 1.5 GB. Tokenising, context walks and R1C1 rendering
are memoised per formula string (bounded LRU caches); every check reads the same formulas.
Real models in the benchmark corpus take a median of 14 ms each.
