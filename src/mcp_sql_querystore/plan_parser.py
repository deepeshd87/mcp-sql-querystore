"""Parse SQL Server showplan XML into a compact JSON summary.

Goal: strip the heavy XML down to the things an LLM actually needs to reason
about — missing indexes, implicit conversions (via warnings), key lookups, and
plan-level warnings — so we don't burn tokens on raw showplan.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

# Showplan uses this namespace on every element.
_NS = {"s": "http://schemas.microsoft.com/sqlserver/2004/07/showplan"}


def _tag(elem: ET.Element) -> str:
    return elem.tag.split("}", 1)[-1]


def summarize_plan(plan_xml: str) -> dict[str, Any]:
    """Return a compact dict summary of a showplan XML string."""
    try:
        root = ET.fromstring(plan_xml)
    except ET.ParseError as exc:
        return {"error": f"could not parse plan XML: {exc}"}

    summary: dict[str, Any] = {
        "missing_indexes": _missing_indexes(root),
        "warnings": _warnings(root),
        "key_lookups": _count_ops(root, "Key Lookup"),
        "index_scans": _count_ops(root, "Index Scan"),
        "table_scans": _count_ops(root, "Table Scan"),
        "estimated_subtree_cost": _root_cost(root),
    }
    return summary


def _missing_indexes(root: ET.Element) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for mig in root.iter("{%s}MissingIndexGroup" % _NS["s"]):
        impact = mig.get("Impact")
        for mi in mig.iter("{%s}MissingIndex" % _NS["s"]):
            cols = {"equality": [], "inequality": [], "included": []}
            for cg in mi.iter("{%s}ColumnGroup" % _NS["s"]):
                usage = cg.get("Usage", "").upper()
                names = [c.get("Name") for c in cg.iter("{%s}Column" % _NS["s"])]
                # Check INEQUALITY before EQUALITY: "EQUALITY" is a substring of
                # "INEQUALITY", so a loose "equality in usage" test misclassifies
                # inequality column groups as equality ones.
                if usage == "INEQUALITY":
                    cols["inequality"] = names
                elif usage == "EQUALITY":
                    cols["equality"] = names
                elif usage == "INCLUDE":
                    cols["included"] = names
            out.append(
                {
                    "impact_pct": float(impact) if impact else None,
                    "database": mi.get("Database"),
                    "schema": mi.get("Schema"),
                    "table": mi.get("Table"),
                    "columns": cols,
                }
            )
    return out


def _warnings(root: ET.Element) -> list[str]:
    found: list[str] = []
    for w in root.iter("{%s}Warnings" % _NS["s"]):
        for child in w:
            name = _tag(child)
            # Common ones: PlanAffectingConvert (implicit conversion),
            # SpillToTempDb, ColumnsWithNoStatistics, NoJoinPredicate.
            if name == "PlanAffectingConvert":
                found.append(
                    f"implicit_conversion: {child.get('Expression', '')[:200]}"
                )
            else:
                found.append(name)
    return found


def _count_ops(root: ET.Element, physical_op: str) -> int:
    return sum(
        1
        for rel in root.iter("{%s}RelOp" % _NS["s"])
        if rel.get("PhysicalOp") == physical_op
    )


def _root_cost(root: ET.Element) -> float | None:
    for stmt in root.iter("{%s}StmtSimple" % _NS["s"]):
        cost = stmt.get("StatementSubTreeCost")
        if cost:
            try:
                return float(cost)
            except ValueError:
                return None
    return None
