import pytest

from sheetguard.core.refs import (
    MAX_COL,
    MAX_ROW,
    AreaRef,
    CellKey,
    ExternalRef,
    NameRef,
    RangeKey,
    StructRef,
    UnparsedRef,
    analyze_formula,
    format_cell,
    format_range,
    parse_cell_address,
    parse_operand,
    parse_range_address,
    quote_sheet,
    split_sheet_prefix,
    to_r1c1,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A1", AreaRef(None, 1, 1, 1, 1)),
        ("$B$2:C5", AreaRef(None, 2, 2, 5, 3)),
        ("C5:B2", AreaRef(None, 2, 2, 5, 3)),
        ("A:A", AreaRef(None, 1, 1, MAX_ROW, 1)),
        ("$B:$D", AreaRef(None, 1, 2, MAX_ROW, 4)),
        ("3:3", AreaRef(None, 3, 1, 3, MAX_COL)),
        ("'My Sheet'!A1", AreaRef("My Sheet", 1, 1, 1, 1)),
        ("'It''s'!B2", AreaRef("It's", 2, 2, 2, 2)),
        ("'A1'!C3", AreaRef("A1", 3, 3, 3, 3)),
        ("'Données €'!A1", AreaRef("Données €", 1, 1, 1, 1)),
        ("Sheet1:Sheet3!B2", AreaRef("Sheet1", 2, 2, 2, 2, sheet_end="Sheet3")),
        ("Sheet1!A1:Sheet1!B2", AreaRef("Sheet1", 1, 1, 2, 2)),
        ("TaxRate", NameRef("TaxRate")),
        ("Sheet1!Rate", NameRef("Rate", "Sheet1")),
        ("XFE1", NameRef("XFE1")),  # beyond the last column: a name, not a cell
    ],
)
def test_parse_operand(text, expected):
    assert parse_operand(text) == expected


@pytest.mark.parametrize("text", ["[1]Ext!C3", "'C:\\x\\[Book.xlsx]S'!A1", "[Book.xlsx]Sheet1!A1"])
def test_external_refs(text):
    assert isinstance(parse_operand(text), ExternalRef)


@pytest.mark.parametrize(
    ("text", "table", "specials", "c1", "c2"),
    [
        ("Table1[Revenue]", "Table1", set(), "Revenue", "Revenue"),
        ("Table1[[#This Row],[Qty]]", "Table1", {"#this row"}, "Qty", "Qty"),
        ("[@Price]", None, {"#this row"}, "Price", "Price"),
        ("[@[Unit Price]]", None, {"#this row"}, "Unit Price", "Unit Price"),
        ("Table1[[#Headers],[A]:[C]]", "Table1", {"#headers"}, "A", "C"),
        ("Table1[#All]", "Table1", {"#all"}, None, None),
        ("Table1[]", "Table1", set(), None, None),
        ("Table1[a!b]", "Table1", set(), "a!b", "a!b"),
        ("[Revenue]", None, set(), "Revenue", "Revenue"),
    ],
)
def test_structured_refs(text, table, specials, c1, c2):
    ref = parse_operand(text)
    assert isinstance(ref, StructRef)
    assert ref.table == table
    assert set(ref.specials) == specials
    assert (ref.col_start, ref.col_end) == (c1, c2)


def test_unparsed():
    assert isinstance(parse_operand("A1:INDEX(B:B,2)"), UnparsedRef)


@pytest.mark.parametrize(
    ("name", "quoted"),
    [
        ("Sheet1", "Sheet1"),
        ("My Sheet", "'My Sheet'"),
        ("A1", "'A1'"),
        ("R1C1", "'R1C1'"),
        ("It's", "'It''s'"),
        ("Q1", "'Q1'"),
        ("Données", "'Données'"),
        ("2026", "'2026'"),
    ],
)
def test_quote_sheet(name, quoted):
    assert quote_sheet(name) == quoted


def test_split_prefix_roundtrip():
    for name in ["Sheet1", "My Sheet", "It's", "A1", "x!y"[:1]]:
        sheet, rest = split_sheet_prefix(f"{quote_sheet(name)}!B2")
        assert (sheet, rest) == (name, "B2")
    assert split_sheet_prefix("'unterminated!A1") == (None, "'unterminated!A1")
    assert split_sheet_prefix("A1") == (None, "A1")


def test_format_and_parse_addresses():
    key = CellKey("My Sheet", 7, 2)
    assert format_cell(key) == "'My Sheet'!B7"
    assert parse_cell_address("'My Sheet'!B7") == key
    assert parse_cell_address("B7", default_sheet="S") == CellKey("S", 7, 2)
    assert format_range(RangeKey("S", 1, 1, MAX_ROW, 2)) == "S!A:B"
    assert format_range(RangeKey("S", 3, 1, 4, MAX_COL)) == "S!3:4"
    assert format_range(RangeKey("S", 2, 2, 2, 2)) == "S!B2"
    assert parse_range_address("S!A1:C3") == RangeKey("S", 1, 1, 3, 3)
    with pytest.raises(ValueError, match="cell address"):
        parse_cell_address("nonsense")
    with pytest.raises(ValueError, match="range"):
        parse_range_address("S!zz")


def test_analyze_formula():
    info = analyze_formula('=IFERROR(_xlfn.XLOOKUP(A1,B:B,C:C),"no ""match""")+12.5+#REF!')
    assert info.ok
    assert info.functions == ["IFERROR", "XLOOKUP"]
    assert info.numbers == ["12.5"]
    assert info.texts == ['no "match"']
    assert info.errors == ["#REF!"]
    assert len(info.refs) == 3
    assert not info.is_dynamic
    assert analyze_formula("=OFFSET(A1,1,1)+NOW()").volatile_functions == ["NOW", "OFFSET"]
    assert analyze_formula("=INDIRECT(A1)").is_dynamic


def test_analyze_formula_bad_input():
    assert not analyze_formula('=SUM(A1,"unterminated').ok


def test_r1c1_same_pattern_when_filled():
    assert to_r1c1("=SUM(B2:B5)*$A$1+C3", 6, 3) == to_r1c1("=sum( B3:B6 )*$A$1+C4", 7, 3)
    assert to_r1c1("=B2", 2, 3) == "=RC[-1]"
    assert to_r1c1("=$B2+B$2+A:A+3:3", 4, 2) == "=R[-2]C2+R2C+C[-1]:C[-1]+R[-1]:R[-1]"
    assert to_r1c1("=Other!A1", 1, 1) == "=Other!RC"


def test_r1c1_differs_for_outlier():
    assert to_r1c1("=E2*E3", 4, 5) != to_r1c1("=E2*E2", 4, 5)
