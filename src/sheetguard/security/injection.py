"""Prompt-injection scanner (§7.2, MUST).

A *layered heuristic*, not a guarantee (see SECURITY.md). For every
text-bearing location in the workbook it adds up signals:

* instruction-like phrasing (imperatives aimed at an AI or reviewer, role
  markers, "ignore previous instructions" in several languages, requests to
  hide findings or exfiltrate data);
* concealment (font colour equal to fill, white on white, tiny font, ``;;;``
  number format);
* location risk (hidden and very hidden sheets, hidden rows and columns);
* character anomalies (zero-width and bidi control characters, homoglyphs,
  very long strings).

Text is normalised (NFKC, control characters stripped, look-alike Cyrillic and
Greek letters mapped to Latin) before matching, so obfuscation does not hide an
instruction; the obfuscation itself is a signal.

Design rule: the scanner flags and labels; it never deletes or rewrites
content. And the untrusted envelope protects even when this scanner misses.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import unquote

from sheetguard.core.model import Cell, Sheet, ValueType, Workbook
from sheetguard.core.refs import CellKey, analyze_formula, format_cell, quote_sheet
from sheetguard.security.envelope import Risk, UntrustedText, wrap

MAX_SCAN_CHARS = 2_000_000  # per text: longer texts are scanned head + tail
SCAN_BUDGET_CHARS = 100_000_000  # per workbook: after this, only heads/tails of 4k chars
SPLIT_MAX_CELLS = 12

# --------------------------------------------------------------- normalisation

_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\ufeff\u00ad]")
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f]")
_CONFUSABLES = str.maketrans(
    {
        # Cyrillic
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "һ": "h",
        "ԁ": "d",
        "ԛ": "q",
        "ԝ": "w",
        "ӏ": "l",
        "ɡ": "g",
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Х": "X",
        "І": "I",
        "Ј": "J",
        "Ѕ": "S",
        # Greek
        "α": "a",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Υ": "Y",
        "Χ": "X",
    }
)
_LATIN = re.compile(r"[A-Za-z]")
_NON_LATIN_LOOKALIKE = re.compile("[Ͱ-ϿЀ-ӿɐ-ʯ]")
_KEYWORDS = frozenset(
    {
        "ignore",
        "instructions",
        "instruction",
        "previous",
        "system",
        "assistant",
        "report",
        "errors",
        "disregard",
        "prompt",
        "model",
    }
)


@dataclass(frozen=True, slots=True)
class Normalised:
    text: str  # what patterns are matched against
    flags: tuple[str, ...]


def normalise(raw: str, limit: int = MAX_SCAN_CHARS) -> Normalised:
    flags: list[str] = []
    if len(raw) > limit:
        # Head and tail: padding cannot push an instruction out of view.
        half = limit // 2
        text = raw[:half] + " \u2026 " + raw[-half:]
    else:
        text = raw
    if _ZERO_WIDTH.search(text):
        flags.append("zero_width_chars")
        text = _ZERO_WIDTH.sub("", text)
    if _BIDI.search(text):
        flags.append("bidi_control_chars")
        text = _BIDI.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    homoglyph = False
    words = []
    for word in re.split(r"(\s+)", text):
        mapped = word.translate(_CONFUSABLES)
        if mapped != word:
            mixed = bool(_LATIN.search(word)) and bool(_NON_LATIN_LOOKALIKE.search(word))
            disguised = re.sub(r"[^a-z]", "", mapped.lower()) in _KEYWORDS
            if mixed or disguised:
                homoglyph = True
                word = mapped
        words.append(word)
    if homoglyph:
        flags.append("homoglyphs")
        text = "".join(words)
    if len(raw) > 5_000:
        flags.append("very_long")
    if len(raw) > limit:
        flags.append("scan_truncated")
    return Normalised(re.sub(r"\s+", " ", text).strip().lower(), tuple(flags))


# ------------------------------------------------------------------ patterns

_AI = (
    r"(?:ai|a\.i\.|assistant|chatbot|llm|gpt|chatgpt|claude|copilot|gemini|bard|"
    r"language model|agent|reviewer|auditor|analyst bot)"
)
_OBJ = r"(?:instructions?|prompts?|rules|directions|guidelines|context|messages?|commands?)"

PATTERNS: tuple[tuple[str, int, re.Pattern[str]], ...] = tuple(
    (name, weight, re.compile(rx))
    for name, weight, rx in (
        (
            "ignore_previous",
            6,
            r"\b(?:ignore|disregard|forget|override|bypass|skip)\b.{0,40}\b(?:previous|prior|"
            r"above|earlier|all|any|preceding|system|original|your)\b.{0,30}\b" + _OBJ + r"\b",
        ),
        (
            "new_instructions",
            4,
            r"\b(?:new|updated|real|actual|hidden|secret)\s+(?:system\s+)?"
            r"instructions?\b",
        ),
        (
            "identity_override",
            6,
            r"\byou\s+are\s+(?:now\s+)?(?:an?\s+|the\s+)?(?:ai|assistant|"
            r"chatbot|language model|llm|gpt|claude|dan)\b",
        ),
        (
            "false_conclusion",
            4,
            r"\b(?:report|state|say|tell|conclude|confirm|respond|answer|reply|output|write)\b"
            r".{0,40}\b(?:no (?:errors?|issues?|problems?|findings?|mistakes?)|(?:is|are) "
            r"(?:correct|accurate|error[- ]free|fine|clean|valid|approved))",
        ),
        (
            "suppress_findings",
            4,
            r"\b(?:do not|don't|never|avoid)\b.{0,25}\b(?:mention|report|flag|disclose|reveal|"
            r"highlight)\b.{0,30}\b(?:this|these|that|errors?|issues?|problems?|anything|it|"
            r"findings?|cells?|rows?|sheet)\b",
        ),
        (
            "exfiltration",
            4,
            r"\b(?:send|email|e-mail|forward|upload|post|exfiltrate|leak)\b"
            r".{0,30}\b(?:model|workbook|file|spreadsheet|data|contents|values|"
            r"numbers|figures|everything|sheet|it|this)\b"
            r".{0,40}(?:https?://|www\.|\S+@\S+\.\w+)",
        ),
        (
            "ai_obligation",
            3,
            r"\b" + _AI + r"s?\b.{0,60}\b(?:must|should|needs? to|have to|"
            r"shall|is required to)\b",
        ),
        ("addressed_to_ai", 2, r"(?:^|[\s\"'(\[])(?:dear\s+)?" + _AI + r"s?\s*[:,]"),
        (
            "mode_switch",
            3,
            r"\byou are now (?:in |operating in )?(?:an? )?[\w-]+ mode\b|"
            r"\b(?:enter|switch to|activate|enable)\b.{0,20}\b(?:approval|"
            r"developer|admin|god|unrestricted|debug) mode\b",
        ),
        (
            "role_marker",
            2,
            r"(?:^|\n)\s*(?:system|assistant|developer|user|human)\s*:|"
            r"<\|?(?:im_start|im_end|system|endoftext)\|?>|\[/?(?:inst|sys)\]",
        ),
        ("you_must", 2, r"\byou (?:must|should|will|are required to|need to)\b"),
        ("prompt_terms", 2, r"\b(?:system prompt|prompt injection|jailbreak|developer mode)\b"),
        (
            "tool_use",
            2,
            r"\b(?:execute|run|call|invoke|use)\b.{0,20}\b(?:tool|function call|"
            r"shell|command|script)\b",
        ),
        # other languages: "ignore (all) previous instructions"
        ("ignore_previous_de", 6, r"\bignorier\w*\b.{0,30}\b(?:anweisungen|befehle|regeln)\b"),
        (
            "ignore_previous_fr",
            6,
            r"\bignore[zr]\b.{0,30}\b(?:instructions|consignes|règles)\b|"
            r"\bignore\b.{0,10}\b(?:les|toutes|tous)\b.{0,30}"
            r"\b(?:instructions|consignes|règles)\b",
        ),
        ("ignore_previous_es", 6, r"\bignora\w*\b.{0,30}\b(?:instrucciones|reglas|órdenes)\b"),
        ("ignore_previous_ru", 6, r"\bигнорир\w*\b.{0,30}\b(?:инструкци\w*|указани\w*|правил\w*)"),
        ("ignore_previous_zh", 6, r"忽略.{0,10}(?:指令|指示|说明|规则)"),
        ("ignore_previous_ka", 6, r"უგულებელყ\w*.{0,30}(?:ინსტრუქცი\w*|მითითებ\w*)"),
        ("weak_imperative", 1, r"\b(?:ignore|disregard|overlook)\b"),
        ("mentions_instructions", 1, r"\binstructions?\b"),
    )
)


def strong_signals(text: str) -> set[str]:
    """Names of the patterns with weight >= 4 that match ``text``."""
    return {name for name, weight, rx in PATTERNS if weight >= 4 and rx.search(text)}


def phrase_signals(text: str) -> tuple[int, list[str]]:
    """(score, signal names) for one normalised text."""
    hits = [(name, weight) for name, weight, rx in PATTERNS if rx.search(text)]
    names = [n for n, _ in hits]
    # weak signals add nothing when a strong one already explains them
    score = sum(w for _, w in hits)
    if any(w >= 4 for _, w in hits):
        score -= sum(w for _, w in hits if w == 1)
    return score, names


# ----------------------------------------------------------------- findings


@dataclass(slots=True)
class Finding:
    vector: str
    location: str
    text: str
    cell: CellKey | None = None
    signals: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    instruction_score: int = 0
    context_score: int = 0
    risk: Risk = "none"

    @property
    def score(self) -> int:
        return self.instruction_score + self.context_score

    @property
    def id(self) -> str:
        digest = hashlib.sha1(
            f"{self.vector}|{self.location}|{self.text[:500]}".encode(), usedforsecurity=False
        ).hexdigest()
        return f"U-{digest[:10]}"

    def envelope(self, max_len: int = 500) -> UntrustedText:
        return wrap(
            self.text,
            f"{self.location} ({self.vector.replace('_', ' ')})",
            flags=tuple(self.signals),
            risk=self.risk,
            max_len=max_len,
        )


_CONTEXT_WEIGHTS = {
    "concealed_formatting": 3,
    "very_hidden_sheet": 2,
    "hidden_sheet": 1,
    "hidden_row": 1,
    "hidden_column": 1,
    "zero_width_chars": 2,
    "bidi_control_chars": 2,
    "homoglyphs": 2,
    "very_long": 1,
    "unusual_length": 2,
    "local_file_url": 2,
}
_HIDDEN_LOCATIONS = frozenset({"hidden_sheet", "very_hidden_sheet", "hidden_row", "hidden_column"})
_REASONS = {
    "concealed_formatting": "formatting makes the text invisible to a human",
    "hidden_sheet": "in a hidden sheet",
    "very_hidden_sheet": "in a very hidden sheet",
    "hidden_row": "in a hidden row",
    "hidden_column": "in a hidden column",
    "zero_width_chars": "contains zero-width characters",
    "bidi_control_chars": "contains bidirectional control characters",
    "homoglyphs": "uses look-alike letters from other alphabets",
    "very_long": "unusually long text",
    "unusual_length": "unusually long name",
    "dangerous_url_scheme": "link uses a javascript:, vbscript: or data: URL",
    "local_file_url": "link points to a local or network file",
    "split_across_cells": "instruction only appears when neighbouring cells are read together",
}


def assess(f: Finding, extra_flags: Iterable[str] = (), limit: int = MAX_SCAN_CHARS) -> Finding:
    norm = normalise(f.text, limit)
    instr, names = phrase_signals(norm.text)
    flags = list(dict.fromkeys([*extra_flags, *norm.flags]))
    ctx = sum(_CONTEXT_WEIGHTS.get(fl, 0) for fl in flags)
    f.instruction_score, f.context_score = instr, ctx
    f.signals = [*(["instruction_like"] if instr >= 2 else []), *names, *flags]
    if instr >= 6 or (instr >= 4 and ctx >= 2):
        risk: Risk = "high"
    elif instr >= 4 or (instr >= 2 and ctx >= 2):
        risk = "medium"
    elif instr >= 1 or ctx >= 2 or any(fl in _HIDDEN_LOCATIONS for fl in flags):
        # Any text a human cannot see because of where it sits is at least "low": the
        # phrase patterns miss naturally worded instructions, the location does not.
        risk = "low"
    else:
        risk = "none"
    if "dangerous_url_scheme" in flags:
        risk = "high"
    elif "local_file_url" in flags and risk in ("none", "low"):
        risk = "medium"
    f.risk = risk
    shown = ", ".join(n for n in names if n != "weak_imperative")
    reasons = [f"instruction-like phrasing ({shown})" if instr >= 2 else ""]
    reasons += [_REASONS[fl] for fl in flags if fl in _REASONS]
    f.reasons = [r for r in reasons if r]
    return f


# ----------------------------------------------------------------- concealment


def _rgb(color: str | None) -> str | None:
    if color is None:
        return None
    if color.startswith("rgb:"):
        return color[4:][-6:].upper()
    if (
        color.startswith("theme:0")
        or color.startswith("indexed:9")
        or color.startswith("indexed:1:")
    ):
        return "FFFFFF"  # theme 0 = "Background 1" (white); indexed 9 = white
    if color.startswith("theme:1") or color.startswith("indexed:8"):
        return "000000"
    return None


def concealment(cell: Cell) -> list[str]:
    reasons: list[str] = []
    fmt = cell.number_format.replace(" ", "")
    if fmt and set(fmt.split(";")) == {""} and fmt.count(";") >= 2:
        reasons.append("number format ;;; hides the value")
    size = cell.style.font_size
    if size is not None and size <= 2:
        reasons.append(f"font size {size:g}")
    font, fill = _rgb(cell.style.font_color), _rgb(cell.style.fill_color)
    if font is not None and fill is not None and font == fill:
        reasons.append("font colour = fill colour")
    elif font == "FFFFFF" and fill is None:
        reasons.append("white text on default white background")
    return reasons


# ---------------------------------------------------------------------- scan


def _location_flags(sheet: Sheet, row: int, col: int) -> list[str]:
    flags: list[str] = []
    if sheet.state == "veryHidden":
        flags.append("very_hidden_sheet")
    elif sheet.state == "hidden":
        flags.append("hidden_sheet")
    if row in sheet.hidden_rows:
        flags.append("hidden_row")
    if col in sheet.hidden_cols:
        flags.append("hidden_column")
    return flags


_SCHEME = re.compile(r"^\s*([a-z][a-z0-9+.-]*):", re.IGNORECASE)


def scan(wb: Workbook) -> list[Finding]:
    """Every text-bearing location, assessed. Only findings with risk > none are kept."""
    out: list[Finding] = []
    budget = [SCAN_BUDGET_CHARS]

    def add(f: Finding, flags: Iterable[str] = ()) -> None:
        limit = MAX_SCAN_CHARS if budget[0] > 0 else 8_000
        budget[0] -= min(len(f.text), limit)
        assess(f, flags, limit)
        if f.risk != "none":
            out.append(f)

    for sheet in wb.sheets.values():
        sq = quote_sheet(sheet.name)
        if len(sheet.name) >= 2:
            add(Finding("sheet_name", f"sheet {sq}", sheet.name))
        for (r, c), cell in sorted(sheet.cells.items()):
            key = CellKey(sheet.name, r, c)
            addr = format_cell(key)
            loc = _location_flags(sheet, r, c)
            hidden_how = concealment(cell)
            cflags = [*loc, *(["concealed_formatting"] if hidden_how else [])]
            if cell.formula is None and cell.vtype is ValueType.TEXT and cell.value:
                f = Finding("cell_text", addr, str(cell.value), key)
                add(f, cflags)
                if hidden_how and f.risk != "none":
                    f.reasons.append("; ".join(hidden_how))
            elif cell.formula is not None:
                literals = analyze_formula(cell.formula).texts
                if literals:
                    add(Finding("formula_literal", addr, " ".join(literals), key), cflags)
                if cell.vtype is ValueType.TEXT and cell.value and str(cell.value) not in literals:
                    add(Finding("formula_result", addr, str(cell.value), key), cflags)
            if cell.comment:
                add(Finding("comment", addr, cell.comment, key), loc)
            if cell.hyperlink:
                link = unquote(cell.hyperlink)
                scheme = _SCHEME.match(link)
                lflags = list(loc)
                if scheme and scheme.group(1).lower() in ("javascript", "vbscript", "data"):
                    lflags.append("dangerous_url_scheme")
                elif (scheme and scheme.group(1).lower() == "file") or link.startswith("\\\\"):
                    lflags.append("local_file_url")
                add(Finding("hyperlink", addr, link, key), lflags)
        for dv in sheet.data_validations:
            for label, text in (
                ("prompt title", dv.prompt_title),
                ("prompt", dv.prompt),
                ("error title", dv.error_title),
                ("error", dv.error),
            ):
                if text:
                    add(Finding("data_validation", f"{sq}!{dv.sqref} ({label})", text))
        for where, text in sheet.headers_footers:
            add(Finding("header_footer", f"sheet {sq} {where}", text))
        out.extend(_split_text(sheet))

    for n in wb.names:
        scope = f"{n.scope_sheet}!" if n.scope_sheet else ""
        flags = ["unusual_length"] if len(n.name) > 64 else []
        add(Finding("defined_name", f"name {scope}{n.name}", f"{n.name} = {n.text}"), flags)
    for prop, text in wb.properties:
        add(Finding("document_property", f"property {prop}", text))
    for vector, location, text in wb.extra_texts:
        add(Finding(vector, location, text))
    for target in wb.active_content.external_link_targets:
        add(Finding("external_link", "external link target", target))

    risk_rank = {"high": 0, "medium": 1, "low": 2, "none": 3}
    order = {name: i for i, name in enumerate(wb.sheets)}
    out.sort(
        key=lambda f: (
            risk_rank[f.risk],
            order.get(f.cell.sheet, -1) if f.cell else -1,
            (f.cell.row, f.cell.col) if f.cell else (0, 0),
            f.vector,
            f.location,
        )
    )
    return _dedupe(out)


def _split_text(sheet: Sheet) -> list[Finding]:
    """Instructions split across neighbouring text cells (edge case in §9)."""
    found: list[Finding] = []
    for axis in ("row", "column"):
        lines: dict[int, list[tuple[int, str]]] = {}
        for (r, c), cell in sheet.cells.items():
            if cell.formula is None and cell.vtype is ValueType.TEXT and cell.value:
                lines.setdefault(r if axis == "row" else c, []).append(
                    (c if axis == "row" else r, str(cell.value))
                )
        for line, items in sorted(lines.items()):
            items.sort()
            run: list[tuple[int, str]] = []
            for pos, text in [*items, (-10, "")]:
                if run and pos == run[-1][0] + 1 and len(run) < SPLIT_MAX_CELLS:
                    run.append((pos, text))
                    continue
                if len(run) >= 2:
                    joined = " ".join(t for _, t in run)
                    whole = strong_signals(normalise(joined).text)
                    parts: set[str] = set()
                    for _, t in run:
                        parts |= strong_signals(normalise(t).text)
                    if whole - parts:
                        first = (
                            CellKey(sheet.name, line, run[0][0])
                            if axis == "row"
                            else CellKey(sheet.name, run[0][0], line)
                        )
                        last = (
                            CellKey(sheet.name, line, run[-1][0])
                            if axis == "row"
                            else CellKey(sheet.name, run[-1][0], line)
                        )
                        loc = f"{format_cell(first)}:{format_cell(last).split('!')[-1]}"
                        f = Finding("split_text", loc, joined, first)
                        flags = [
                            "split_across_cells",
                            *_location_flags(sheet, first.row, first.col),
                        ]
                        assess(f, flags)
                        if f.risk != "none":
                            found.append(f)
                run = [(pos, text)]
    return found


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Threaded comments also leave a legacy copy; keep one finding per (location, text)."""
    seen: set[tuple[str, str]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.location, f.text.strip())
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


RISK_ORDER: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3}


def cell_index(findings: Iterable[Finding]) -> dict[str, tuple[Risk, tuple[str, ...]]]:
    """Highest risk and union of signals per cell address, for annotating envelopes."""
    idx: dict[str, tuple[Risk, tuple[str, ...]]] = {}
    for f in findings:
        if f.cell is None:
            continue
        addr = format_cell(f.cell)
        prev = idx.get(addr)
        if prev is None:
            idx[addr] = (f.risk, tuple(f.signals))
        else:
            risk = f.risk if RISK_ORDER[f.risk] > RISK_ORDER[prev[0]] else prev[0]
            idx[addr] = (risk, tuple(dict.fromkeys([*prev[1], *f.signals])))
    return idx
