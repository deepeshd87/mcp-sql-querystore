"""Database connection layer.

The read-only guarantee comes from the SQL login's permissions, NOT from this
code. Provision a dedicated login with only:
    GRANT VIEW DATABASE STATE   -- (or VIEW SERVER STATE for server-wide DMVs)
    GRANT SELECT ON the sys.query_store_* catalog views
and NO db_datareader / no SELECT on user tables. See README.

This module adds parameterization and a keyword screen as defense-in-depth only.

Secret handling: the connection string can be supplied whole, or assembled from
parts with the password sourced indirectly (env var or file), so the password
need not sit in a plaintext MCP config. See _connection_string() for the order.

Audit logging: every query attempt is recorded via the "mcp_sql_querystore.audit"
logger — tool, database, a hash of the SQL (not the text), row count, duration,
and outcome. Credentials and parameter values are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Iterator

# pyodbc is imported lazily inside get_cursor() rather than at module load. This
# keeps the pure validators (validate_db_name, assert_select_only) importable in
# environments without the ODBC driver — e.g. CI running the test suite — while
# still requiring pyodbc the moment an actual connection is attempted.

audit_log = logging.getLogger("mcp_sql_querystore.audit")


# --- Configuration & secret handling ------------------------------------------
#
# Resolution order for the connection string:
#   1. MCP_SQL_CONNECTION_STRING           — full string (back-compat; simplest)
#   2. MCP_SQL_CONNECTION_STRING_FILE      — path to a file containing the full
#                                            string (Docker/K8s secret style)
#   3. assembled from parts:
#        MCP_SQL_SERVER, MCP_SQL_DATABASE (default "master"),
#        MCP_SQL_DRIVER (default "ODBC Driver 18 for SQL Server"),
#        MCP_SQL_ENCRYPT (default "yes"), MCP_SQL_TRUST_CERT (default "no"),
#        MCP_SQL_EXTRA (optional, appended verbatim, e.g. ApplicationIntent=ReadOnly)
#      Auth for the assembled form:
#        - Integrated (Windows/AAD): set MCP_SQL_TRUSTED=yes and omit UID/PWD.
#          Preferred for CJIS/PCI — no password to store at all.
#        - SQL auth: MCP_SQL_UID plus the password from ONE of:
#            MCP_SQL_PWD_FILE   — path to a file holding just the password
#                                 (secret-store / vault-mounted file)
#            MCP_SQL_PWD_ENV    — the NAME of another env var holding the password
#            MCP_SQL_PWD        — the password directly (least preferred)

_FULL_ENV = "MCP_SQL_CONNECTION_STRING"
_FULL_FILE_ENV = "MCP_SQL_CONNECTION_STRING_FILE"


def _read_secret_file(path: str) -> str:
    # utf-8-sig transparently strips a UTF-8/UTF-16 BOM if present (e.g. files
    # written by PowerShell Out-File or some secret mounts) and reads plain
    # UTF-8 otherwise.
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except UnicodeDecodeError:
        # Fall back for UTF-16 without/with BOM that utf-8-sig can't handle.
        try:
            with open(path, encoding="utf-16") as fh:
                return fh.read().strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"Secret file {path!r} is not UTF-8 or UTF-16 text: {exc}"
            ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not read secret file {path!r}: {exc}") from exc


def _resolve_password() -> str:
    """Resolve the SQL password from a file, a named env var, or directly —
    in that order of preference. Returns '' if none set (caller decides if ok)."""
    pwd_file = os.environ.get("MCP_SQL_PWD_FILE")
    if pwd_file:
        return _read_secret_file(pwd_file)
    pwd_env = os.environ.get("MCP_SQL_PWD_ENV")
    if pwd_env:
        val = os.environ.get(pwd_env)
        if val is None:
            raise RuntimeError(
                f"MCP_SQL_PWD_ENV points to {pwd_env!r} but that variable is not set."
            )
        return val
    return os.environ.get("MCP_SQL_PWD", "")


def _assemble_connection_string() -> str:
    server = os.environ.get("MCP_SQL_SERVER")
    if not server:
        raise RuntimeError(
            "No connection configured. Set MCP_SQL_CONNECTION_STRING, or "
            "MCP_SQL_CONNECTION_STRING_FILE, or MCP_SQL_SERVER (+ auth parts)."
        )
    driver = os.environ.get("MCP_SQL_DRIVER", "ODBC Driver 18 for SQL Server")
    database = os.environ.get("MCP_SQL_DATABASE", "master")
    encrypt = os.environ.get("MCP_SQL_ENCRYPT", "yes")
    trust_cert = os.environ.get("MCP_SQL_TRUST_CERT", "no")

    parts = [
        f"Driver={{{driver}}}",
        f"Server={server}",
        f"Database={database}",
        f"Encrypt={encrypt}",
        f"TrustServerCertificate={trust_cert}",
    ]

    if os.environ.get("MCP_SQL_TRUSTED", "").lower() in ("1", "yes", "true"):
        parts.append("Trusted_Connection=yes")
    else:
        uid = os.environ.get("MCP_SQL_UID")
        if not uid:
            raise RuntimeError(
                "SQL auth selected but MCP_SQL_UID is not set (and MCP_SQL_TRUSTED "
                "is not enabled for integrated auth)."
            )
        pwd = _resolve_password()
        if not pwd:
            raise RuntimeError(
                "No password resolved. Set MCP_SQL_PWD_FILE, MCP_SQL_PWD_ENV, or "
                "MCP_SQL_PWD — or use integrated auth via MCP_SQL_TRUSTED=yes."
            )
        parts.append(f"UID={uid}")
        parts.append(f"PWD={pwd}")

    extra = os.environ.get("MCP_SQL_EXTRA")
    if extra:
        parts.append(extra)
    return ";".join(parts) + ";"


def _connection_string() -> str:
    full = os.environ.get(_FULL_ENV)
    if full:
        return full
    full_file = os.environ.get(_FULL_FILE_ENV)
    if full_file:
        return _read_secret_file(full_file)
    return _assemble_connection_string()


# --- Defense-in-depth identifier + statement screening ------------------------

# Database names are validated against this before being used to switch context.
# SQL Server identifiers are broad, but we deliberately restrict to a safe subset
# rather than trying to fully escape arbitrary names.
_SAFE_DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]{0,127}$")


def validate_db_name(name: str) -> str:
    if not isinstance(name, str) or not _SAFE_DB_NAME.match(name):
        raise ValueError(
            f"Invalid database name: {name!r}. Expected a plain SQL Server "
            "identifier (letters, digits, _ $ #; starting with a letter/underscore)."
        )
    return name


# Statements we run are all fixed SELECT text defined in this codebase. This
# screen is a tripwire in case someone adds dynamic text later.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|"
    r"GRANT|REVOKE|DENY|BACKUP|RESTORE|SHUTDOWN|RECONFIGURE)\b",
    re.IGNORECASE,
)


def assert_select_only(sql: str) -> None:
    if _FORBIDDEN.search(sql):
        raise ValueError("Refusing to run: statement contains a non-SELECT keyword.")


# --- Connection handling ------------------------------------------------------

# Per-query command timeout (seconds); 0 disables. Connect timeout is separate.
_QUERY_TIMEOUT = int(os.environ.get("MCP_SQL_QUERY_TIMEOUT", "30"))
_CONNECT_TIMEOUT = int(os.environ.get("MCP_SQL_CONNECT_TIMEOUT", "10"))


@contextmanager
def get_cursor() -> Iterator[Any]:
    import pyodbc  # lazy: only needed when actually connecting

    conn = pyodbc.connect(_connection_string(), timeout=_CONNECT_TIMEOUT)
    try:
        # Belt-and-suspenders: reject any accidental writes at the session level.
        # This does NOT replace login permissions.
        conn.autocommit = True
        conn.timeout = _QUERY_TIMEOUT  # per-command timeout
        cursor = conn.cursor()
        yield cursor
    finally:
        conn.close()


def _sql_fingerprint(sql: str) -> str:
    """Short stable hash of the SQL text, for the audit log — lets you correlate
    which fixed statement ran without recording the text itself."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]


def run_query(
    sql: str,
    params: tuple[Any, ...] = (),
    database: str | None = None,
    tool: str | None = None,
) -> list[dict[str, Any]]:
    """Run a fixed SELECT against an optional database context.

    database, if given, is validated and switched via USE with a validated
    identifier (it cannot be parameterized). params are bound positionally.
    tool, if given, is recorded in the audit log to attribute the query.
    """
    assert_select_only(sql)
    fingerprint = _sql_fingerprint(sql)
    started = time.monotonic()
    try:
        with get_cursor() as cursor:
            if database is not None:
                db = validate_db_name(database)
                # Identifier cannot be a bound parameter; db is validated above.
                cursor.execute(f"USE [{db}];")
            cursor.execute(sql, params)
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        # Log the outcome, never the SQL text, params, or connection string.
        audit_log.warning(
            "query_failed tool=%s db=%s sql=%s elapsed_ms=%s error=%s",
            tool or "-", database or "-", fingerprint, elapsed_ms,
            type(exc).__name__,
        )
        raise
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    audit_log.info(
        "query_ok tool=%s db=%s sql=%s rows=%s elapsed_ms=%s",
        tool or "-", database or "-", fingerprint, len(rows), elapsed_ms,
    )
    return rows
