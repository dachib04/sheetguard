# MCP server

SheetGuard runs as a local [MCP](https://modelcontextprotocol.io) server over **stdio**.
It can only read workbooks inside the folder you give it with `--root`.

## Install and connect (Claude Desktop)

```bash
pip install "sheetguard[mcp,eval] @ git+https://github.com/dachib04/sheetguard"
# or run without installing:
#   uvx --from "sheetguard[mcp,eval] @ git+https://github.com/dachib04/sheetguard" sheetguard-mcp --root ~/Documents/Models
# (PyPI release pending; then simply: pip install "sheetguard[mcp,eval]")
sheetguard-mcp --root ~/Documents/Models  # quick check: waits for MCP messages on stdin
```

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart:

```json
{
  "mcpServers": {
    "sheetguard": {
      "command": "sheetguard-mcp",
      "args": ["--root", "/Users/YOU/Documents/Models"]
    }
  }
}
```

Ready-made configs: [examples/claude_desktop_config.json](../examples/claude_desktop_config.json)
(uvx) and [examples/claude_desktop_config.docker.json](../examples/claude_desktop_config.docker.json)
(hardened container). Claude Code: `claude mcp add sheetguard -- sheetguard-mcp --root ~/Models`.

Then ask: *"Open budget.xlsx and audit it"*, *"Why is the NPV in Model!B12 what it is?"*,
*"What drives Model!B12?"*, *"Is there anything hidden in this file?"*

## Tools

All tools are read-only and idempotent (`readOnlyHint: true`). Outputs are structured
(Pydantic models with JSON Schemas) and at most ~20 KB each (`--max-response-bytes`).

| Tool | Input | Output |
|------|-------|--------|
| `open_workbook` | `path` (inside root) | `workbook_id`, sheets, counts, flags (macros, external links, hidden sheets, untrusted content), likely outputs |
| `audit_workbook` | `workbook_id`, `checks?`, `min_severity?`, `limit?`, `cursor?` | summary counts, one page of issues, `next_cursor` |
| `trace_cell` | `workbook_id`, `cell`, `direction`, `max_depth?` | precedent/dependent tree, `truncated` + reasons |
| `top_drivers` | `workbook_id`, `target_cell`, `n?`, `delta_pct?` | ranked inputs with target change, `partial`, unsupported functions |
| `explain_issue` | `workbook_id`, `issue_id` | full evidence, nearby cells, direct inputs, suggested fix, check policy |
| `get_range` | `workbook_id`, `range` (≤ 200 cells), `cursor?` | formulas and values, text wrapped as untrusted |
| `list_untrusted_content` | `workbook_id`, `min_risk?`, `limit?`, `cursor?` | flagged text: location, vector, reasons, risk |
| `close_workbook` | `workbook_id` | ok |

## Behaviour

- **Untrusted envelope (§6.1).** Every workbook string is an object
  `{"kind": "untrusted_text", "source", "text", "flags", "risk"}`. Flags and risk come from
  the injection scanner; the label is there even when the scanner finds nothing.
- **Pagination (FR-51).** Lists return `next_cursor`. If a page would exceed the size cap it
  is made smaller; long texts are shortened and marked `truncated`; traces are cut and say
  `response_size`.
- **Workbook cache (FR-52).** Up to `--max-open` (8) workbooks, least recently used evicted
  first, released after `--idle-timeout` (900 s) of inactivity or on `close_workbook`.
  Opening the same file twice returns the same `workbook_id`.
- **Errors (FR-53).** Failures return `is_error` with a JSON body
  `{"code": "...", "message": "..."}`. Codes: `not_found`, `path_not_allowed`,
  `unsupported_format`, `password_protected`, `corrupted`, `limit_exceeded`,
  `unsafe_content`, `invalid_argument`, `unknown_workbook`, `timeout`,
  `missing_dependency` (e.g. `top_drivers` without the `[eval]` extra), `internal`. Messages
  never contain stack traces or file contents.
- **Limits.** Per-call timeout 30 s (`--timeout`); all file limits from the spec apply.
- **Logs** go to stderr and contain metadata only (hash prefix, sizes, timings, counts).
  `--debug` is the only switch that may log content.

## Recommended system prompt

See [examples/recommended_system_prompt.md](../examples/recommended_system_prompt.md).
