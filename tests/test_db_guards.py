"""Unit tests for the db-layer guardrails — validate_db_name, assert_select_only.

These are the defense-in-depth checks. No database connection is made.
"""

import pytest

from mcp_sql_querystore.db import assert_select_only, validate_db_name


@pytest.mark.parametrize("name", ["master", "RAG", "OrderManagement", "DB_1", "a$b#c"])
def test_valid_db_names_pass(name):
    assert validate_db_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "a; DROP TABLE x",
        "master;--",
        "db name with spaces",
        "[bracketed]",
        "1startswithdigit",
        "",
        "'; DELETE FROM y; --",
        "a" * 200,  # too long
    ],
)
def test_invalid_db_names_rejected(name):
    with pytest.raises(ValueError):
        validate_db_name(name)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM sys.query_store_query",
        "  select top 5 * from t  ",
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
    ],
)
def test_select_only_allows_selects(sql):
    assert_select_only(sql)  # should not raise


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM x",
        "INSERT INTO x VALUES (1)",
        "UPDATE x SET a=1",
        "DROP TABLE x",
        "ALTER DATABASE x SET QUERY_STORE = OFF",
        "TRUNCATE TABLE x",
        "EXEC sp_who",
        "SELECT 1; DROP TABLE x",  # stacked statement
        "GRANT SELECT TO y",
        "BACKUP DATABASE x TO DISK='c:\\x.bak'",
    ],
)
def test_select_only_blocks_writes(sql):
    with pytest.raises(ValueError):
        assert_select_only(sql)
