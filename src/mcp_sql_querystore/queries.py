"""Fixed T-SQL for each diagnostic tool.

All statements are SELECT-only and parameterized. Metric column names are chosen
from a fixed whitelist in the caller — never interpolated from user input.

Query Store column availability assumed: SQL Server 2019+/2022 and Azure SQL MI.
On 2016/2017 some columns (e.g. avg_query_max_used_memory) differ; adjust if you
target those.
"""

from __future__ import annotations

# Whitelist mapping the public "metric" enum to the Query Store column.
# The value is substituted into SQL, so it MUST come from this dict only.
METRIC_COLUMNS: dict[str, str] = {
    "cpu_time": "avg_cpu_time",
    "duration": "avg_duration",
    "logical_reads": "avg_logical_io_reads",
}


def regressed_queries_sql(metric_column: str) -> str:
    """Baseline-vs-recent regression detection.

    Splits the lookback window into a baseline period (older) and a recent period
    (newer), aggregates the chosen metric per query over each, and returns queries
    whose recent metric is materially worse than baseline.

    metric_column MUST be a value from METRIC_COLUMNS (caller enforces this).
    All other values are bound positionally via pyodbc `?` placeholders.

    Bound parameter order (see server._get_regressed_queries):
        1  recent_hours     -> recent window boundary
        2  recent_hours     -> (reused) baseline window start
        3  baseline_hours   -> (reused) baseline window start
        4  min_executions   -> baseline exec floor
        5  min_executions   -> recent exec floor
        6  regression_threshold
        7  top_n
    """
    return f"""
SET NOCOUNT ON;

DECLARE @now DATETIMEOFFSET = SYSUTCDATETIME();
DECLARE @recent_start DATETIMEOFFSET = DATEADD(HOUR, -CAST(? AS INT), @now);
DECLARE @baseline_start DATETIMEOFFSET =
    DATEADD(HOUR, -(CAST(? AS INT) + CAST(? AS INT)), @now);

WITH stats AS (
    SELECT
        q.query_id,
        rs.plan_id,
        rsi.start_time,
        rs.count_executions,
        rs.{metric_column} AS metric_val
    FROM sys.query_store_runtime_stats rs
    JOIN sys.query_store_runtime_stats_interval rsi
        ON rs.runtime_stats_interval_id = rsi.runtime_stats_interval_id
    JOIN sys.query_store_plan p
        ON rs.plan_id = p.plan_id
    JOIN sys.query_store_query q
        ON p.query_id = q.query_id
    WHERE rsi.start_time >= @baseline_start
),
baseline AS (
    SELECT
        query_id,
        -- execution-weighted average over the baseline period
        SUM(metric_val * count_executions) / NULLIF(SUM(count_executions), 0)
            AS base_metric,
        SUM(count_executions) AS base_execs
    FROM stats
    WHERE start_time <  @recent_start
    GROUP BY query_id
),
recent AS (
    SELECT
        query_id,
        SUM(metric_val * count_executions) / NULLIF(SUM(count_executions), 0)
            AS recent_metric,
        SUM(count_executions) AS recent_execs
    FROM stats
    WHERE start_time >= @recent_start
    GROUP BY query_id
)
SELECT TOP (CAST(? AS INT))
    r.query_id,
    CAST(qt.query_sql_text AS NVARCHAR(1000)) AS query_text_sample,
    b.base_metric,
    r.recent_metric,
    CASE WHEN b.base_metric > 0
         THEN (r.recent_metric - b.base_metric) / b.base_metric
         ELSE NULL END AS pct_change,
    b.base_execs,
    r.recent_execs
FROM recent r
JOIN baseline b ON r.query_id = b.query_id
JOIN sys.query_store_query q ON q.query_id = r.query_id
JOIN sys.query_store_query_text qt ON q.query_text_id = qt.query_text_id
WHERE b.base_metric > 0
  AND b.base_execs   >= ?
  AND r.recent_execs >= ?
  -- regression threshold: recent is at least X% worse than baseline
  AND (r.recent_metric - b.base_metric) / b.base_metric >= ?
ORDER BY (r.recent_metric - b.base_metric) / b.base_metric DESC;
"""


# get_query_execution_plan: returns the plan XML and the compiled plans for a
# query_id. Missing-index / warning parsing is done in Python from the XML.
EXECUTION_PLAN_SQL = """
SET NOCOUNT ON;

SELECT
    p.plan_id,
    p.query_id,
    p.query_plan,                         -- XML showplan
    p.is_forced_plan,
    p.count_compiles,
    -- pyodbc cannot convert datetimeoffset (SQL type -155); return as ISO string.
    CONVERT(NVARCHAR(34), p.last_compile_start_time, 127) AS last_compile_start_time,
    CAST(qt.query_sql_text AS NVARCHAR(MAX)) AS query_sql_text
FROM sys.query_store_plan p
JOIN sys.query_store_query q ON p.query_id = q.query_id
JOIN sys.query_store_query_text qt ON q.query_text_id = qt.query_text_id
WHERE p.query_id = ?
ORDER BY p.last_compile_start_time DESC;
"""

