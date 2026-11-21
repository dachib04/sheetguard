from sheetguard.core.graph import DepGraph, NameNode, format_node, node_kind
from sheetguard.core.loader import load_workbook
from sheetguard.core.refs import CellKey, RangeKey
from tests.helpers import FIXTURES, build, graph_of


def K(addr: str, sheet: str = "S") -> CellKey:
    from sheetguard.core.refs import parse_cell_address

    return parse_cell_address(addr, default_sheet=sheet)


def test_direct_precedents_and_dependents():
    g = graph_of({"S": {"A1": 1, "A2": 2, "B1": "=A1+A2", "C1": "=B1*2"}})
    assert set(g.precedents(K("B1"))) == {K("A1"), K("A2")}
    assert g.dependents(K("A1")) == (K("B1"),)
    assert g.dependents(K("B1")) == (K("C1"),)
    assert g.is_input(K("A1")) and not g.is_input(K("B1"))
    assert not g.has_dependents(K("C1"))


def test_range_node_and_reverse_lookup():
    g = graph_of({"S": {"A1": 1, "A2": 2, "A3": 3, "B1": "=SUM(A1:A3)"}})
    (rng,) = g.precedents(K("B1"))
    assert rng == RangeKey("S", 1, 1, 3, 1)
    assert set(g.precedents(rng)) == {K("A1"), K("A2"), K("A3")}
    assert g.dependents(K("A2")) == (rng,)
    assert g.has_dependents(K("A3"))


def test_whole_column_bounded_by_used_range():
    g = graph_of({"S": {"A1": 1, "A5": 2, "C1": "=SUM(A:A)", "C2": "=SUM(3:3)"}})
    (rng,) = g.precedents(K("C1"))
    assert rng == RangeKey("S", 1, 1, 5, 1)  # not 1,048,576 rows
    (row3,) = g.precedents(K("C2"))
    assert row3 == RangeKey("S", 3, 1, 3, 3)  # clipped to the used columns A:C
    assert g.precedents(row3) == ()  # and row 3 holds nothing


def test_cross_sheet_and_3d_refs():
    wb = build(
        {
            "A": {"B2": 1},
            "B": {"B2": 2},
            "C": {"B2": 3},
            "Sum": {"A1": "=SUM(A:C!B2)", "A2": "='B'!B2*2", "A3": "=Missing!A1"},
        }
    )
    g = DepGraph(wb)
    assert set(g.precedents(K("A1", "Sum"))) == {K("B2", "A"), K("B2", "B"), K("B2", "C")}
    assert g.precedents(K("A2", "Sum")) == (K("B2", "B"),)
    assert g.flags[K("A3", "Sum")].unresolved == ["missing sheet"]
    assert g.flags[K("A3", "Sum")].partial


def test_sheet_names_case_insensitive():
    g = graph_of({"Data": {"A1": 1}, "S": {"A1": "=data!A1"}})
    assert g.precedents(K("A1")) == (K("A1", "Data"),)


def test_defined_names_scope_shadowing_and_chains():
    wb = build(
        {"S": {"A1": 0.2, "A2": 0.3, "B1": "=Rate*10"}, "T": {"A1": "=Rate*10"}},
        names=[
            ("Rate", None, "S!$A$1"),
            ("Rate", "S", "S!$A$2"),
            ("Alias", None, "Rate"),
            ("Const", None, "0.25"),
            ("Broken", None, "#REF!"),
        ],
    )
    g = DepGraph(wb)
    # sheet-scoped Rate shadows the workbook one inside S
    assert g.precedents(K("B1")) == (NameNode("S", "Rate"),)
    assert g.precedents(NameNode("S", "Rate")) == (K("A2"),)
    # from sheet T, the workbook-scoped Rate applies
    assert g.precedents(K("A1", "T")) == (NameNode(None, "Rate"),)
    assert g.precedents(NameNode(None, "Rate")) == (K("A1"),)


def test_name_to_name_and_constant_names():
    wb = build(
        {"S": {"A1": 5, "B1": "=Alias+Const", "B2": "=Broken"}},
        names=[
            ("Base", None, "S!$A$1"),
            ("Alias", None, "Base"),
            ("Const", None, "0.25"),
            ("Broken", None, "#REF!"),
        ],
    )
    g = DepGraph(wb)
    assert set(g.precedents(K("B1"))) == {NameNode(None, "Alias"), NameNode(None, "Const")}
    assert g.precedents(NameNode(None, "Alias")) == (NameNode(None, "Base"),)
    assert g.is_input(NameNode(None, "Const"))
    assert g.flags[NameNode(None, "Broken")].partial
    assert format_node(NameNode("S", "Rate")) == "S!Rate"
    assert node_kind(NameNode(None, "x")) == "name"


def test_unknown_name_is_unresolved_but_booleans_are_not():
    g = graph_of({"S": {"A1": "=NoSuchName+1", "A2": "=IF(TRUE,1,0)"}})
    assert g.flags[K("A1")].unresolved == ["name NoSuchName"]
    assert g.flags[K("A2")].unresolved == []


