"""Unit tests for connection-string resolution and secret handling.

No database is touched — these exercise _connection_string() and its helpers
by manipulating environment variables. Each test clears the relevant vars first
so tests don't leak into each other.
"""

import pytest

from mcp_sql_querystore import db

# All env vars the resolver looks at, cleared before each test.
_VARS = [
    "MCP_SQL_CONNECTION_STRING", "MCP_SQL_CONNECTION_STRING_FILE",
    "MCP_SQL_SERVER", "MCP_SQL_DATABASE", "MCP_SQL_DRIVER",
    "MCP_SQL_ENCRYPT", "MCP_SQL_TRUST_CERT", "MCP_SQL_EXTRA",
    "MCP_SQL_TRUSTED", "MCP_SQL_UID", "MCP_SQL_PWD",
    "MCP_SQL_PWD_FILE", "MCP_SQL_PWD_ENV", "MY_SECRET_PWD",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


def test_full_string_wins(monkeypatch):
    monkeypatch.setenv("MCP_SQL_CONNECTION_STRING", "Driver={x};Server=y;")
    # even if parts are also set, the full string takes precedence
    monkeypatch.setenv("MCP_SQL_SERVER", "ignored")
    assert db._connection_string() == "Driver={x};Server=y;"


def test_full_string_from_file(tmp_path, monkeypatch):
    f = tmp_path / "conn.txt"
    f.write_text("Driver={x};Server=fromfile;\n")
    monkeypatch.setenv("MCP_SQL_CONNECTION_STRING_FILE", str(f))
    assert db._connection_string() == "Driver={x};Server=fromfile;"


def test_assembled_integrated_auth(monkeypatch):
    monkeypatch.setenv("MCP_SQL_SERVER", "localhost\\INST")
    monkeypatch.setenv("MCP_SQL_TRUSTED", "yes")
    cs = db._connection_string()
    assert "Server=localhost\\INST" in cs
    assert "Trusted_Connection=yes" in cs
    assert "PWD=" not in cs  # no password anywhere in integrated auth


def test_assembled_sql_auth_password_from_file(tmp_path, monkeypatch):
    pf = tmp_path / "pwd.txt"
    pf.write_text("s3cr3t-from-vault\n")
    monkeypatch.setenv("MCP_SQL_SERVER", "host")
    monkeypatch.setenv("MCP_SQL_UID", "mcp_readonly")
    monkeypatch.setenv("MCP_SQL_PWD_FILE", str(pf))
    cs = db._connection_string()
    assert "UID=mcp_readonly" in cs
    assert "PWD=s3cr3t-from-vault" in cs  # trimmed of trailing newline


def test_assembled_sql_auth_password_from_named_env(monkeypatch):
    monkeypatch.setenv("MCP_SQL_SERVER", "host")
    monkeypatch.setenv("MCP_SQL_UID", "mcp_readonly")
    monkeypatch.setenv("MCP_SQL_PWD_ENV", "MY_SECRET_PWD")
    monkeypatch.setenv("MY_SECRET_PWD", "env-sourced-pw")
    cs = db._connection_string()
    assert "PWD=env-sourced-pw" in cs


def test_pwd_env_points_to_missing_var_raises(monkeypatch):
    monkeypatch.setenv("MCP_SQL_SERVER", "host")
    monkeypatch.setenv("MCP_SQL_UID", "u")
    monkeypatch.setenv("MCP_SQL_PWD_ENV", "DOES_NOT_EXIST")
    with pytest.raises(RuntimeError):
        db._connection_string()


def test_sql_auth_without_password_raises(monkeypatch):
    monkeypatch.setenv("MCP_SQL_SERVER", "host")
    monkeypatch.setenv("MCP_SQL_UID", "u")
    # no password source at all
    with pytest.raises(RuntimeError):
        db._connection_string()


def test_no_config_at_all_raises(monkeypatch):
    with pytest.raises(RuntimeError):
        db._connection_string()


def test_missing_secret_file_raises(monkeypatch):
    monkeypatch.setenv("MCP_SQL_CONNECTION_STRING_FILE", "/no/such/file/here")
    with pytest.raises(RuntimeError):
        db._connection_string()


def test_password_file_with_utf8_bom(tmp_path, monkeypatch):
    # PowerShell Out-File and some secret mounts prepend a BOM; must be tolerated.
    pf = tmp_path / "pwd_bom.txt"
    pf.write_bytes(b"\xef\xbb\xbfbom-password")  # UTF-8 BOM + value
    monkeypatch.setenv("MCP_SQL_SERVER", "host")
    monkeypatch.setenv("MCP_SQL_UID", "u")
    monkeypatch.setenv("MCP_SQL_PWD_FILE", str(pf))
    cs = db._connection_string()
    assert "PWD=bom-password" in cs
    assert "\ufeff" not in cs  # BOM char must not leak into the string


def test_password_file_utf16(tmp_path, monkeypatch):
    pf = tmp_path / "pwd_utf16.txt"
    pf.write_text("utf16-password", encoding="utf-16")  # BOM + UTF-16 bytes
    monkeypatch.setenv("MCP_SQL_SERVER", "host")
    monkeypatch.setenv("MCP_SQL_UID", "u")
    monkeypatch.setenv("MCP_SQL_PWD_FILE", str(pf))
    cs = db._connection_string()
    assert "PWD=utf16-password" in cs


def test_extra_appended(monkeypatch):
    monkeypatch.setenv("MCP_SQL_SERVER", "host")
    monkeypatch.setenv("MCP_SQL_TRUSTED", "yes")
    monkeypatch.setenv("MCP_SQL_EXTRA", "ApplicationIntent=ReadOnly")
    assert "ApplicationIntent=ReadOnly" in db._connection_string()
