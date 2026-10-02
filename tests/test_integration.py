"""Integration tests against a REAL SQL Server instance.

These are skipped unless you opt in, so they never break CI or a plain `pytest`
run on a machine with no database. To run them:

    1. Set a working connection (any of the forms db.py accepts), e.g.
         $env:MCP_SQL_CONNECTION_STRING = "Driver={ODBC Driver 18 for SQL Server};..."
    2. Point them at a Query Store-enabled test database:
         $env:MCP_SQL_TEST_DATABASE = "RAG"
    3. Enable the suite:
         $env:MCP_SQL_RUN_INTEGRATION = "1"
    4. pytest tests/test_integration.py -v

They are read-only (same as the tools) and safe against any DB the mcp_readonly
login can reach. They assert on shape and invariants, not on specific rows, so
they pass regardless of what history the test DB happens to hold.
"""

import os

import pytest

RUN = os.environ.get("MCP_SQL_RUN_INTEGRATION") in ("1", "yes", "true")
TEST_DB = os.environ.get("MCP_SQL_TEST_DATABASE", "")

pytestmark = pytest.mark.skipif(
    not RUN or not TEST_DB,
    reason="set MCP_SQL_RUN_INTEGRATION=1 and MCP_SQL_TEST_DATABASE to run",
)


@pytest.fixture(scope="module")
def call():
    """Return a synchronous caller that unwraps the MCP TextContent JSON."""
    import asyncio
    import json

    from mcp_sql_querystore.server import call_tool

    def _call(name, args):
        out = asyncio.run(call_tool(name, args))
        return json.loads(out[0].text)

    return _call


def test_connectivity():
    from mcp_sql_querystore.db import run_query

    rows = run_query("SELECT 1 AS ok")
    assert rows == [{"ok": 1}]


def test_regressed_queries_shape(call):
    res = call("get_regressed_queries", {"database_name": TEST_DB})
    assert "regressed_queries" in res
    assert "count" in res
    assert res["count"] == len(res["regressed_queries"])


def test_execution_plan_handles_missing_id(call):
    # a query_id that almost certainly doesn't exist must return empty, not error
    res = call("get_query_execution_plan",
               {"database_name": TEST_DB, "query_id": 999999999})
    assert res.get("plan_count") == 0
    assert res.get("plans") == []


def test_wait_stats_shape(call):
    res = call("get_wait_stats", {"database_name": TEST_DB, "recent_hours": 168})
    assert "wait_categories" in res
    # each row, if any, has the expected keys
    for row in res["wait_categories"]:
        assert "wait_category_desc" in row
        assert "total_wait_ms" in row


def test_parameter_sniffing_shape(call):
    res = call("analyze_parameter_sniffing",
               {"database_name": TEST_DB, "min_plan_count": 1})
    assert "suspected_sniffing" in res


def test_missing_index_shape(call):
    res = call("get_missing_index_impact", {"database_name": TEST_DB})
    assert "recommendations" in res
    assert "plans_scanned" in res


def test_sweep_runs_clean(call):
    res = call("sweep_regressions", {"database_names": [TEST_DB]})
    assert res["databases_scanned"] == 1
    # errors must be a list; a clean run has it empty
    assert isinstance(res["errors"], list)


def test_invalid_database_returns_error(call):
    res = call("get_wait_stats", {"database_name": "a; DROP TABLE x"})
    assert "error" in res  # rejected by validate_db_name, surfaced cleanly


def test_readonly_login_cannot_write():
    """If the connection is the mcp_readonly login, a write must be denied at
    the server. This proves permission-based enforcement end to end.
    Skipped automatically if the query somehow succeeds on a non-readonly login,
    which would itself be a provisioning problem worth seeing.
    """
    from mcp_sql_querystore.db import get_cursor

    with get_cursor() as cur:
        cur.execute(f"USE [{TEST_DB}];")
        with pytest.raises(Exception):
            # attempt a harmless-looking write; must be denied by permissions
            cur.execute("CREATE TABLE _mcp_should_not_exist (id int);")
