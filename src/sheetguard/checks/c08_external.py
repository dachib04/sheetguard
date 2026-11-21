"""C08 - external workbook link (warning).

Detection: formulas or defined names that refer to another workbook, and
external-link parts in the file. Links are recorded, never resolved, opened
or fetched (S-7, S-9).

False-positive policy: none needed; a link is a fact. The same formula pattern
is one issue.
"""

from __future__ import annotations

from collections.abc import Iterable

from sheetguard.checks.base import (
    WORKBOOK_LABEL,
    Check,
    CheckContext,
    cell_evidence,
    formula_r1c1,
    group_by_signature,
    make_issue,
    workbook_key,
)
from sheetguard.core.graph import NameNode
from sheetguard.core.refs import CellKey
from sheetguard.report.schema import Confidence, Evidence, Issue, Severity
from sheetguard.security.envelope import wrap


class ExternalLink(Check):
    id = "C08"
    title = "External workbook link"
    default_severity = Severity.WARNING

    def run(self, ctx: CheckContext) -> Iterable[Issue]:
        g = ctx.graph
        items: list[tuple[CellKey, str]] = []
        used_any = False
        for node, flags in sorted(g.flags.items(), key=lambda kv: g.sort_key(kv[0])):
            if not flags.external:
                continue
            used_any = True
            if isinstance(node, CellKey):
                cell = ctx.wb.cell(node)
                if cell is not None and cell.formula is not None:
                    items.append((node, formula_r1c1(node, cell.formula)))
            elif isinstance(node, NameNode):
                yield make_issue(
                    self.id,
                    self.default_severity,
                    workbook_key(ctx),
                    f"Defined name {node.name} refers to another workbook. The link is "
                    f"recorded, not followed.",
                    evidence=[
                        Evidence(
                            address=node.name,
                            value=wrap(
                                ", ".join(flags.external), f"defined name {node.name} (external)"
                            ),
                        )
                    ],
                    cell_label=f"name {node.name}",
                    discriminator=node.name,
                )
        for group in group_by_signature(items, g):
            key = group[0]
            more = (
                f" The same pattern appears in {len(group) - 1} more cell(s)."
                if len(group) > 1
                else ""
            )
            yield make_issue(
                self.id,
                self.default_severity,
                key,
                "Formula refers to another workbook; values may be stale and the link "
                f"is not followed.{more}",
                evidence=[cell_evidence(ctx, k) for k in group[:3]],
                confidence=Confidence.HIGH,
                affected_count=len(group),
            )
        ac = ctx.wb.active_content
        if ac.has_external_links and not used_any:
            targets = ", ".join(ac.external_link_targets) or "unknown target"
            dde = " It includes a DDE link." if ac.has_dde else ""
            yield make_issue(
                self.id,
                self.default_severity,
                workbook_key(ctx),
                f"The workbook contains external link definitions not used by any formula.{dde}",
                evidence=[
                    Evidence(address=WORKBOOK_LABEL, value=wrap(targets, "external link targets"))
                ],
                cell_label=WORKBOOK_LABEL,
                discriminator="unused-links",
            )