# analyze_parameter_sniffing: find query_ids that have multiple plans AND high
# runtime variance across those plans — the classic sniffing signature (one query,
# several plans, wildly different durations). We use the stdev columns Query Store
# already stores, so no XML parsing is needed here.
#
# Bound parameter order (text order):
#   1: min_plan_count   (HAVING COUNT(DISTINCT plan_id) >= ?)
#   2: top_n            (SELECT TOP)
PARAMETER_SNIFFING_SQL = """
SET NOCOUNT ON;

WITH plan_stats AS (
    SELECT
        q.query_id,
        rs.plan_id,
        SUM(rs.count_executions) AS execs,
        -- execution-weighted mean duration for this plan
        SUM(rs.avg_duration * rs.count_executions)
            / NULLIF(SUM(rs.count_executions), 0) AS mean_duration,
        -- max stdev observed for this plan (per-interval stdev, take the worst)
        MAX(rs.stdev_duration) AS max_stdev_duration
    FROM sys.query_store_runtime_stats rs
    JOIN sys.query_store_plan p ON rs.plan_id = p.plan_id
    JOIN sys.query_store_query q ON p.query_id = q.query_id
    GROUP BY q.query_id, rs.plan_id
),
agg AS (
    SELECT
        query_id,
        COUNT(DISTINCT plan_id) AS plan_count,
        SUM(execs)              AS total_execs,
        MIN(mean_duration)      AS min_plan_mean_duration,
        MAX(mean_duration)      AS max_plan_mean_duration,
        MAX(max_stdev_duration) AS worst_stdev_duration
    FROM plan_stats
    GROUP BY query_id
    HAVING COUNT(DISTINCT plan_id) >= ?
)
SELECT TOP (CAST(? AS INT))
    a.query_id,
    a.plan_count,
    a.total_execs,
    a.min_plan_mean_duration,
    a.max_plan_mean_duration,
    -- ratio of the slowest plan's mean to the fastest: bigger = more suspicious
    CASE WHEN a.min_plan_mean_duration > 0
         THEN a.max_plan_mean_duration / a.min_plan_mean_duration
         ELSE NULL END AS duration_ratio,
    a.worst_stdev_duration,
    CAST(qt.query_sql_text AS NVARCHAR(1000)) AS query_text_sample
FROM agg a
JOIN sys.query_store_query q ON q.query_id = a.query_id
JOIN sys.query_store_query_text qt ON q.query_text_id = qt.query_text_id
ORDER BY duration_ratio DESC;
"""


# get_missing_index_impact: the missing-index impact score lives inside the plan
# XML, not in any column. So we fetch the most recent plans (bounded) and let the
# Python side parse + aggregate + rank. This query just returns candidate plans.
#
# Bound parameter order (text order):
#   1: plan_scan_limit  (SELECT TOP — how many recent plans to scan for indexes)
MISSING_INDEX_PLANS_SQL = """
SET NOCOUNT ON;

SELECT TOP (CAST(? AS INT))
    p.plan_id,
    p.query_id,
    p.query_plan,
    CAST(qt.query_sql_text AS NVARCHAR(1000)) AS query_text_sample
FROM sys.query_store_plan p
JOIN sys.query_store_query q ON p.query_id = q.query_id
JOIN sys.query_store_query_text qt ON q.query_text_id = qt.query_text_id
WHERE p.query_plan LIKE '%<MissingIndexes>%'
ORDER BY p.last_execution_time DESC;
"""

# get_wait_stats: aggregate query wait time by wait category over a time window.
# Answers "why is it slow" (CPU vs blocking vs IO vs memory) rather than "what is
# slow". Per MS docs, wait rows must be de-duplicated by grouping on plan_id,
# interval, execution_type and wait_category before summing, or flushed + in-memory
# rows double-count. We aggregate to the wait_category level across the window.
#
# Bound parameter order (text order):
#   1: recent_hours   (window start)
#   2: top_n          (SELECT TOP)
WAIT_STATS_SQL = """
SET NOCOUNT ON;

DECLARE @window_start DATETIMEOFFSET =
    DATEADD(HOUR, -CAST(? AS INT), SYSUTCDATETIME());

WITH deduped AS (
    -- one row per (plan, interval, execution_type, category): MS-recommended grain
    SELECT
        ws.plan_id,
        ws.runtime_stats_interval_id,
        ws.execution_type,
        ws.wait_category_desc,
        SUM(ws.total_query_wait_time_ms) AS total_wait_ms,
        MAX(ws.max_query_wait_time_ms)   AS max_wait_ms
    FROM sys.query_store_wait_stats ws
    JOIN sys.query_store_runtime_stats_interval rsi
        ON ws.runtime_stats_interval_id = rsi.runtime_stats_interval_id
    WHERE rsi.start_time >= @window_start
    GROUP BY ws.plan_id, ws.runtime_stats_interval_id,
             ws.execution_type, ws.wait_category_desc
)
SELECT TOP (CAST(? AS INT))
    wait_category_desc,
    SUM(total_wait_ms)              AS total_wait_ms,
    MAX(max_wait_ms)               AS max_wait_ms,
    COUNT(DISTINCT plan_id)        AS distinct_plans
FROM deduped
GROUP BY wait_category_desc
ORDER BY SUM(total_wait_ms) DESC;
"""

# list_querystore_databases: enumerate online databases with Query Store actually
# ON, so the sweep skips DBs that would error or return nothing. Runs in the
# server context (master); no USE needed. sys.databases is server-scoped.
LIST_QS_DATABASES_SQL = """
SET NOCOUNT ON;

SELECT name
FROM sys.databases
WHERE state_desc = 'ONLINE'
  AND database_id > 4            -- skip system DBs
  AND is_query_store_on = 1
ORDER BY name;
"""
