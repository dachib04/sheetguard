"""Render docs/demo.gif: a ~60 s terminal demo built from REAL command output.

    python scripts/make_demo_gif.py

Each scene runs the actual `sheetguard` CLI against the repository fixtures and
draws the command being typed, then its real output. Nothing is mocked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parents[1]
OUT = ROOT / "docs" / "demo.gif"
W, H, PAD, LINE = 1100, 640, 22, 19
BG, FG, DIM, PROMPT, ACCENT = (
    (24, 26, 33),
    (220, 223, 228),
    (130, 136, 150),
    (110, 200, 140),
    (240, 180, 90),
)
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "C:/Windows/Fonts/consola.ttf",
]

SCENES: list[tuple[str, list[str], str]] = [
    (
        "Audit a model: exact cells, root causes, suggested fixes",
        ["audit", "tests/fixtures/reference/errors.xlsx"],
        "sheetguard audit errors.xlsx",
    ),
    (
        "What drives the valuation? (recalculated, +/-10% per input)",
        ["drivers", "tests/fixtures/reference/dcf.xlsx", "Model!B12", "-n", "4"],
        "sheetguard drivers dcf.xlsx Model!B12 -n 4",
    ),
    (
        "A workbook that tries to manipulate an AI reviewer",
        ["audit", "tests/fixtures/injection/traps.xlsx", "--json"],
        "sheetguard audit traps.xlsx --json    # untrusted_content, abridged",
    ),
]


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    raise SystemExit("no monospace font found")


def run(args: list[str]) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "sheetguard.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    if args[-1] == "--json":
        import json

        data = json.loads(out)
        lines = [
            f"workbook flags: untrusted_content_found = "
            f"{data['workbook']['flags']['untrusted_content_found']}",
            f"untrusted items: {data['untrusted_summary']['by_risk']}",
            "",
        ]
        for f in data["untrusted_content"][:6]:
            lines.append(f"[{f['risk']:4}] {f['location']:<34} {f['vector']}")
            lines.append(f'       {{"kind": "untrusted_text", "text": "{f["text"]["text"][:58]}"}}')
        return lines
    return out.splitlines()


def wrap(line: str, width: int = 104) -> list[str]:
    return [line[i : i + width] for i in range(0, max(len(line), 1), width)] or [""]


def frame(
    title: str,
    typed: str,
    output: list[str],
    f: ImageFont.FreeTypeFont,
    fb: ImageFont.FreeTypeFont,
    cursor: bool,
) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        d.ellipse((PAD + i * 20, 12, PAD + i * 20 + 12, 24), fill=c)
    d.text(
        (W // 2, 18),
        "SheetGuard - read-only Excel auditing for AI agents",
        fill=DIM,
        font=f,
        anchor="mm",
    )
    d.text((PAD, 44), f"# {title}", fill=ACCENT, font=fb)
    y = 74
    d.text((PAD, y), "$ ", fill=PROMPT, font=fb)
    d.text((PAD + 22, y), typed + ("\u2588" if cursor else ""), fill=FG, font=fb)
    y += LINE + 8
    for line in output:
        for part in wrap(line):
            if y > H - PAD - LINE:
                return img
            color = FG
            if "[critical" in part or "[high" in part:
                color = (240, 110, 100)
            elif "[warning" in part or "[medi" in part:
                color = ACCENT
            elif "suggested:" in part or "untrusted_text" in part:
                color = (140, 190, 250)
            d.text((PAD, y), part, fill=color, font=f)
            y += LINE
    return img


def main() -> int:
    f, fb = font(15), font(16)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for title, args, shown in SCENES:
        output = run(args)
        for i in range(0, len(shown) + 1, 2):  # typing
            frames.append(frame(title, shown[:i], [], f, fb, cursor=True))
            durations.append(60)
        frames.append(frame(title, shown, [], f, fb, cursor=False))
        durations.append(500)
        for n in range(1, len(output) + 1, 3):  # output appears
            frames.append(frame(title, shown, output[:n], f, fb, cursor=False))
            durations.append(120)
        frames.append(frame(title, shown, output, f, fb, cursor=False))
        durations.append(14000)
    end = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(end)
    for i, text in enumerate(
        [
            "SheetGuard",
            "",
            "8 read-only MCP tools for Claude and other agents",
            "14 model checks  -  recalculation & drivers",
            "prompt-injection scanner  -  untrusted-text envelope",
            "",
            'pip install "sheetguard[mcp,eval]"',
        ]
    ):
        d.text(
            (W // 2, 200 + i * 34),
            text,
            fill=FG if i else ACCENT,
            font=font(28 if i == 0 else 18),
            anchor="mm",
        )
    frames.append(end)
    durations.append(8000)
    OUT.parent.mkdir(exist_ok=True)
    frames[0].save(
        OUT, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True
    )
    print(
        f"wrote {OUT} ({len(frames)} frames, {sum(durations) / 1000:.0f}s, "
        f"{OUT.stat().st_size // 1024} KB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
