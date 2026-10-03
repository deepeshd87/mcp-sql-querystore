"""MCP server exposing read-only Query Store diagnostics.

Run with:  python -m mcp_sql_querystore.server
Requires:  MCP_SQL_CONNECTION_STRING set to a dedicated read-only login.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import queries
from .db import run_query
from .plan_parser import summarize_plan

app: Server = Server("mcp-sql-querystore")


# --- Tool catalog -------------------------------------------------------------


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_regressed_queries",
            description=(
                "Detect queries whose performance regressed by comparing a recent "
                "period against an earlier baseline period from Query Store."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "database_name": {
                        "type": "string",
                        "description": "Target SQL Server database name.",
                    },
                    "metric": {
                        "type": "string",
                        "enum": list(queries.METRIC_COLUMNS.keys()),
                        "default": "cpu_time",
                        "description": "Metric to evaluate regression against.",
                    },
                    "recent_hours": {
                        "type": "integer",
                        "default": 24,
                        "description": "Length of the recent window, in hours.",
                    },
                    "baseline_hours": {
                        "type": "integer",
                        "default": 168,
                        "description": "Length of the baseline window preceding the "
                        "recent window, in hours (default 7 days).",
                    },
                    "regression_threshold": {
                        "type": "number",
                        "default": 0.5,
                        "description": "Minimum fractional worsening to flag "
                        "(0.5 = 50% worse than baseline).",
                    },
                    "min_executions": {
                        "type": "integer",
                        "default": 5,
                        "description": "Ignore queries with fewer executions than "
                        "this in either period (filters noise).",
                    },
                    "top_n": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max rows to return.",
                    },
                },
                "required": ["database_name"],
            },
        ),
        types.Tool(
            name="get_query_execution_plan",
            description=(
                "Fetch execution plan(s) for a Query Store query_id and return a "
                "compact summary (missing indexes, warnings, lookups) plus optional "
                "raw XML."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "database_name": {
                        "type": "string",
                        "description": "Target SQL Server database name.",
                    },
                    "query_id": {
                        "type": "integer",
                        "description": "Query Store Query ID.",
                    },
                    "include_xml": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include raw showplan XML alongside summary.",
                    },
                },
                "required": ["database_name", "query_id"],
            },
        ),
        types.Tool(
            name="analyze_parameter_sniffing",
            description=(
                "Detect queries whose runtime varies widely across multiple compiled "
                "plans — the classic parameter-sniffing signature. Ranks by the ratio "
                "of slowest to fastest plan mean duration."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "database_name": {
                        "type": "string",
                        "description": "Target SQL Server database name.",
                    },
                    "min_plan_count": {
                        "type": "integer",
                        "default": 2,
                        "description": "Minimum distinct plans for a query to be "
                        "considered (2 = at least two plans).",
                    },
                    "top_n": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max rows to return.",
                    },
                },
                "required": ["database_name"],
            },
        ),
        types.Tool(
            name="get_missing_index_impact",
            description=(
                "Aggregate missing-index recommendations found in Query Store plans, "
                "ranked by the optimizer's estimated impact score. Groups duplicate "
                "recommendations across queries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "database_name": {
                        "type": "string",
                        "description": "Target SQL Server database name.",
                    },
                    "top_n": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max index recommendations to return.",
                    },
                    "plan_scan_limit": {
                        "type": "integer",
                        "default": 200,
                        "description": "How many recent plans (that contain missing "
                        "indexes) to scan and aggregate. Higher = more thorough, slower.",
                    },
                },
                "required": ["database_name"],
            },
        ),
        types.Tool(
            name="get_wait_stats",
            description=(
                "Aggregate query wait time by wait category over a time window — shows "
                "WHY queries are slow (CPU, blocking/locks, IO, memory, etc.) rather "
                "than which are slow. Ranked by total wait time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "database_name": {
                        "type": "string",
                        "description": "Target SQL Server database name.",
                    },
                    "recent_hours": {
                        "type": "integer",
                        "default": 24,
                        "description": "Lookback window in hours.",
                    },
                    "top_n": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max wait categories to return.",
                    },
                },
                "required": ["database_name"],
            },
        ),
        types.Tool(
            name="sweep_regressions",
            description=(
                "Run regression detection across ALL online databases that have Query "
                "Store enabled, and return the worst regressions found per database. "
                "Use this to triage a whole instance instead of one database at a time."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": list(queries.METRIC_COLUMNS.keys()),
                        "default": "cpu_time",
                        "description": "Metric to evaluate regression against.",
                    },
                    "recent_hours": {
                        "type": "integer",
                        "default": 24,
                        "description": "Length of the recent window, in hours.",
                    },
                    "baseline_hours": {
                        "type": "integer",
                        "default": 168,
                        "description": "Length of the baseline window, in hours.",
                    },
                    "regression_threshold": {
                        "type": "number",
                        "default": 0.5,
                        "description": "Minimum fractional worsening to flag.",
                    },
                    "min_executions": {
                        "type": "integer",
                        "default": 5,
                        "description": "Ignore queries below this execution count.",
                    },
                    "top_n_per_db": {
                        "type": "integer",
                        "default": 3,
                        "description": "Max regressed queries to return per database.",
                    },
                    "database_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional explicit list of databases to sweep. "
                        "If omitted, sweeps all Query Store-enabled online databases.",
                    },
                },
                "required": [],
            },
        ),
    ]


# --- Tool dispatch ------------------------------------------------------------


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        if name == "get_regressed_queries":
            result = _get_regressed_queries(arguments)
        elif name == "get_query_execution_plan":
            result = _get_query_execution_plan(arguments)
        elif name == "analyze_parameter_sniffing":
            result = _analyze_parameter_sniffing(arguments)
        elif name == "get_missing_index_impact":
            result = _get_missing_index_impact(arguments)
        elif name == "get_wait_stats":
            result = _get_wait_stats(arguments)
        elif name == "sweep_regressions":
            result = _sweep_regressions(arguments)
        else:
            raise ValueError(f"Unknown tool: {name}")
    except Exception as exc:  # surface a clean error to the agent
        result = {"error": type(exc).__name__, "message": str(exc)}

    return [types.TextContent(type="text", text=json.dumps(result, default=str, indent=2))]


def _run_regression(
    database: str,
    metric: str,
    recent_hours: int,
    baseline_hours: int,
    min_executions: int,
    threshold: float,
    top_n: int,
    tool: str = "get_regressed_queries",
) -> list[dict[str, Any]]:
    """Core regression query for one database. Single source of truth for the
    parameter binding order, reused by both the single-DB tool and the sweep."""
    if metric not in queries.METRIC_COLUMNS:
        raise ValueError(f"Unsupported metric: {metric}")
    metric_column = queries.METRIC_COLUMNS[metric]  # whitelisted, safe to interpolate
    sql = queries.regressed_queries_sql(metric_column)

    # Params bound in the ORDER THE ? PLACEHOLDERS APPEAR in the SQL text:
    #   1: recent_start DATEADD           (recent_hours)
    #   2: baseline_start recent portion  (recent_hours)
    #   3: baseline_start baseline portion(baseline_hours)
    #   4: SELECT TOP                     (top_n)
    #   5: baseline exec floor            (min_executions)
    #   6: recent exec floor              (min_executions)
    #   7: regression threshold           (threshold)
    params = (
        recent_hours,
        recent_hours,
        baseline_hours,
        top_n,
        min_executions,
        min_executions,
        threshold,
    )
    return run_query(sql, params, database=database, tool=tool)


def _get_regressed_queries(args: dict[str, Any]) -> dict[str, Any]:
    metric = args.get("metric", "cpu_time")
    rows = _run_regression(
        database=args["database_name"],
        metric=metric,
        recent_hours=int(args.get("recent_hours", 24)),
        baseline_hours=int(args.get("baseline_hours", 168)),
        min_executions=int(args.get("min_executions", 5)),
        threshold=float(args.get("regression_threshold", 0.5)),
        top_n=int(args.get("top_n", 10)),
    )
    return {"metric": metric, "count": len(rows), "regressed_queries": rows}


def _get_query_execution_plan(args: dict[str, Any]) -> dict[str, Any]:
    rows = run_query(
        queries.EXECUTION_PLAN_SQL,
        (int(args["query_id"]),),
        database=args["database_name"],
        tool="get_query_execution_plan",
    )
    include_xml = bool(args.get("include_xml", False))
    plans = []
    for row in rows:
        xml = row.get("query_plan") or ""
        entry: dict[str, Any] = {
            "plan_id": row.get("plan_id"),
            "is_forced_plan": row.get("is_forced_plan"),
            "count_compiles": row.get("count_compiles"),
            "summary": summarize_plan(xml) if xml else {"error": "no plan xml"},
        }
        if include_xml:
            entry["query_plan_xml"] = xml
        plans.append(entry)
    return {
        "query_id": args["query_id"],
        "plan_count": len(plans),
        "plans": plans,
    }


def _analyze_parameter_sniffing(args: dict[str, Any]) -> dict[str, Any]:
    min_plan_count = int(args.get("min_plan_count", 2))
    top_n = int(args.get("top_n", 10))
    # Bound params in text order: min_plan_count (HAVING), then top_n (TOP).
    rows = run_query(
        queries.PARAMETER_SNIFFING_SQL,
        (min_plan_count, top_n),
        database=args["database_name"],
        tool="analyze_parameter_sniffing",
    )
    return {"count": len(rows), "suspected_sniffing": rows}


def _get_missing_index_impact(args: dict[str, Any]) -> dict[str, Any]:
    top_n = int(args.get("top_n", 10))
    plan_scan_limit = int(args.get("plan_scan_limit", 200))
    rows = run_query(
        queries.MISSING_INDEX_PLANS_SQL,
        (plan_scan_limit,),
        database=args["database_name"],
        tool="get_missing_index_impact",
    )

    # Aggregate missing-index recommendations across the scanned plans. The impact
    # score and column set come from the plan XML (parsed by summarize_plan). We key
    # duplicates by (table, equality, inequality, included) so the same suggested
    # index across many queries is combined rather than listed repeatedly.
    aggregated: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        xml = row.get("query_plan") or ""
        if not xml:
            continue
        for mi in summarize_plan(xml).get("missing_indexes", []):
            cols = mi.get("columns", {})
            key = (
                mi.get("table"),
                tuple(cols.get("equality", [])),
                tuple(cols.get("inequality", [])),
                tuple(cols.get("included", [])),
            )
            impact = mi.get("impact_pct") or 0.0
            if key not in aggregated:
                aggregated[key] = {
                    "table": mi.get("table"),
                    "schema": mi.get("schema"),
                    "database": mi.get("database"),
                    "equality_columns": cols.get("equality", []),
                    "inequality_columns": cols.get("inequality", []),
                    "included_columns": cols.get("included", []),
                    "max_impact_pct": impact,
                    "occurrences": 0,
                }
            entry = aggregated[key]
            entry["occurrences"] += 1
            entry["max_impact_pct"] = max(entry["max_impact_pct"], impact)

    # Rank by impact first, then by how often the recommendation recurs.
    ranked = sorted(
        aggregated.values(),
        key=lambda e: (e["max_impact_pct"], e["occurrences"]),
        reverse=True,
    )[:top_n]
    return {
        "plans_scanned": len(rows),
        "distinct_recommendations": len(aggregated),
        "recommendations": ranked,
    }


def _get_wait_stats(args: dict[str, Any]) -> dict[str, Any]:
    recent_hours = int(args.get("recent_hours", 24))
    top_n = int(args.get("top_n", 10))
    # Bound params in text order: recent_hours (window), then top_n (TOP).
    rows = run_query(
        queries.WAIT_STATS_SQL,
        (recent_hours, top_n),
        database=args["database_name"],
        tool="get_wait_stats",
    )
    return {"recent_hours": recent_hours, "count": len(rows), "wait_categories": rows}


def _sweep_regressions(args: dict[str, Any]) -> dict[str, Any]:
    metric = args.get("metric", "cpu_time")
    recent_hours = int(args.get("recent_hours", 24))
    baseline_hours = int(args.get("baseline_hours", 168))
    min_executions = int(args.get("min_executions", 5))
    threshold = float(args.get("regression_threshold", 0.5))
    top_n_per_db = int(args.get("top_n_per_db", 3))

    # Determine the database set: explicit list, or auto-enumerate QS-enabled DBs.
    explicit = args.get("database_names")
    if explicit:
        databases = [str(d) for d in explicit]
    else:
        # is_query_store_on in sys.databases is a cached bit and can be stale after
        # AG/mirroring failover, so a DB may pass this filter yet error on query —
        # that per-DB error is caught below and reported, not fatal to the sweep.
        db_rows = run_query(queries.LIST_QS_DATABASES_SQL, tool="sweep_regressions")  # server context
        databases = [r["name"] for r in db_rows]

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for db in databases:
        try:
            rows = _run_regression(
                database=db,
                metric=metric,
                tool="sweep_regressions",
                recent_hours=recent_hours,
                baseline_hours=baseline_hours,
                min_executions=min_executions,
                threshold=threshold,
                top_n=top_n_per_db,
            )
            if rows:  # only report databases that actually have regressions
                results.append({"database": db, "regression_count": len(rows),
                                "top_regressions": rows})
        except Exception as exc:
            # One bad database must not sink the whole sweep.
            errors.append({"database": db, "error": type(exc).__name__,
                           "message": str(exc)})

    # Databases with the worst single regression first.
    results.sort(
        key=lambda r: max((abs(q.get("pct_change") or 0) for q in r["top_regressions"]),
                          default=0),
        reverse=True,
    )
    return {
        "metric": metric,
        "databases_scanned": len(databases),
        "databases_with_regressions": len(results),
        "results": results,
        "errors": errors,
    }


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def run() -> None:
    """Synchronous entry point for the console script. The console script in
    pyproject.toml must point here, not at the async `main`, or invoking the
    command just creates a coroutine and never awaits it."""
    import asyncio

    asyncio.run(main())


if __name__ == "__main__":
    run()