def test_structured_references():
    cells = {
        "A1": "Qty",
        "B1": "Price",
        "C1": "Total",
        "A2": 2,
        "B2": 3,
        "C2": "=[@Qty]*[@Price]",
        "A3": 4,
        "B3": 5,
        "C3": "=Sales[[#This Row],[Qty]]*Sales[[#This Row],[Price]]",
        "E1": "=SUM(Sales[Total])",
        "E2": "=COUNTA(Sales[#Headers])",
        "E3": "=SUM(Sales[[Qty]:[Price]])",
        "E4": "=SUM(Nope[X])",
        "E5": "=Sales[Bogus]",
    }
    g = graph_of({"S": cells}, tables=[("Sales", "S", "A1:C3", ["Qty", "Price", "Total"])])
    assert set(g.precedents(K("C2"))) == {K("A2"), K("B2")}
    assert set(g.precedents(K("C3"))) == {K("A3"), K("B3")}
    assert g.precedents(K("E1")) == (RangeKey("S", 2, 3, 3, 3),)
    assert g.precedents(K("E2")) == (RangeKey("S", 1, 1, 1, 3),)
    assert g.precedents(K("E3")) == (RangeKey("S", 2, 1, 3, 2),)
    assert g.flags[K("E4")].unresolved == ["table Nope"]
    assert g.flags[K("E5")].unresolved == ["column Bogus"]


def test_this_row_outside_table_is_unresolved():
    g = graph_of(
        {"S": {"A1": "Qty", "A2": 1, "D9": "=[@Qty]"}}, tables=[("T", "S", "A1:A2", ["Qty"])]
    )
    assert g.flags[K("D9")].partial


def test_external_and_dynamic_flags():
    g = graph_of(
        {
            "S": {
                "A1": "=[1]Prices!B2*2",
                "A2": '=INDIRECT("B"&1)',
                "B1": 1,
                "A3": "=OFFSET(B1,0,0)",
                "A4": "=SUM(",
                "A5": "=#REF!+1",
            }
        }
    )
    assert g.flags[K("A1")].external == ["[1]Prices!B2"]
    assert not g.flags[K("A1")].partial
    assert g.flags[K("A2")].dynamic and g.flags[K("A2")].partial
    assert g.precedents(K("A3")) == (K("B1"),) and g.flags[K("A3")].dynamic
    assert g.flags[K("A4")].parse_error
    assert g.flags[K("A5")].unresolved == ["#REF!"]


def test_cycles_self_mutual_and_through_ranges():
    g = graph_of(
        {
            "S": {
                "A1": "=A1+1",
                "B1": "=C1",
                "C1": "=B1",
                "D1": 1,
                "D2": "=SUM(D1:D3)",
                "D3": 5,
                "E1": "=F1",
                "F1": 2,
            }
        }
    )
    cycles = g.find_cycles()
    assert [K("A1")] in cycles
    assert [K("B1"), K("C1")] in cycles
    assert [K("D2")] in cycles  # D2 sums a range containing itself
    assert len(cycles) == 3


def test_no_false_cycles_on_dag():
    wb = load_workbook(FIXTURES / "loan.xlsx")
    assert DepGraph(wb).find_cycles() == []


def test_deep_chain_does_not_overflow():
    cells = {"A1": 1, **{f"A{i}": f"=A{i - 1}+1" for i in range(2, 5001)}}
    g = graph_of({"S": cells})
    assert g.find_cycles() == []
    assert g.count_upstream(K("A5000"), cap=10_000) == 4999
    long_cycle = {f"A{i}": f"=A{i + 1}" for i in range(1, 3000)}
    long_cycle["A3000"] = "=A1"
    assert len(graph_of({"S": long_cycle}).find_cycles()[0]) == 3000


def test_cross_sheet_chain_more_than_20_levels():
    sheets = {"S0": {"A1": 1}}
    for i in range(1, 30):
        sheets[f"S{i}"] = {"A1": f"=S{i - 1}!A1+1"}
    g = DepGraph(build(sheets))
    inputs, capped, _ = g.upstream_inputs(K("A1", "S29"), cap=10)
    assert inputs == [K("A1", "S0")] and not capped


def test_likely_outputs_and_upstream_inputs():
    wb = load_workbook(FIXTURES / "dcf.xlsx")
    g = DepGraph(wb)
    (top, n), *_ = g.likely_outputs()
    assert top == CellKey("Model", 12, 2) and n > 20
    inputs, capped, flags = g.upstream_inputs(top, cap=200)
    addrs = {format_node(i) for i in inputs}
    assert "Assumptions!B3" in addrs and "Assumptions!B7" in addrs
    assert not capped and not flags.partial
    few, capped, _ = g.upstream_inputs(top, cap=2)
    assert len(few) == 2 and capped


def test_sort_key_orders_by_sheet_then_position():
    g = graph_of({"B": {"A2": 1, "A1": 1}, "A": {"A1": 1}})
    keys = sorted([K("A1", "A"), K("A2", "B"), K("A1", "B")], key=g.sort_key)
    assert keys == [K("A1", "B"), K("A2", "B"), K("A1", "A")]  # workbook order: B before A
