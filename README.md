# mcp-sql-querystore

Read-only MCP server exposing SQL Server Query Store diagnostics to LLM agents.

Six read-only diagnostic tools over Query Store, DMVs, and execution plans. The
tools have been validated against a live SQL Server instance and are covered by a
unit + integration test suite. Still validate against a non-prod instance of your
own before pointing it at production, especially on SQL Server versions other than
those noted under caveats.

## Quickstart

1. **Provision a read-only login.** Run `provisioning/create_readonly_login.sql`
   against your instance (edit names first). This login's permissions are the
   read-only guarantee — see the security model below.
2. **Install.** `pip install -e .` in a virtual environment. ODBC Driver 18 for
   SQL Server must be installed on the host.
3. **Store the password outside the repo.** Put it in a plain-text file somewhere
   the repo can't reach (not under the project folder):

       # Windows PowerShell, UTF-8, password only, no quotes/newline
       New-Item -ItemType Directory -Force C:\Users\you\secrets | Out-Null
       Set-Content -NoNewline -Encoding utf8 C:\Users\you\secrets\mcp_sql.pwd 'your-password'

   Or skip the password entirely with integrated auth (`MCP_SQL_TRUSTED=yes`) —
   preferred for CJIS/PCI. See **Secret handling** below for all options.
4. **Configure your MCP client.** Copy the `sql-querystore` block from
   `claude_desktop_config.example.json` into your real Claude Desktop config
   (Windows: `%APPDATA%\Claude\claude_desktop_config.json`), then replace the
   placeholder paths, server name, and `MCP_SQL_PWD_FILE`. Set
   `MCP_SQL_TRUST_CERT=yes` only for a self-signed/local cert; leave it `no`
   against instances with proper certificates.
5. **Restart your MCP client** and confirm the server shows as running.

Never commit your real config or your password file. `.gitignore` already
excludes `*.pwd`, `.env`, and `claude_desktop_config.json`.

## Security model (read this first)

The read-only guarantee comes from **the SQL login's permissions**, not from any
code in this repo:

- Provision a dedicated login with `VIEW DATABASE STATE` (and `VIEW SERVER STATE`
  only if you use server-scoped DMVs) and **nothing else** — no `db_datareader`,
  no `SELECT` on user tables. See `provisioning/create_readonly_login.sql`.
- The keyword screen in `db.py` and the fixed SELECT-only query text are
  **defense-in-depth**, not the primary control.
- `ApplicationIntent=ReadOnly` in the connection string only routes to a readable
  secondary in an availability group. On a standalone instance it does not make
  the session read-only. Do not rely on it for safety.
- Credentials never belong in code. The simplest setup uses the
  `MCP_SQL_CONNECTION_STRING` env var, but for CJIS/PCI environments prefer
  integrated auth or a file/secret-store-sourced password — see the
  **Secret handling** section below.
- Every query is recorded via the audit logger — see **Audit logging** below. In
  a regulated environment, route that logger to a durable file or SIEM.

## Setup

ODBC Driver 18 for SQL Server must be installed on the host. Install the package,
then configure the connection via environment variables (see **Secret handling**
for all options). The recommended form keeps the password in a file, not inline:

```powershell
pip install -e .

# PowerShell — connection assembled from parts, password read from a file
$env:MCP_SQL_SERVER      = "yourhost\INSTANCE"
$env:MCP_SQL_DATABASE    = "master"
$env:MCP_SQL_UID         = "mcp_readonly"
$env:MCP_SQL_PWD_FILE    = "C:\path\to\your\secret.pwd"
$env:MCP_SQL_TRUST_CERT  = "no"   # "yes" only for a self-signed/local cert

python -m mcp_sql_querystore.server
```

Or use integrated auth with no stored password at all (`MCP_SQL_TRUSTED=yes`).
A full `MCP_SQL_CONNECTION_STRING` is also accepted for simple cases — see
**Secret handling**.

Register it with your MCP client (e.g. Claude Desktop) as an stdio server
invoking `python -m mcp_sql_querystore.server`; see
`claude_desktop_config.example.json`.

## Tools

All tools are read-only and take a `database_name` (except `sweep_regressions`,
which can sweep all databases). Each returns JSON, or a structured error dict on
failure rather than raising.

- **get_regressed_queries** — compares a recent window against an earlier
  baseline window per query and flags those worse by at least
  `regression_threshold`. A real baseline-vs-recent comparison, not a top-CPU list.
- **get_query_execution_plan** — returns compiled plans for a `query_id` with a
  compact JSON summary (missing indexes, warnings incl. implicit conversions,
  key lookups) and optional raw XML.
- **analyze_parameter_sniffing** — finds queries with multiple compiled plans and
  ranks them by the ratio of slowest to fastest plan mean duration — the classic
  parameter-sniffing signature.
