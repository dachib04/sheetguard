"""Run the SheetGuard benchmark and write the results table (spec §10.2).

    python bench/run_bench.py               # uses the committed corpus
    python bench/run_bench.py --rebuild     # rebuild the corpus first (needs LibreOffice)

Outputs bench/results/results.json and bench/results/results.md.

Scoring
-------
Seeded errors: a seed is *detected* when an issue from the seeded check names a
seeded cell (as its anchor or in its evidence). False positives are issues that
are not in the clean parent's report and do not touch a seeded cell; every
issue on a clean model is a false positive. Another check flagging the seeded
cell is recorded as a co-detection, not as a false positive. A finding on a
direct input of the seeded cell that the clean parent did not have (e.g. an
input left unused after its formula was overwritten) is recorded as a
consequence of the seeded error, not as a false positive.

Injection: a trap or paraphrase file is detected when the scanner reports a
finding of risk medium or higher; a benign file is a false positive when it
does. Lower and higher thresholds are reported too.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from sheetguard import __version__
from sheetguard.api import analyze
from sheetguard.checks.base import precedent_cells
from sheetguard.core.refs import format_cell, parse_cell_address
from sheetguard.report.schema import Issue

HERE = Path(__file__).parent
CORPUS = HERE / "corpus"
RESULTS = HERE / "results"
SEEDED_CHECKS = ["C01", "C02", "C04", "C05", "C06", "C07"]
RISK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def issue_cells(issue: Issue) -> set[str]:
    cells = {issue.cell.split(" (")[0]}
    cells |= {e.address for e in issue.evidence}
    return cells


def timed(path: Path) -> tuple[Any, list[Issue], float]:
    start = time.perf_counter()
    a = analyze(path)
    issues = a.audit()
    _ = a.untrusted
    return a, issues, time.perf_counter() - start


def pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def run_models() -> dict[str, Any]:
    timings: list[float] = []
    fp: Counter[str] = Counter()
    fp_examples: dict[str, list[str]] = defaultdict(list)
    clean_ids: dict[str, set[str]] = {}
    clean_issue_count: Counter[str] = Counter()
    for path in sorted((CORPUS / "clean").glob("*.xlsx")):
        _, issues, t = timed(path)
        timings.append(t)
        clean_ids[path.name] = {i.id for i in issues}
        for i in issues:
            fp[i.check_id] += 1
            clean_issue_count[i.check_id] += 1
            if len(fp_examples[i.check_id]) < 5:
                fp_examples[i.check_id].append(f"clean/{path.name} {i.cell}: {i.description}")

    seeds = json.loads((CORPUS / "seeded" / "manifest.json").read_text(encoding="utf-8"))
    consequences: Counter[str] = Counter()
    parents = {p.name: analyze(p) for p in sorted((CORPUS / "clean").glob("*.xlsx"))}
    tp: Counter[str] = Counter()
    total: Counter[str] = Counter()
    high_conf: Counter[str] = Counter()
    co: dict[str, Counter[str]] = defaultdict(Counter)
    missed: dict[str, list[str]] = defaultdict(list)
    for seed in seeds:
        path = CORPUS / "seeded" / seed["file"]
        _, issues, t = timed(path)
        timings.append(t)
        check = seed["check"]
        total[check] += 1
        targets = set(seed["cells"])
        hit = [i for i in issues if i.check_id == check and issue_cells(i) & targets]
        if hit:
            tp[check] += 1
            if any(i.confidence.value != "low" for i in hit):
                high_conf[check] += 1
        elif len(missed[check]) < 5:
            missed[check].append(f"{seed['file']} {seed['cell']} ({seed['mutation']})")
        inherited = clean_ids.get(seed["parent"], set())
        parent = parents[seed["parent"]]
        inputs: set[str] = set()
        for addr in targets:
            key = parse_cell_address(addr)
            inputs |= {format_cell(p) for p in precedent_cells(parent.graph, key)}
        for i in issues:
            touches = bool(issue_cells(i) & targets)
            if i.check_id != check and touches:
                co[check][i.check_id] += 1
            elif not touches and i.id not in inherited and issue_cells(i) & inputs:
                consequences[i.check_id] += 1
            elif not touches and i.id not in inherited:
                fp[i.check_id] += 1
                if len(fp_examples[i.check_id]) < 5:
                    fp_examples[i.check_id].append(
                        f"seeded/{seed['file']} {i.cell}: {i.description}"
                    )

    per_check = {}
    for check in sorted(set(SEEDED_CHECKS) | set(fp)):
        n_tp, n_fp, n = tp[check], fp[check], total[check]
        precision = pct(n_tp, n_tp + n_fp) if check in SEEDED_CHECKS else None
        recall = pct(n_tp, n)
        f1 = (
            round(2 * precision * recall / (precision + recall), 1)
            if precision and recall
            else None
        )
        per_check[check] = {
            "seeded": n,
            "detected": n_tp,
            "false_positives": n_fp,
            "false_positives_on_clean": clean_issue_count[check],
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "recall_excluding_low_confidence": pct(high_conf[check], n),
            "co_detections": dict(sorted(co[check].items())),
            "missed_examples": missed[check],
            "false_positive_examples": fp_examples[check],
        }
    return {
        "per_check": per_check,
        "timings": timings,
        "consequences": dict(sorted(consequences.items())),
        "clean_models": len(clean_ids),
        "seeded_variants": len(seeds),
        "clean_models_with_issues": sum(1 for v in clean_ids.values() if v),
    }


def run_injection() -> dict[str, Any]:
    plants = json.loads((CORPUS / "injection" / "manifest.json").read_text(encoding="utf-8"))
    rows: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    timings: list[float] = []
    misses: dict[str, list[str]] = defaultdict(list)
    benign_hits: list[str] = []
    for plant in plants:
        path = CORPUS / "injection" / plant["file"]
        start = time.perf_counter()
        findings = analyze(path).untrusted
        timings.append(time.perf_counter() - start)
        top = max((RISK[f.risk] for f in findings), default=0)
        kind, vector = plant["kind"], plant["vector"]
        c = rows[kind][vector]
        c["files"] += 1
        for name, level in (("low", 1), ("medium", 2), ("high", 3)):
            if top >= level:
                c[f"at_{name}"] += 1
        if kind != "benign" and top < 2 and len(misses[kind]) < 12:
            misses[kind].append(f"{vector}: {plant['text']}")
        if kind == "benign" and top >= 2:
            benign_hits.append(f"{vector}: {plant['text']}")

    clean_findings = Counter()
    for path in sorted((CORPUS / "clean").glob("*.xlsx")):
        for f in analyze(path).untrusted:
            clean_findings[f.risk] += 1

    def summary(kind: str) -> dict[str, Any]:
        files = sum(c["files"] for c in rows[kind].values())
        return {
            "files": files,
            **{
                f"rate_at_{lvl}": pct(sum(c[f"at_{lvl}"] for c in rows[kind].values()), files)
                for lvl in ("low", "medium", "high")
            },
            "per_vector": {
                v: {
                    "files": c["files"],
                    "detected_at_medium": c["at_medium"],
                    "rate_at_medium": pct(c["at_medium"], c["files"]),
                }
                for v, c in sorted(rows[kind].items())
            },
        }

    return {
        "trap": summary("trap"),
        "paraphrase": summary("paraphrase"),
        "benign": summary("benign"),
        "misses": dict(misses),
        "benign_false_positives": benign_hits,
        "clean_model_findings": dict(clean_findings),
        "timings": timings,
    }


def source_digest() -> str:
    """SHA-256 over the package source: identifies the code that produced the results,
    and (unlike a commit hash) survives rebases and squashes."""
    import hashlib

    h = hashlib.sha256()
    src = HERE.parent / "src" / "sheetguard"
    for path in sorted(src.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".j2"}:
            h.update(path.relative_to(src).as_posix().encode())
            h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def env() -> dict[str, str]:
    return {
        "sheetguard": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "source": source_digest(),
    }


def fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}%"


def cell(x: dict[str, Any] | None) -> str:
    return "-" if not x else f"{x['detected_at_medium']}/{x['files']}"


def to_markdown(r: dict[str, Any]) -> str:
    m, inj, e = r["models"], r["injection"], r["environment"]
    t = sorted(m["timings"])
    lines = [
        "# SheetGuard benchmark results",
        "",
        f"SheetGuard {e['sheetguard']} · Python {e['python']} · {e['platform']} · "
        f"source {e['source']}",
        "",
        f"Corpus: {m['clean_models']} clean models (6 families), {m['seeded_variants']} "
        f"seeded-error variants, {inj['trap']['files']} injection traps, "
        f"{inj['paraphrase']['files']} held-out paraphrased injections, "
        f"{inj['benign']['files']} benign look-alikes.",
        "",
        "## Model checks (seeded errors)",
        "",
        "| Check | Seeded | Detected | Recall | Precision | F1 | Recall excl. low confidence "
        "| False positives (clean) |",
        "|-------|-------:|---------:|-------:|----------:|---:|---:|---:|",
    ]
    for check in SEEDED_CHECKS:
        c = m["per_check"][check]
        lines.append(
            f"| {check} | {c['seeded']} | {c['detected']} | {fmt(c['recall'])} | "
            f"{fmt(c['precision'])} | {fmt(c['f1'])} | "
            f"{fmt(c['recall_excluding_low_confidence'])} | "
            f"{c['false_positives']} ({c['false_positives_on_clean']}) |"
        )
    others = {k: v for k, v in m["per_check"].items() if k not in SEEDED_CHECKS}
    lines += [
        "",
        f"Clean models with any issue: {m['clean_models_with_issues']} of {m['clean_models']}.",
        "",
    ]
    if others:
        lines += ["Other checks (no seeded errors; every issue counted as a false positive):", ""]
        lines += [f"- {k}: {v['false_positives']} issue(s)" for k, v in sorted(others.items())]
        lines.append("")
    if m.get("consequences"):
        lines += [
            "Consequences (a finding on a direct input of the seeded cell, e.g. an input "
            "left unused after its formula was overwritten): "
            + ", ".join(f"{k} x{v}" for k, v in m["consequences"].items()),
            "",
        ]
    co = {k: v["co_detections"] for k, v in m["per_check"].items() if v["co_detections"]}
    if co:
        lines += ["Co-detections (another check also flagged the seeded cell):", ""]
        lines += [f"- {k}: " + ", ".join(f"{c} x{n}" for c, n in v.items()) for k, v in co.items()]
        lines.append("")
    lines += [
        "## Prompt-injection scanner",
        "",
        "| Set | Files | Flagged ≥ low | Flagged ≥ medium | Flagged high |",
        "|-----|------:|-----:|------:|-----:|",
    ]
    for kind, label in (
        ("trap", "Traps (direct phrasing)"),
        ("paraphrase", "Held-out paraphrases"),
        ("benign", "Benign look-alikes (false positives)"),
    ):
        s = inj[kind]
        lines.append(
            f"| {label} | {s['files']} | {fmt(s['rate_at_low'])} | "
            f"{fmt(s['rate_at_medium'])} | {fmt(s['rate_at_high'])} |"
        )
    lines += [
        "",
        f"Findings on the {m['clean_models']} clean models: "
        f"{sum(inj['clean_model_findings'].values())}"
        f" {inj['clean_model_findings'] or ''}".rstrip(),
        "",
        "### Detection rate at ≥ medium, per vector",
        "",
        "| Vector | Traps | Paraphrases | Benign false positives |",
        "|--------|------:|------:|------:|",
    ]
    vectors = sorted(set(inj["trap"]["per_vector"]) | set(inj["paraphrase"]["per_vector"]))
    for v in vectors:
        tr = inj["trap"]["per_vector"].get(v)
        pa = inj["paraphrase"]["per_vector"].get(v)
        be = inj["benign"]["per_vector"].get(v)
        lines.append(f"| {v} | {cell(tr)} | {cell(pa)} | {cell(be)} |")
    lines += [
        "",
        "## Runtime",
        "",
        f"Models (load + 14 checks + scan): median {statistics.median(t) * 1000:.0f} ms, "
        f"p95 {t[int(len(t) * 0.95) - 1] * 1000:.0f} ms, max {t[-1] * 1000:.0f} ms over "
        f"{len(t)} files.",
        "",
    ]
    if inj["misses"]:
        lines += ["## Missed injections (as published, not tuned away)", ""]
        for kind, items in inj["misses"].items():
            lines += [f"**{kind}**", ""] + [f"- {x}" for x in items] + [""]
    if inj["benign_false_positives"]:
        lines += ["## Benign files flagged ≥ medium", ""]
        lines += [f"- {x}" for x in inj["benign_false_positives"]] + [""]
    missed = {k: v["missed_examples"] for k, v in m["per_check"].items() if v["missed_examples"]}
    if missed:
        lines += ["## Missed seeded errors (examples)", ""]
        for k, items in missed.items():
            lines += [f"**{k}**", ""] + [f"- {x}" for x in items] + [""]
    fps = {
        k: v["false_positive_examples"]
        for k, v in m["per_check"].items()
        if v["false_positive_examples"]
    }
    if fps:
        lines += ["## False positives (examples)", ""]
        for k, items in fps.items():
            lines += [f"**{k}**", ""] + [f"- {x}" for x in items] + [""]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rebuild", action="store_true", help="rebuild the corpus first")
    args = parser.parse_args()
    if args.rebuild:
        code = subprocess.run(
            [sys.executable, str(HERE / "generators" / "build_corpus.py")], check=False
        ).returncode
        if code:
            return code
    started = time.perf_counter()
    results = {"environment": env(), "models": run_models(), "injection": run_injection()}
    RESULTS.mkdir(exist_ok=True)
    for part in ("models", "injection"):
        timings = results[part].pop("timings")
        results[part]["runtime_s"] = {
            "median": round(statistics.median(timings), 4),
            "max": round(max(timings), 4),
            "files": len(timings),
        }
        if part == "models":
            results[part]["timings"] = timings
    md = to_markdown(results)
    results["models"].pop("timings")
    (RESULTS / "results.json").write_text(
        json.dumps(results, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (RESULTS / "results.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"(benchmark took {time.perf_counter() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
