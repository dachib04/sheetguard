# Acceptance checklist (Phase 1 spec v1.0)

Every requirement ID in the spec, its status, and the evidence. Status legend:
**done** (implemented and tested), **partial** (gap stated), **pending** (needs something
outside the build environment: a GitHub push, PyPI or registry access), **cut** (MAY item, not
built).

Last verified for v0.1.0: 343 tests pass on Python 3.11, 3.12 and 3.13 (Linux), 94% coverage,
`mypy --strict` and `ruff` clean.

## 5.1 Input handling

| ID | Level | Status | Evidence |
|----|-------|--------|----------|
| FR-1 | MUST | done | `.xlsx`/`.xlsm` accepted; macros detected, never run: `test_xlsm_macros_detected_not_executed` |
| FR-2 | MUST | done | `.xls`, `.xlsb`, `.ods`, encrypted, corrupted, renamed `.docx` rejected with typed errors: `tests/malicious/test_malicious.py` |
| FR-3 | MUST | done | Formula + cached value per cell; no cache reported as C13, not a failure: `test_no_cached_values_reported_not_failed` |
| FR-4 | MUST | done | Names (scope shadowing), tables (incl. `[#This Row]`, renamed tables), cross-sheet, 3D: `test_graph.py`, `test_table_renamed_after_creation` |
| FR-5 | MUST | done | Sheet/row/column hidden state, font/fill colour, font size: `test_styles_hidden_state_and_text_vectors` |
| FR-6 | SHOULD | partial | Shared formulas expand per cell (`test_shared_formulas_expand_per_cell`), array/dynamic formulas load; merged cells are recorded but C02 has no special merged-cell logic |
| FR-7 | MAY | cut | `.csv` not supported (first item in the spec's cut order) |

## 5.2 Dependency graph and tracing

| ID | Level | Status | Evidence |
|----|-------|--------|----------|
| FR-10 | MUST | done | Graph across sheets; `A:A`/`1:1` clipped to the used range: `test_whole_column_bounded_by_used_range` |
| FR-11 | MUST | done | Tree nodes with address, formula, value, type, `is_input`: `test_tracer.py` |
| FR-12 | MUST | done | `max_depth` 6, `max_nodes` 500, truncation reasons reported: `test_depth_limit_reported`, `test_node_limit_reported` |
| FR-13 | MUST | done | Cycles reported as C05; traces terminate: `test_cycle_terminates`, property tests |
| FR-14 | SHOULD | done | `likely_outputs` (CLI `outputs`, MCP `open_workbook`) |

## 5.3 Check catalog

All 14 checks implemented with positive and negative tests (`test_checks.py`,
`test_checks_week2.py`) and a documented false-positive policy (`docs/checks.md`).

| Check | Status | Benchmark (recall / precision) |
|-------|--------|---------------------|
| C01-C07 | done | C01 100/100, C02 94.7/100, C04 100/100, C05 100/100, C06 95.7/100, C07 100/100 |
| C03, C08-C14 | done | not seeded; 0 issues on the 24 clean models |

## 5.4 Driver analysis

| ID | Level | Status | Evidence |
|----|-------|--------|----------|
| FR-20 | MUST | done | Inputs = constants with no precedents, incl. constant names: `test_drivers_name_inputs_and_non_numeric` |
| FR-21 | MUST | done | +/- delta (default 10%) on a copy; ranked by absolute change: `test_drivers_rank_dcf_inputs`, `test_override_does_not_touch_model` |
| FR-22 | MUST | done | Unsupported/dynamic functions, cycles, 3D refs: partial, no numbers: `test_drivers_partial_returns_no_numbers` |
| FR-23 | MUST | done | Zero inputs use an absolute delta and are labelled: `test_drivers_zero_input_uses_absolute_delta` |
| FR-24 | SHOULD | done | Cap 200 inputs, reported: `test_drivers_cap_reported` |

## 5.5 Fix suggestions

| ID | Level | Status | Evidence |
|----|-------|--------|----------|
| FR-30 | MUST | done | C02 and C06 suggest the neighbour's formula for the target position |
| FR-31 | SHOULD | done | C01 suggests a named input and the rewritten formula |
| FR-32 | MUST | done | Text only; nothing writes a workbook: `test_s1_*` |

## 5.6 Reports

| ID | Level | Status | Evidence |
|----|-------|--------|----------|
| FR-40 | MUST | done | `schema_version`, stable order and IDs; golden files for 10 workbooks: `tests/golden/` |
| FR-41 | MUST | done | HTML: counts, severity groups, cells, evidence, fixes, Untrusted content section |
| FR-42 | MUST | done | Autoescape + CSP: `test_html_escapes_workbook_text` |
| FR-43 | SHOULD | done | Exit codes 0/1/2/3: `test_cli_exit_codes` |

## 6 MCP server

| ID | Level | Status | Evidence |
|----|-------|--------|----------|
| 8 tools | MUST | done | `test_all_tools_end_to_end_in_process`, `test_real_stdio_session` |
| 6.1 envelope | MUST | done | Every workbook string is `untrusted_text`, labelled with scanner risk |
| FR-50 | MUST | done | Short static descriptions, read-only annotations |
| FR-51 | MUST | done | Pagination and a 20 KB cap: `test_every_response_respects_size_cap` |
| FR-52 | MUST | done | LRU with max count and idle timeout: `test_store_lru_and_idle_timeout` |
| FR-53 | MUST | done | `{"code", "message"}` errors, no traces: `test_structured_errors` |
| FR-54 | SHOULD | done | `examples/claude_desktop_config*.json`, `examples/recommended_system_prompt.md` |

## 7 Security

| ID | Level | Status | Evidence |
|----|-------|--------|----------|
| 7.2 vectors | MUST | done | All 12 vectors (incl. chart/shape text, SHOULD) scanned: `test_every_vector_in_traps_workbook` |
| S-1 to S-10, S-13 | MUST | done | Mapped to code and tests in [security.md](security.md) |
| S-11 | SHOULD | partial | Dockerfile and hardened run documented; behaviour verified in an equivalent sandbox (uid 65534, no caps, no network, read-only data). The image build itself runs in CI and has not run yet (pending first push) |
| S-12 | SHOULD | done | `requirements/runtime.lock` with hashes; `pip-audit` CI job |

## 8 Non-functional

| ID | Level | Status | Evidence |
|----|-------|--------|----------|
| NF-1 | MUST | done | 10k formulas 1.0 s, 200k formulas 35 s ([architecture.md](architecture.md)) |
| NF-2 | MUST | done | 649 MB peak for 200k formulas |
| NF-3 | MUST | done | Byte-identical JSON: `test_cli_json_is_deterministic`, golden files |
| NF-4 | MUST | partial | Linux verified on 3.11/3.12/3.13; macOS and Windows run in the CI matrix (pending first push) |
| NF-5 | MUST | done | 94% coverage on the package; `mypy --strict` clean |
| NF-6 | SHOULD | partial | One command from GitHub now: `pip install "sheetguard[mcp,eval] @ git+https://github.com/dachib04/sheetguard"` (or `uvx --from ...`); plain `pip install "sheetguard[mcp,eval]"` once the release workflow publishes to PyPI |
| NF-7 | MUST | done | README, architecture, checks, security, benchmark, MCP docs, ADR |

## 9 Edge cases

Every listed edge case has at least one test. Highlights: empty workbook and sparse huge
used range (`test_loader.py`); quoted/unicode/ref-like sheet names; very hidden sheets, all
hidden but one, 300+ sheets; shared and dynamic-array formulas; renamed tables; no cached
values and manual calculation (C13); password-protected and protected sheets; truncated and
renamed files; whole-column refs in SUM/VLOOKUP/INDEX-MATCH; 3D refs and 29-level cross-sheet
chains; names to ranges/constants/formulas/names with shadowing; INDIRECT/OFFSET partial;
self and mutual cycles and iterative calc; IFERROR/IFNA; 8,000+ character and 120-level
nested formulas; unsupported functions; numbers as text with separators and currency; 1900
vs 1904 dates and the 1900 leap-year bug; percentages as whole numbers; negative zero, huge
floats, NaN-like text; booleans and `""` in ranges; split instructions, other languages,
zero-width characters, homoglyphs, benign look-alikes, 10,000 comments, 1 MB strings,
`javascript:`/`data:`/`file:` links, zip bombs, entity expansion, `../../etc/passwd`, and a
symlink escaping the root.

## 10 Testing and benchmark

| Level | Status | Evidence |
|-------|--------|----------|
| Unit | done | `tests/unit/` |
| Property-based | done | `tests/property/` (hypothesis): no crashes, traces terminate, cycles match a reference |
| Golden files | done | 10 reference workbooks |
| Fuzz / malicious | done | `tests/malicious/` |
| MCP integration | done | `tests/mcp/` incl. a real stdio session |
| Benchmark | done | 24 clean, 345 seeded, 40 traps, 54 benign (+20 held-out paraphrases); one command |
| Agent experiment | SHOULD, not run | Harness in `bench/agent_experiment.py`; needs an API key. No numbers claimed |

## 11 Deliverables

| Deliverable | Status |
|-------------|--------|
| Public GitHub repository, MIT, CI green, no secrets/client data | pending: repository ready locally, first push and CI run outstanding |
| Core library + CLI | done |
| MCP server | done; integration tests pass over stdio. A live Claude Desktop session is a manual check (pending) |
| HTML and JSON reports | done; schema in `docs/schema/report.schema.json`, samples in `examples/sample_reports/` |
| Benchmark | done |
| Documentation | done; README demo GIF is a terminal recording of the CLI (a Claude Desktop GIF can be recorded once installed) |
| Write-up | done (draft): [writeup.md](writeup.md) |
| Release | tag `v0.1.0` created; PyPI package and GHCR image are published by the release workflow on the first tag push (pending) |
