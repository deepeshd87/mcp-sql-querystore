"""Unit tests for plan_parser.summarize_plan — pure, no database needed."""

from mcp_sql_querystore.plan_parser import summarize_plan

NS = 'xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan"'


def _wrap(inner: str, subtree_cost: str = "1.0") -> str:
    return f"""<?xml version="1.0"?>
<ShowPlanXML {NS}>
 <BatchSequence><Batch><Statements>
  <StmtSimple StatementSubTreeCost="{subtree_cost}">
   <QueryPlan>{inner}</QueryPlan>
  </StmtSimple>
 </Statements></Batch></BatchSequence>
</ShowPlanXML>"""


def test_missing_index_extracted():
    inner = """
    <MissingIndexes>
     <MissingIndexGroup Impact="92.5">
      <MissingIndex Database="[App]" Schema="[dbo]" Table="[Orders]">
       <ColumnGroup Usage="EQUALITY"><Column Name="[CustomerId]"/></ColumnGroup>
       <ColumnGroup Usage="INEQUALITY"><Column Name="[OrderDate]"/></ColumnGroup>
       <ColumnGroup Usage="INCLUDE"><Column Name="[Total]"/></ColumnGroup>
      </MissingIndex>
     </MissingIndexGroup>
    </MissingIndexes>"""
    out = summarize_plan(_wrap(inner))
    assert len(out["missing_indexes"]) == 1
    mi = out["missing_indexes"][0]
    assert mi["impact_pct"] == 92.5
    assert mi["table"] == "[Orders]"
    assert mi["columns"]["equality"] == ["[CustomerId]"]
    assert mi["columns"]["inequality"] == ["[OrderDate]"]
    assert mi["columns"]["included"] == ["[Total]"]


def test_implicit_conversion_warning():
    inner = """
    <Warnings>
     <PlanAffectingConvert ConvertIssue="Cardinality Estimate"
        Expression="CONVERT(int,[x])"/>
    </Warnings>"""
    out = summarize_plan(_wrap(inner))
    assert any("implicit_conversion" in w for w in out["warnings"])


def test_operator_counts():
    inner = """
    <RelOp PhysicalOp="Key Lookup"/>
    <RelOp PhysicalOp="Key Lookup"/>
    <RelOp PhysicalOp="Index Scan"/>
    <RelOp PhysicalOp="Table Scan"/>"""
    out = summarize_plan(_wrap(inner))
    assert out["key_lookups"] == 2
    assert out["index_scans"] == 1
    assert out["table_scans"] == 1


def test_clean_plan_returns_empty_lists():
    """A plan with no indexes/warnings must return empties, not raise."""
    out = summarize_plan(_wrap("<RelOp PhysicalOp='Clustered Index Seek'/>"))
    assert out["missing_indexes"] == []
    assert out["warnings"] == []
    assert out["key_lookups"] == 0


def test_subtree_cost_parsed():
    out = summarize_plan(_wrap("<RelOp PhysicalOp='Index Seek'/>", subtree_cost="3.14"))
    assert out["estimated_subtree_cost"] == 3.14


def test_malformed_xml_returns_error_not_raise():
    out = summarize_plan("<this is not valid xml")
    assert "error" in out


def test_empty_string_returns_error():
    out = summarize_plan("")
    assert "error" in out
