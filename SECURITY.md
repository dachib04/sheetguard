# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, not in public issues:

- Email: **security@[your-domain]** *(placeholder, set before public launch)*
- Or use GitHub's private vulnerability reporting on this repository.

Include a minimal workbook or script that reproduces the problem, the SheetGuard version,
and your OS and Python version. Expect an acknowledgement within 5 working days. Please
give us 90 days to fix before public disclosure; we will credit you unless you prefer not.

## Threat model

**Asset:** the integrity of the agent's conclusions and actions, and the user's machine
and files.
**Attacker:** anyone who can get a workbook in front of the user (email attachment, shared
drive, client deliverable).
**Attacker goals:** make the agent report false conclusions, take unwanted actions with
other tools, leak data, or crash / exhaust the host.

## Guarantees

| Requirement | How it is enforced |
|-------------|-------------------|
| Read-only (S-1) | The file is opened once in `rb` mode and read into memory; no code path writes a workbook or any file |
| No active content (S-2) | VBA, XLM macro sheets, DDE, OLE, ActiveX and data connections are detected from the archive and reported; openpyxl is called with `keep_vba=False, keep_links=False` |
| Hardened XML (S-3) | Every XML part is scanned for `<!DOCTYPE` / `<!ENTITY` and rejected before any parser runs; defusedxml is also installed |
| Zip bombs (S-4) | Member count, per-member and total *real* decompressed size, and compression ratio are enforced while streaming; declared sizes are not trusted |
| Resource limits (S-5) | File size, cell count (counted before loading), formula length, sheet count, recursion depth, cooperative per-call timeout |
| Path safety (S-6) | Paths resolved with `realpath`; must stay inside the allowed root; symlinks out of the root are rejected |
| No network (S-7) | No code path performs network I/O; external links and hyperlinks are recorded, never fetched |
| Metadata-only logs (S-8) | Logs carry file hash, sizes, timings and counts, never cell contents, unless `--debug` |
| External references (S-9) | Recorded as issues with their target string; never opened |
| No stray files (S-10) | Nothing is written to disk at all |
| Hardened container (S-11) | Image runs as uid 10001; documented `docker run` uses `--read-only --cap-drop ALL --network none --security-opt no-new-privileges` and a read-only data mount |
| Pinned dependencies (S-12) | `requirements/runtime.lock` with hashes (`pip install --require-hashes`); `pip-audit` in CI |

Every requirement is mapped to its enforcing code and its test in
[docs/security.md](docs/security.md).

## Limits of the prompt-injection scanner

The injection scanner is a **layered heuristic, not a guarantee**.
It looks for instruction-like phrasing, role markers, concealment (white-on-white text, tiny
fonts, `;;;` formats, hidden sheets/rows/columns), and character anomalies (zero-width
characters, homoglyphs). A determined attacker can write text it does not flag, for example
paraphrased instructions in an unusual language, or meaning that only emerges when cells are
read together.

That is why the scanner is not the main defence. The main defence is the **untrusted
envelope**: every string from the workbook is returned as

```json
{"kind": "untrusted_text", "source": "Sheet2!C14 (cell text)", "text": "...", "flags": [], "risk": "none"}
```

whether or not the scanner flagged it, so a client agent can always separate file data from
instructions. Agents should be configured to
treat `untrusted_text` as data only (see
[examples/recommended_system_prompt.md](examples/recommended_system_prompt.md)). The scanner flags and labels; it never deletes or
rewrites content, because silent removal would hide evidence from the user.

## Out of scope

Legacy `.xls`, `.xlsb`, `.ods`; executing or emulating macros; evaluating every Excel
function (unsupported functions are reported, never guessed).
