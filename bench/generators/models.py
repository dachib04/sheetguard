"""Clean-corpus generators: six families of realistic, *clean* financial models.

Every model is built from a seeded ``random.Random`` so the corpus is
reproducible. Families vary sheet names, sizes, start positions and inputs so
the checks are not tested against one layout. All files are synthetic: no
client or employer data, ever (spec §10.2).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter as L
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.table import Table, TableStyleInfo

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def dcf(path: Path, rng: random.Random) -> None:
    years = rng.randint(5, 8)
    start_year = rng.choice([2025, 2026, 2027])
    wb = openpyxl.Workbook()
    a = wb.active
    a.title = rng.choice(["Assumptions", "Inputs", "Drivers"])
    inputs = [
        ("Base revenue", rng.randrange(500_000, 5_000_000, 10_000)),
        ("Revenue growth", round(rng.uniform(0.03, 0.15), 3)),
        ("EBITDA margin", round(rng.uniform(0.12, 0.35), 3)),
        ("Tax rate", rng.choice([0.19, 0.21, 0.25, 0.28])),
        ("Capex % revenue", round(rng.uniform(0.02, 0.08), 3)),
        ("WACC", round(rng.uniform(0.07, 0.12), 3)),
        ("Terminal growth", round(rng.uniform(0.01, 0.03), 3)),
    ]
    a["A1"], a["B1"] = "Input", "Value"
    for i, (label, value) in enumerate(inputs, start=2):
        a.cell(i, 1, label)
        a.cell(i, 2, value)
    s = a.title
    wb.defined_names["WACC"] = DefinedName("WACC", attr_text=f"{s}!$B$7")
    m = wb.create_sheet("Valuation")
    m["A1"] = "Line"
    rows = ["Revenue", "EBITDA", "Tax", "Capex", "FCF", "Discount factor", "PV"]
    for i, label in enumerate(rows, start=2):
        m.cell(i, 1, label)
    for j in range(years):
        c = 2 + j
        X, P = L(c), L(c - 1)
        m.cell(1, c, start_year + j)
        m[f"{X}2"] = f"={s}!$B$2" if j == 0 else f"={P}2*(1+{s}!$B$3)"
        m[f"{X}3"] = f"={X}2*{s}!$B$4"
        m[f"{X}4"] = f"={X}3*{s}!$B$5"
        m[f"{X}5"] = f"={X}2*{s}!$B$6"
        m[f"{X}6"] = f"={X}3-{X}4-{X}5"
        m[f"{X}7"] = f"=1/(1+WACC)^({X}1-$B$1+1)"
        m[f"{X}8"] = f"={X}6*{X}7"
    last = L(1 + years)
    m["A10"], m["B10"] = "Sum of PV", f"=SUM(B8:{last}8)"
    m["A11"], m["B11"] = "Terminal value", (f"={last}6*(1+{s}!$B$8)/(WACC-{s}!$B$8)*{last}7")
    m["A12"], m["B12"] = "Enterprise value", "=B10+B11"
    wb.save(path)


def budget(path: Path, rng: random.Random) -> None:
    cats = rng.sample(
        [
            "Rent",
            "Salaries",
            "Software",
            "Travel",
            "Marketing",
            "Insurance",
            "Utilities",
            "Training",
            "Legal",
            "Equipment",
        ],
        rng.randint(4, 8),
    )
    wb = openpyxl.Workbook()
    b = wb.active
    b.title = f"Budget {rng.choice([2025, 2026, 2027])}"
    b.append(["Category", *MONTHS, "Total"])
    for r, cat in enumerate(cats, start=2):
        b.cell(r, 1, cat)
        base = rng.randrange(500, 20_000, 50)
        for c in range(2, 14):
            b.cell(r, c, round(base * rng.uniform(0.9, 1.15)))
        b.cell(r, 14, f"=SUM(B{r}:M{r})")
    n = len(cats) + 1
    tab = Table(displayName="BudgetLines", ref=f"A1:N{n}")
    tab.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
    b.add_table(tab)
    tr = n + 1
    b.cell(tr, 1, "Total")
    for c in range(2, 15):
        b.cell(tr, c, f"=SUM({L(c)}2:{L(c)}{n})")
    s = wb.create_sheet("Summary")
    q = f"'{b.title}'"
    s["A1"], s["B1"] = "Annual total", "=SUM(BudgetLines[Total])"
    s["A2"], s["B2"] = "Average month", "=B1/12"
    s["A3"], s["B3"] = "Largest line", f"=MAX({q}!N2:N{n})"
    s["A4"], s["B4"] = "First half", f"=SUM({q}!B{tr}:G{tr})"
    wb.save(path)


def loan(path: Path, rng: random.Random) -> None:
    months = rng.choice([24, 36, 48, 60, 84, 120])
    wb = openpyxl.Workbook()
    i = wb.active
    i.title = "Inputs"
    i["A1"], i["B1"] = "Principal", rng.randrange(20_000, 900_000, 5_000)
    i["A2"], i["B2"] = "Annual rate", round(rng.uniform(0.02, 0.09), 4)
    i["A3"], i["B3"] = "Months", months
    i["A4"], i["B4"] = "Payment", "=PMT(B2/12,B3,-B1)"
    s = wb.create_sheet("Schedule")
    s.append(["Period", "Opening", "Interest", "Payment", "Principal", "Closing"])
    for p in range(1, months + 1):
        r = p + 1
        s[f"A{r}"] = p
        s[f"B{r}"] = "=Inputs!$B$1" if p == 1 else f"=F{r - 1}"
        s[f"C{r}"] = f"=B{r}*Inputs!$B$2/12"
        s[f"D{r}"] = "=Inputs!$B$4"
        s[f"E{r}"] = f"=D{r}-C{r}"
        s[f"F{r}"] = f"=B{r}-E{r}"
    t = months + 3
    s[f"A{t}"] = "Total"
    for col in "CDE":
        s[f"{col}{t}"] = f"=SUM({col}2:{col}{months + 1})"
    wb.save(path)


def forecast(path: Path, rng: random.Random) -> None:
    wb = openpyxl.Workbook()
    a = wb.active
    a.title = "Inputs"
    a["A1"], a["B1"] = "Starting units", rng.randrange(100, 5_000, 10)
    a["A2"], a["B2"] = "Monthly growth", round(rng.uniform(0.005, 0.06), 3)
    a["A3"], a["B3"] = "Unit price", round(rng.uniform(5, 400), 2)
    a["A4"], a["B4"] = "Price increase (annual)", round(rng.uniform(0.0, 0.08), 3)
    a["A5"], a["B5"] = "Gross margin", round(rng.uniform(0.3, 0.8), 2)
    f = wb.create_sheet(rng.choice(["Forecast", "Sales", "Plan"]))
    f.append(["Line", *MONTHS, "Total"])
    f.append(["Seasonality", *[round(rng.uniform(0.8, 1.3), 2) for _ in MONTHS]])
    for c in range(2, 14):
        X, P = L(c), L(c - 1)
        f[f"{X}3"] = "=Inputs!$B$1" if c == 2 else f"={P}3*(1+Inputs!$B$2)"
        f[f"{X}4"] = f"={X}3*{X}2"
        f[f"{X}5"] = "=Inputs!$B$3" if c == 2 else f"={P}5*(1+Inputs!$B$4/12)"
        f[f"{X}6"] = f"={X}4*{X}5"
        f[f"{X}7"] = f"={X}6*Inputs!$B$5"
    for r, label in zip(
        range(3, 8), ["Base units", "Units", "Price", "Revenue", "Gross profit"], strict=True
    ):
        f.cell(r, 1, label)
    for r in (4, 6, 7):
        f[f"N{r}"] = f"=SUM(B{r}:M{r})"
    f["A9"], f["B9"] = "Average monthly revenue", "=AVERAGE(B6:M6)"
    f["A10"], f["B10"] = "Gross margin check", "=N7/N6"
    wb.save(path)


def captable(path: Path, rng: random.Random) -> None:
    names = rng.sample(
        [
            "Founder A",
            "Founder B",
            "Founder C",
            "Seed fund",
            "Angels",
            "Series A lead",
            "Employees",
            "Advisors",
        ],
        rng.randint(4, 7),
    )
    wb = openpyxl.Workbook()
    c = wb.active
    c.title = "Cap table"
    c.append(["Holder", "Shares", "Ownership"])
    for holder in names:
        c.append([holder, rng.randrange(100_000, 5_000_000, 10_000)])
    pool_row = len(names) + 2
    c.cell(pool_row, 1, "Option pool")
    c.cell(pool_row, 2, rng.randrange(200_000, 1_500_000, 10_000))
    total = pool_row + 2
    for r in range(2, pool_row + 1):
        c.cell(r, 3, f"=B{r}/$B${total}")
    c.cell(total, 1, "Total")
    c.cell(total, 2, f"=SUM(B2:B{pool_row})")
    c.cell(total, 3, f"=SUM(C2:C{pool_row})")
    v = wb.create_sheet("Round")
    v["A1"], v["B1"] = "Pre-money valuation", rng.randrange(2_000_000, 60_000_000, 500_000)
    v["A2"], v["B2"] = "Investment", rng.randrange(500_000, 15_000_000, 250_000)
    v["A3"], v["B3"] = "Post-money", "=B1+B2"
    v["A4"], v["B4"] = "Price per share", f"=B1/'Cap table'!B{total}"
    v["A5"], v["B5"] = "New shares", "=B2/B4"
    v["A6"], v["B6"] = "Investor ownership", f"=B5/('Cap table'!B{total}+B5)"
    wb.save(path)


def pnl(path: Path, rng: random.Random) -> None:
    quarters = rng.choice([4, 8, 12])
    wb = openpyxl.Workbook()
    a = wb.active
    a.title = "Assumptions"
    a["A1"], a["B1"] = "COGS % revenue", round(rng.uniform(0.3, 0.6), 2)
    a["A2"], a["B2"] = "Opex growth / quarter", round(rng.uniform(0.0, 0.05), 3)
    a["A3"], a["B3"] = "Tax rate", rng.choice([0.15, 0.2, 0.25])
    a["A4"], a["B4"] = "Revenue growth / quarter", round(rng.uniform(0.01, 0.08), 3)
    p = wb.create_sheet("P&L")
    p.cell(1, 1, "Line")
    lines = rng.sample(["Product", "Services", "Licences", "Support"], rng.randint(2, 4))
    opex = rng.sample(["Salaries", "Rent", "Marketing", "R&D", "G&A"], rng.randint(2, 5))
    for q in range(quarters):
        p.cell(1, 2 + q, f"Q{q % 4 + 1} {2026 + q // 4}")
    r = 2
    rev_rows = []
    for name in lines:
        p.cell(r, 1, f"Revenue - {name}")
        base = rng.randrange(50_000, 900_000, 1_000)
        for q in range(quarters):
            col = 2 + q
            p.cell(r, col, base if q == 0 else f"={L(col - 1)}{r}*(1+Assumptions!$B$4)")
        rev_rows.append(r)
        r += 1
    tot_rev = r
    p.cell(r, 1, "Total revenue")
    for q in range(quarters):
        X = L(2 + q)
        p.cell(r, 2 + q, f"=SUM({X}{rev_rows[0]}:{X}{rev_rows[-1]})")
    r += 1
    cogs = r
    p.cell(r, 1, "COGS")
    for q in range(quarters):
        X = L(2 + q)
        p.cell(r, 2 + q, f"={X}{tot_rev}*Assumptions!$B$1")
    r += 1
    gp = r
    p.cell(r, 1, "Gross profit")
    for q in range(quarters):
        X = L(2 + q)
        p.cell(r, 2 + q, f"={X}{tot_rev}-{X}{cogs}")
    r += 1
    op_rows = []
    for name in opex:
        p.cell(r, 1, name)
        base = rng.randrange(10_000, 200_000, 1_000)
        for q in range(quarters):
            col = 2 + q
            p.cell(r, col, base if q == 0 else f"={L(col - 1)}{r}*(1+Assumptions!$B$2)")
        op_rows.append(r)
        r += 1
    tot_op = r
    p.cell(r, 1, "Total opex")
    for q in range(quarters):
        X = L(2 + q)
        p.cell(r, 2 + q, f"=SUM({X}{op_rows[0]}:{X}{op_rows[-1]})")
    r += 1
    ebitda = r
    p.cell(r, 1, "EBITDA")
    for q in range(quarters):
        X = L(2 + q)
        p.cell(r, 2 + q, f"={X}{gp}-{X}{tot_op}")
    r += 1
    p.cell(r, 1, "Tax")
    for q in range(quarters):
        X = L(2 + q)
        p.cell(r, 2 + q, f"=MAX(0,{X}{ebitda})*Assumptions!$B$3")
    r += 1
    p.cell(r, 1, "Net income")
    for q in range(quarters):
        X = L(2 + q)
        p.cell(r, 2 + q, f"={X}{ebitda}-{X}{r - 1}")
    last = L(1 + quarters)
    p.cell(r + 2, 1, "Net income, all quarters")
    p.cell(r + 2, 2, f"=SUM(B{r}:{last}{r})")
    wb.save(path)


FAMILIES: dict[str, Callable[[Path, random.Random], None]] = {
    "dcf": dcf,
    "budget": budget,
    "loan": loan,
    "forecast": forecast,
    "captable": captable,
    "pnl": pnl,
}
