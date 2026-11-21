# Security model

The threat model, guarantees and disclosure policy are in [SECURITY.md](../SECURITY.md).
This page maps every security requirement of the spec to the code that enforces it and the
test that proves it.

## Requirement traceability (§7.3)

| ID | Requirement | Enforced in | Tested by |
|----|-------------|-------------|-----------|
| S-1 | Strictly read-only | `core/loader.py` reads bytes once in `rb`; no writer anywhere | `test_security_requirements.py::test_s1_*` (static AST scan + runtime `open` guard) |
| S-2 | Never execute active content; report it | `security/archive.py` detection; openpyxl `keep_vba=False, keep_links=False` | `test_loader.py::test_xlsm_macros_detected_not_executed`, `test_s2_*` |
| S-3 | Hardened XML | DTD/ENTITY rejection on every part before parsing + defusedxml | `test_malicious.py::test_xxe_*`, `test_billion_laughs_*`, `test_dtd_in_rels_*` |
| S-4 | Zip-bomb protection | streaming real-size checks: total, per member, count, ratio | `test_zip_bomb_*`, `test_member_size_limit`, `test_too_many_members`, `test_lying_zip_header_*` |
| S-5 | Resource limits + timeout | `security/limits.py`; cell count before load; MCP per-call timeout | `test_file_size_limit`, `test_cell_limit_*`, `test_formula_length_limit`, `test_sheet_count_limit`, `test_mcp_server.py::test_timeout_*` |
| S-6 | Path safety | `security/paths.py` (`realpath`, root containment) | `test_path_traversal_rejected`, `test_symlink_escape_*`, `test_mcp_server.py::test_symlink_escape_over_mcp` |
| S-7 | No network, no telemetry | no network code; links recorded only | `test_s7_no_network_access` (sockets disabled during full analysis), sandbox smoke test |
| S-8 | Metadata-only logs | log calls carry hash prefix, sizes, timings, counts | `test_s8_logs_hold_metadata_only` |
| S-9 | External refs recorded, never opened | graph flags + C08 | `test_external_links_recorded_never_opened`, C08 tests |
| S-10 | No writes outside an owned temp dir | nothing is written at all | `test_s1_s10_no_file_is_opened_for_writing_at_runtime` |
| S-11 | Hardened container | `Dockerfile` (non-root uid 10001, ro `/data`); documented `docker run` flags | CI `docker` job; CI and local `sandbox` smoke test (uid 65534, no caps, no network, ro data) |
| S-12 | Pinned deps + vuln scan | `requirements/runtime.lock` (`--require-hashes`), `pip-audit` CI job | CI `audit` and `docker` jobs |
| S-13 | SECURITY.md | [SECURITY.md](../SECURITY.md) | `test_s13_security_md_documents_contact_and_limits` |

## Prompt-injection vectors (§7.2)

| Vector | Where SheetGuard reads it | Detection signals |
|--------|---------------------------|-------------------|
| Visible cell text | cell constants | instruction phrasing |
| Concealed cell text | font colour = fill, white on white, font size ≤ 2, `;;;` format | concealment (+3) |
| Hidden sheets, rows, columns | sheet state, row/column dimensions | location (+1, very hidden +2) |
| Comments and notes | legacy notes, threaded comments (`xl/threadedComments`) | phrasing |
| Data validation messages | prompt/error titles and texts | phrasing |
| Defined names and sheet names | name, name formula, sheet title | phrasing, unusual length |
| Document properties | core and custom properties | phrasing |
| Headers and footers | odd/even/first, left/centre/right | phrasing |
| Formula string literals | literals, and cached text results (catches concatenation) | phrasing |
| Hyperlinks | URL / location | `javascript:` `vbscript:` `data:` (high), `file:` / UNC (medium), phrasing |
| Chart and shape text | `xl/charts`, `xl/drawings` | phrasing |
| Split across cells | runs of up to 12 adjacent text cells, rows and columns | a strong phrase that no single cell contains |
| External link targets | external link relationships | phrasing |

Normalisation before matching: zero-width and bidi control characters removed (flagged),
NFKC, Cyrillic/Greek look-alike letters mapped to Latin when a word mixes scripts or spells a
keyword (flagged as homoglyphs). Instruction patterns exist for English, German, French,
Spanish, Russian, Chinese and Georgian. Texts over 2 MB are scanned head and tail; a 100 MB
per-workbook budget keeps scanning inside the time limit.

**Risk.** Phrase weights (strong 4–6, medium 2–3, weak 1) plus context weights:
high = phrase ≥ 6, or ≥ 4 with context ≥ 2; medium = phrase ≥ 4, or ≥ 2 with context ≥ 2;
low = any weak phrase or concealment alone. Dangerous URL schemes are always high.

**What the scanner does not promise.** Paraphrased or novel instructions, languages without
patterns, meaning spread across distant cells, or text in images. The untrusted envelope is
the primary defence and applies whether or not anything is flagged.
