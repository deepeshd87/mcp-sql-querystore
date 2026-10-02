"""Tests for the multi-database sweep's orchestration logic.

The DB layer is mocked, so no SQL Server is needed. We verify:
  - a failing database does not abort the whole sweep
  - databases with no regressions are omitted from results
  - results are ranked by worst pct_change
  - an explicit database_names list bypasses enumeration
"""

from unittest.mock import patch

from mcp_sql_querystore import server


def test_sweep_tolerates_one_failing_db():
    def fake_regression(database, **kwargs):
        if database == "BadDB":
            raise RuntimeError("Query Store off despite cached bit")
        if database == "QuietDB":
            return []  # runs fine, no regressions
        return [{"query_id": 1, "pct_change": 0.8}]

    with patch.object(server, "run_query", return_value=[
        {"name": "GoodDB"}, {"name": "BadDB"}, {"name": "QuietDB"}
    ]), patch.object(server, "_run_regression", side_effect=fake_regression):
        out = server._sweep_regressions({})

    assert out["databases_scanned"] == 3
    assert out["databases_with_regressions"] == 1          # only GoodDB
    assert out["results"][0]["database"] == "GoodDB"
    assert len(out["errors"]) == 1
    assert out["errors"][0]["database"] == "BadDB"


def test_sweep_ranks_by_worst_pct_change():
    def fake_regression(database, **kwargs):
        mapping = {
            "DbLow":  [{"query_id": 1, "pct_change": 0.2}],
            "DbHigh": [{"query_id": 2, "pct_change": 3.0}],
            "DbMid":  [{"query_id": 3, "pct_change": 0.9}],
        }
        return mapping[database]

    with patch.object(server, "run_query", return_value=[
        {"name": "DbLow"}, {"name": "DbHigh"}, {"name": "DbMid"}
    ]), patch.object(server, "_run_regression", side_effect=fake_regression):
        out = server._sweep_regressions({})

    order = [r["database"] for r in out["results"]]
    assert order == ["DbHigh", "DbMid", "DbLow"]


def test_explicit_db_list_skips_enumeration():
    called = {"enumerate": False}

    def fake_run_query(*a, **k):
        called["enumerate"] = True
        return [{"name": "ShouldNotBeUsed"}]

    with patch.object(server, "run_query", side_effect=fake_run_query), \
         patch.object(server, "_run_regression", return_value=[]):
        out = server._sweep_regressions({"database_names": ["ExplicitA", "ExplicitB"]})

    # enumeration query must NOT have been called when a list is supplied
    assert called["enumerate"] is False
    assert out["databases_scanned"] == 2