- **get_missing_index_impact** — scans recent plans containing missing-index
  recommendations, parses the impact score from the plan XML, and aggregates
  duplicate recommendations across queries, ranked by impact then recurrence.
- **get_wait_stats** — aggregates query wait time by wait category over a window,
  showing *why* queries are slow (CPU, blocking/locks, IO, memory) rather than
  which. De-duplicates flushed vs in-memory rows per Microsoft guidance.
- **sweep_regressions** — runs regression detection across all Query Store-enabled
  online databases (or an explicit `database_names` list) and returns the worst
  per database, ranked. One failing database does not abort the sweep; its error
  is collected and reported.

## Example prompts

Once the server is connected to your MCP client, you drive the tools in plain
language. Name the target database in the prompt (except `sweep_regressions`,
which can scan all of them). Replace `YourDB` with your database name.

**Wait stats — why queries are slow**
- "What are the top wait categories in YourDB over the last week?"
- "Is YourDB waiting on CPU, memory, or IO?"
- "Show me wait stats for YourDB over the last 24 hours."

**Execution plans**
- "Get the execution plan for query_id 10 in YourDB and summarize it."
- "Does query_id 13 in YourDB have missing index recommendations?"
- "Are there implicit conversion warnings in query 12's plan in YourDB?"

**Regression analysis**
- "Check YourDB for CPU regressions over the last 24 hours."
- "Which queries in YourDB regressed by more than 30%?"
- "Find duration regressions in YourDB, ignoring anything with fewer than 10 executions."

**Parameter sniffing**
- "Check YourDB for parameter sniffing."
- "Which queries in YourDB have unstable plans?"

**Missing indexes**
- "What missing indexes does YourDB need most?"
- "Show me the top 10 index recommendations for YourDB by impact."

**Multi-database sweep (no database name needed)**
- "Sweep all my databases for CPU regressions."
- "Which database has the worst regressions this week?"

**Combined — chaining tools in one turn**
- "Find the biggest CPU regression in YourDB, pull its plan, and tell me why it might have regressed."
- "YourDB feels slow — diagnose it." (wait stats → regressions → plans)
- "Full performance triage of YourDB: wait stats, top regressions, and missing indexes."

## Known caveats / TODO

- **Version differences.** Query Store column names assume SQL Server 2019+/2022
  and Azure SQL MI. Verify against 2016/2017 if you target those.
- **Regression semantics.** Current logic uses execution-weighted averages. You
  may prefer percentile-based comparison (Query Store doesn't store percentiles
  directly, so that needs `*_stdev` columns and assumptions).
- **Not time-windowed:** `analyze_parameter_sniffing` aggregates across all Query
  Store history; on busy databases consider adding a `recent_hours` filter like
  the other tools have.
- **Remaining hardening:** connection retry with backoff, and version-aware column
  handling for mixed 2016/2017/2019/2022 fleets.

## Secret handling

The connection string is resolved in this order, so the password need not sit in
plaintext config:

1. `MCP_SQL_CONNECTION_STRING` — the full string (simplest; back-compat).
2. `MCP_SQL_CONNECTION_STRING_FILE` — path to a file holding the full string
   (Docker/K8s secret-mount style).
3. Assembled from parts: `MCP_SQL_SERVER` (+ `MCP_SQL_DATABASE`, `MCP_SQL_DRIVER`,
   `MCP_SQL_ENCRYPT`, `MCP_SQL_TRUST_CERT`, `MCP_SQL_EXTRA`). Auth is either:
   - **Integrated** (preferred for CJIS/PCI — no password stored): `MCP_SQL_TRUSTED=yes`.
   - **SQL auth**: `MCP_SQL_UID` plus the password from `MCP_SQL_PWD_FILE` (a
     vault-mounted file), `MCP_SQL_PWD_ENV` (name of another env var), or
     `MCP_SQL_PWD` (direct; least preferred).

Timeouts: `MCP_SQL_CONNECT_TIMEOUT` (default 10s) and `MCP_SQL_QUERY_TIMEOUT`
(default 30s, 0 disables).

## Audit logging

Every query attempt is logged via the `mcp_sql_querystore.audit` logger: tool,
database, a 12-char hash of the SQL (not the text), row count, elapsed ms, and
outcome. Connection strings, SQL text, and parameter values are never logged.
Configure a handler for that logger to route the audit trail to a file or SIEM.

## Testing

Unit tests (no database, safe in CI):

    pip install -e ".[test]"
    pytest

Integration tests (real instance, opt-in):

    # set a working connection (any form above), then:
    $env:MCP_SQL_TEST_DATABASE = "RAG"
    $env:MCP_SQL_RUN_INTEGRATION = "1"
    pytest tests/test_integration.py -v
