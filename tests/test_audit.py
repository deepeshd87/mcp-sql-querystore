"""Verify audit logging records query outcomes and never leaks SQL text,
parameters, or credentials. The DB cursor is mocked, so no server is needed.
"""

import logging
from unittest.mock import MagicMock, patch

from mcp_sql_querystore import db


class _FakeCursor:
    description = [("col",)]

    def execute(self, *a, **k):
        return None

    def fetchall(self):
        return [("value",)]


def _patch_cursor():
    """Context manager patch that yields a fake cursor from get_cursor."""
    cm = MagicMock()
    cm.__enter__.return_value = _FakeCursor()
    cm.__exit__.return_value = False
    return patch.object(db, "get_cursor", return_value=cm)


def test_success_is_audited(caplog):
    with _patch_cursor(), caplog.at_level(logging.INFO, logger="mcp_sql_querystore.audit"):
        rows = db.run_query("SELECT 1 AS col", tool="get_wait_stats")
    assert rows == [{"col": "value"}]
    msg = caplog.text
    assert "query_ok" in msg
    assert "tool=get_wait_stats" in msg
    assert "rows=1" in msg


def test_secrets_not_in_log(caplog):
    secret_sql = "SELECT 1 WHERE pw = 'super-secret-value'"
    with _patch_cursor(), caplog.at_level(logging.INFO, logger="mcp_sql_querystore.audit"):
        db.run_query(secret_sql, params=("param-secret",), tool="t")
    # neither the SQL text nor the param value may appear in the audit log
    assert "super-secret-value" not in caplog.text
    assert "param-secret" not in caplog.text
    # but a stable fingerprint of the SQL should be present
    assert db._sql_fingerprint(secret_sql) in caplog.text


def test_failure_is_audited(caplog):
    cm = MagicMock()
    bad_cursor = _FakeCursor()

    def boom(*a, **k):
        raise RuntimeError("connection reset")

    bad_cursor.execute = boom
    cm.__enter__.return_value = bad_cursor
    cm.__exit__.return_value = False

    with patch.object(db, "get_cursor", return_value=cm), \
         caplog.at_level(logging.WARNING, logger="mcp_sql_querystore.audit"):
        try:
            db.run_query("SELECT 1", tool="get_wait_stats")
        except RuntimeError:
            pass
    assert "query_failed" in caplog.text
    assert "error=RuntimeError" in caplog.text
    # the exception message itself is not logged (only the type)
    assert "connection reset" not in caplog.text
