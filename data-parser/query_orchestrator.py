#!/usr/bin/env python3
"""
Post-guard FahMai query orchestrator.

This CLI receives a clean/sanitized question, routes it, executes SQL/RAG tools
when requested, validates the results, and records fallback attempts. It is
JSON-first so an agent can call it directly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import query_rag
import query_react_agent
import query_router
import query_tools
import query_understanding


DEFAULT_DATABASE = Path("data-parser/output/fahmai.duckdb")
DEFAULT_CONFIG = Path(__file__).with_name("router_config.json")
DEFAULT_MODEL = "BAAI/bge-m3"
MODEL_PATH_ENV = "MODEL_PATH"
DEFAULT_LLM_API_BASE = "http://x1000c2s2b0n0:8000/v1"
DEFAULT_LLM_MODEL = "qwen-local"

DEFAULT_ROUTE_PROFILES: dict[str, dict[str, Any]] = {
    "sql_only": {
        "rag": None,
        "sql": True,
        "description": "Structured table lookup or computation.",
    },
    "rag_only": {
        "rag": {"mode": "hybrid", "staging_source": ["docs", "reports", "renders"]},
        "sql": False,
        "description": "Evidence retrieval from documents, reports, OCR, or logs.",
    },
    "rag_assisted_sql": {
        "rag": {"mode": "hybrid", "staging_source": ["tables_guide", "tables_summary", "docs"]},
        "sql": True,
        "description": "Retrieve schema/context before SQL.",
    },
    "hybrid_sql_rag": {
        "rag": {
            "mode": "hybrid",
            "staging_source": ["tables_guide", "tables_summary", "docs", "reports", "renders", "logs"],
        },
        "sql": True,
        "description": "Use both structured data and text evidence.",
    },
    "safe_refuse_or_verify": {
        "rag": {"mode": "text", "staging_source": ["docs", "reports", "renders"]},
        "sql": False,
        "description": "Verify against trusted evidence or decline.",
    },
}

FALLBACKS: dict[str, list[str]] = {
    "sql_only": ["rag_assisted_sql", "hybrid_sql_rag"],
    "rag_only": ["hybrid_sql_rag"],
    "rag_assisted_sql": ["hybrid_sql_rag"],
    "hybrid_sql_rag": [],
    "safe_refuse_or_verify": [],
}

RELATED_TABLE_RULES: list[tuple[list[str], list[str]]] = [
    (["vendor", "supplier", "shipping", "invoice", "payment"], ["DIM_VENDOR"]),
    (["sku", "product", "msrp", "สินค้า"], ["DIM_PRODUCT"]),
    (["branch", "store", "สาขา"], ["DIM_BRANCH"]),
    (["customer", "loyalty", "ลูกค้า"], ["DIM_CUSTOMER"]),
    (["campaign", "promo", "promotion", "redemption"], ["DIM_CAMPAIGN"]),
    (["employee", "staff", "พนักงาน"], ["DIM_EMPLOYEE"]),
    (["bank", "account", "settlement"], ["DIM_BANK_ACCOUNT"]),
]

TABLE_RELATED_TABLES: dict[str, list[str]] = {
    "FACT_SHIPPING": ["DIM_VENDOR"],
    "FACT_VENDOR_PAYMENT": ["DIM_VENDOR"],
    "FACT_SALES": ["DIM_PRODUCT", "DIM_BRANCH", "DIM_CUSTOMER"],
    "FACT_SALES_LINE": ["DIM_PRODUCT"],
    "FACT_PROMO_REDEMPTION": ["DIM_CAMPAIGN", "DIM_PRODUCT", "DIM_CUSTOMER"],
    "FACT_REFUND": ["DIM_PRODUCT", "DIM_BRANCH", "DIM_CUSTOMER"],
}


class OrchestratorError(Exception):
    pass


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def now_ms() -> int:
    return int(time.time() * 1000)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def split_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in value.split(","):
            item = part.strip()
            if item and item not in seen:
                out.append(item)
                seen.add(item)
    return out


def merge_route_profiles(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    configured = config.get("orchestrator", {}).get("route_profiles", {})
    profiles = json.loads(json.dumps(DEFAULT_ROUTE_PROFILES))
    for route, updates in configured.items():
        if route not in profiles or not isinstance(updates, dict):
            continue
        merged = dict(profiles[route])
        merged.update(updates)
        profiles[route] = merged
    return profiles


def load_fallbacks(config: dict[str, Any]) -> dict[str, list[str]]:
    configured = config.get("orchestrator", {}).get("fallbacks", {})
    fallbacks = json.loads(json.dumps(FALLBACKS))
    for route, values in configured.items():
        if isinstance(values, list):
            fallbacks[route] = [str(value) for value in values]
    return fallbacks


def route_profile_for_question(
    *,
    route: str,
    question: str,
    profiles: dict[str, dict[str, Any]],
    router_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    profile = json.loads(json.dumps(profiles.get(route, DEFAULT_ROUTE_PROFILES["hybrid_sql_rag"])))
    rag_cfg = profile.get("rag")
    if not rag_cfg:
        return profile

    folded = question.casefold()
    if route == "rag_only":
        if any(term in folded for term in ["line works", "line oa", "chat", "thread"]):
            rag_cfg["staging_source"] = ["docs", "logs"]
        elif any(term in folded for term in ["ocr", "render", "pdf", "image", "รูป", "สแกน"]):
            rag_cfg["staging_source"] = ["renders"]

    for tool in router_tools:
        if tool.get("name") != "query_rag":
            continue
        if route != "rag_only" and tool.get("filters", {}).get("staging_source"):
            rag_cfg["staging_source"] = tool["filters"]["staging_source"]
        if tool.get("mode"):
            rag_cfg["mode"] = tool["mode"]
    return profile


def quote_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise OrchestratorError(f"Unsafe identifier: {name}")
    return '"' + name.replace('"', '""') + '"'


def reject_unsafe_sql(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", stripped):
        raise OrchestratorError("Only read-only SELECT/WITH SQL is allowed.")
    blocked = [
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "attach",
        "detach",
        "copy",
        "export",
        "install",
        "load",
        "pragma",
        "call",
    ]
    for word in blocked:
        if re.search(rf"(?is)\b{word}\b", stripped):
            raise OrchestratorError(f"Unsafe SQL keyword is not allowed: {word}")


def connect_duckdb(database: Path):
    import duckdb

    return duckdb.connect(str(database), read_only=True)


def fetch_dicts(con: Any, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cursor = con.execute(sql, params or [])
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def inspect_tables(database: Path, tables: list[str], sample_rows: int) -> dict[str, Any]:
    if not database.exists():
        raise OrchestratorError(f"Database not found: {database}")
    con = connect_duckdb(database)
    try:
        available_rows = fetch_dicts(
            con,
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            ORDER BY table_name
            """,
        )
        available = {row["table_name"].casefold(): row["table_name"] for row in available_rows}
        selected: list[str] = []
        for table in tables:
            actual = available.get(str(table).casefold())
            if actual and actual not in selected:
                selected.append(actual)

        schemas: list[dict[str, Any]] = []
        for table in selected:
            columns = fetch_dicts(
                con,
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'main'
                  AND table_name = ?
                ORDER BY ordinal_position
                """,
                [table],
            )
            row_count = con.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0]
            samples = fetch_dicts(con, f"SELECT * FROM {quote_identifier(table)} LIMIT ?", [sample_rows])
            schemas.append(
                {
                    "table": table,
                    "row_count": int(row_count),
                    "columns": columns,
                    "sample_rows": samples,
                }
            )
        return {"requested_tables": tables, "schemas": schemas, "missing_tables": [t for t in tables if t.casefold() not in available]}
    finally:
        con.close()


def expand_related_tables(database: Path, tables: list[str], question: str) -> list[str]:
    if not database.exists():
        return tables
    con = connect_duckdb(database)
    try:
        rows = fetch_dicts(
            con,
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            """,
        )
        available = {row["table_name"].casefold(): row["table_name"] for row in rows}
    finally:
        con.close()

    expanded = list(dict.fromkeys(tables))

    def add(table: str) -> None:
        actual = available.get(table.casefold())
        if actual and actual not in expanded:
            expanded.append(actual)

    upper_tables = {table.upper() for table in tables}
    for table in upper_tables:
        for related in TABLE_RELATED_TABLES.get(table, []):
            add(related)

    folded = question.casefold()
    for terms, related_tables in RELATED_TABLE_RULES:
        if any(term.casefold() in folded for term in terms):
            for related in related_tables:
                add(related)
    return expanded


def execute_sql(database: Path, sql: str, limit: int) -> dict[str, Any]:
    if not database.exists():
        raise OrchestratorError(f"Database not found: {database}")
    reject_unsafe_sql(sql)
    limited_sql = sql.strip().rstrip(";")
    con = connect_duckdb(database)
    try:
        rows = fetch_dicts(con, f"SELECT * FROM ({limited_sql}) AS user_query LIMIT ?", [limit])
        return {"sql": sql, "rows": rows, "row_count": len(rows)}
    finally:
        con.close()


def config_llm(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}


def llm_api_settings(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    llm_cfg = config_llm(config)
    compatible = llm_cfg.get("openai_compatible", {}) if isinstance(llm_cfg.get("openai_compatible"), dict) else {}
    return {
        "api_base": args.llm_api_base or compatible.get("api_base") or DEFAULT_LLM_API_BASE,
        "model": args.llm_model or compatible.get("model") or DEFAULT_LLM_MODEL,
        "timeout": args.llm_timeout,
    }


def chat_completion(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    settings = llm_api_settings(args, config)
    api_base = str(settings["api_base"]).rstrip("/")
    payload = {
        "model": settings["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(settings["timeout"])) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise OrchestratorError(f"LLM HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise OrchestratorError(f"LLM connection failed: {exc}") from exc

    data = json.loads(body)
    choices = data.get("choices") or []
    if not choices:
        raise OrchestratorError("LLM returned no choices.")
    message = choices[0].get("message", {}) or {}
    content = message.get("content")
    if content is None or not str(content).strip():
        # Some vLLM reasoning-parser configurations place generated text in
        # reasoning_content/reasoning while leaving message.content empty.
        content = message.get("reasoning_content") or message.get("reasoning") or ""
    if not str(content).strip():
        raise OrchestratorError("LLM returned empty content.")
    return {"content": str(content).strip(), "raw": data, "settings": settings}


def compact_schemas(schemas: list[dict[str, Any]], *, max_sample_rows: int = 2) -> str:
    blocks: list[str] = []
    for schema in schemas:
        columns = ", ".join(f"{col['column_name']} {col['data_type']}" for col in schema.get("columns", []))
        samples = schema.get("sample_rows", [])[:max_sample_rows]
        blocks.append(
            "\n".join(
                [
                    f"Table: {schema.get('table')}",
                    f"Rows: {schema.get('row_count')}",
                    f"Columns: {columns}",
                    f"Sample rows JSON: {json.dumps(samples, ensure_ascii=False, default=str)}",
                ]
            )
        )
    return "\n\n".join(blocks)


def extract_sql(text: str) -> str:
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    if candidate.upper().startswith("SQL:"):
        candidate = candidate[4:].strip()
    match = re.search(r"(?is)\b(with|select)\b.*", candidate)
    if not match:
        raise OrchestratorError(f"Could not extract SQL from LLM output: {text[:300]}")
    sql = match.group(0).strip().rstrip(";")
    extra = re.search(r"(?is);\s*(select|with|insert|update|delete|drop|alter|create)\b", sql)
    if extra:
        sql = sql[: extra.start()].strip().rstrip(";")
    reject_unsafe_sql(sql)
    return sql


DATE_AXIS_CONVENTION = (
    "Date-axis convention for FACT_* period filters: when a question says in year/month/quarter/date range "
    "without explicitly naming a date column, filter by business_event_date. Use posting_date only when the "
    "question explicitly asks about posting_date, accounting posting, GL cycles, month-end close, or accounting cutoffs. "
    "Use effective_date only when the question says effective. Use as_of_date only when the question says as-of. "
    "FACT_VENDOR_PAYMENT is the important exception where posting_date often lags business_event_date, so defaulting "
    "to business_event_date is required for natural business-period questions."
)


POSTING_DATE_EXPLICIT_TERMS = [
    "posting_date",
    "posting date",
    "posted",
    "ลงบัญชี",
    "วันที่ลงบัญชี",
    "บัญชี",
    "accounting",
    "gl",
    "general ledger",
    "month-end close",
    "month end close",
    "cutoff",
    "cut-off",
    "ปิดงบ",
    "ปิดเดือน",
]


PERIOD_QUESTION_TERMS = [
    "ปี",
    "เดือน",
    "ไตรมาส",
    "quarter",
    "q1",
    "q2",
    "q3",
    "q4",
    "year",
    "month",
    "between",
    "ตั้งแต่",
    "ช่วง",
]


def explicitly_requests_posting_axis(question: str) -> bool:
    folded = question.casefold()
    return any(term in folded for term in POSTING_DATE_EXPLICIT_TERMS)


def looks_like_period_question(question: str) -> bool:
    folded = question.casefold()
    return bool(
        re.search(r"\b(?:20\d{2}|25\d{2})\b", question)
        or re.search(r"\b20\d{2}-\d{2}(?:-\d{2})?\b", question)
        or any(term in folded for term in PERIOD_QUESTION_TERMS)
    )


def date_axis_warnings(question: str, sql: str | None) -> list[str]:
    if not sql:
        return []
    if "posting_date" not in sql.casefold():
        return []
    if not looks_like_period_question(question) or explicitly_requests_posting_axis(question):
        return []
    return [
        "Generated SQL uses posting_date for a period-style question that did not explicitly request posting/accounting axis; default should be business_event_date."
    ]


def extract_json_object(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        balanced = first_balanced_json_object(candidate)
        if balanced:
            try:
                data = json.loads(balanced)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                return data
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise OrchestratorError(f"Could not extract JSON object from LLM output: {text[:300]}")
        data = json.loads(candidate[start : end + 1])
    if not isinstance(data, dict):
        raise OrchestratorError("LLM JSON output must be an object.")
    return data


def first_balanced_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        char = text[idx]
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def compact_tool_catalog(*, max_tools: int = 80) -> list[dict[str, Any]]:
    preferred_order = [
        "semantic_metric_aggregate",
        "semantic_top_n",
        "semantic_time_window_compare",
        "semantic_entity_profile",
        "semantic_duplicate_check",
        "semantic_table_profile",
    ]
    catalog = query_tools.tool_catalog()
    by_name = {tool["name"]: tool for tool in catalog}
    ordered: list[dict[str, Any]] = []
    for name in preferred_order:
        if name in by_name:
            ordered.append(by_name.pop(name))
    ordered.extend(sorted(by_name.values(), key=lambda item: item["name"]))
    return ordered[:max_tools]


def normalize_tool_request(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any], str]:
    tool_name = payload.get("tool_name") or payload.get("name") or payload.get("tool")
    if tool_name is not None:
        tool_name = str(tool_name).strip()
    if not tool_name or tool_name.lower() in {"none", "null", "no_tool"}:
        return None, {}, str(payload.get("reason") or "LLM chose no tool.")
    args = payload.get("args") or payload.get("arguments") or {}
    if not isinstance(args, dict):
        raise OrchestratorError("LLM tool args must be an object.")
    return tool_name, args, str(payload.get("reason") or "LLM selected tool.")


def execute_llm_tool_request(database: Path, request_payload: dict[str, Any]) -> dict[str, Any]:
    tool_name, tool_args, reason = normalize_tool_request(request_payload)
    if not tool_name:
        raise OrchestratorError(reason)
    if tool_name not in query_tools.TOOL_FUNCS:
        raise OrchestratorError(f"LLM selected unknown tool: {tool_name}")
    call = query_tools.ToolCall(name=tool_name, args=tool_args, reason=reason, confidence=0.65)
    result = query_tools.execute_tool(database, call)
    return result


def tool_call_to_request(tool_call: query_tools.ToolCall) -> dict[str, Any]:
    return {"tool_name": tool_call.name, "args": tool_call.args, "reason": tool_call.reason}


def compact_understanding(understanding: dict[str, Any] | None) -> dict[str, Any]:
    if not understanding:
        return {}
    constraints = understanding.get("constraints", {}) if isinstance(understanding.get("constraints"), dict) else {}
    hints = understanding.get("hints", {}) if isinstance(understanding.get("hints"), dict) else {}
    return {
        "intent": understanding.get("intent", {}),
        "metrics": constraints.get("metrics", []),
        "dimensions": constraints.get("dimensions", []),
        "dates": constraints.get("dates", []),
        "ranking": constraints.get("ranking"),
        "tables": constraints.get("tables", []),
        "columns": constraints.get("columns", []),
        "preferred_tools": hints.get("preferred_tools", []),
        "avoid_tools": hints.get("avoid_tools", []),
        "validation_contract": understanding.get("validation_contract", {}),
    }


def prevalidate_sql_tool_result(
    *,
    question: str,
    tool_result: dict[str, Any],
    understanding: dict[str, Any] | None,
) -> dict[str, Any]:
    if not understanding:
        return {"accepted": True, "warnings": [], "missing": [], "reason": "No query understanding contract."}

    constraints = understanding.get("constraints", {}) if isinstance(understanding.get("constraints"), dict) else {}
    intent = understanding.get("intent", {}) if isinstance(understanding.get("intent"), dict) else {}
    contract = understanding.get("validation_contract", {}) if isinstance(understanding.get("validation_contract"), dict) else {}
    tool_call = tool_result.get("tool_call", {}) if isinstance(tool_result.get("tool_call"), dict) else {}
    tool_name = tool_call.get("name")
    rows = tool_result.get("rows") or []
    row_keys = set()
    for row in rows[:5]:
        if isinstance(row, dict):
            row_keys.update(str(key).casefold() for key in row.keys())

    warnings: list[str] = []
    missing: list[str] = []
    primary = intent.get("primary")
    ranking = constraints.get("ranking")
    metrics = constraints.get("metrics") or []
    dimensions = constraints.get("dimensions") or []

    if not rows:
        missing.append("non-empty SQL result rows")

    if primary in {"ranking", "aggregation", "comparison"} and tool_name == "semantic_entity_profile":
        warnings.append("Entity profile does not prove ranking or aggregate answer.")

    if ranking:
        metric_name = str(ranking.get("by") or "count").casefold()
        metric_aliases = {
            metric_name,
            "count",
            "total",
            "row_count",
            "transaction_count",
            "net_total_thb",
            "sum_net_total_thb",
            "units_sold",
            "amount_thb",
            "max_amount_thb",
            "points",
            "total_points_earned",
            "stockout_events",
            "branch_count",
            "affected_branch_count",
        }
        has_metric = bool(row_keys.intersection(metric_aliases)) or any(
            any(alias and alias in key for alias in metric_aliases) or "count" in key or "total" in key
            for key in row_keys
        )
        has_dimension = bool(
            row_keys.intersection(
                {
                    "bank_txn_id",
                    "branch_code",
                    "branch_name",
                    "sales_year",
                    "sku_id",
                    "product_name",
                    "vendor_id",
                    "vendor_name",
                    "customer_id",
                    "loyalty_tier",
                    "campaign_id",
                    "employee_id",
                    "employee_name",
                }
            )
        )
        if not has_metric:
            missing.append("ranking metric column")
        if dimensions and not has_dimension:
            missing.append("ranked entity column")
        if tool_name == "semantic_metric_aggregate":
            args = tool_call.get("args", {}) if isinstance(tool_call.get("args"), dict) else {}
            if args.get("filters") and not args.get("dimensions"):
                warnings.append("Aggregate filtered to one entity without grouping cannot prove top/bottom.")

    if metrics and primary in {"aggregation", "metric_lookup", "comparison", "ranking"}:
        metric_terms = {str(metric.get("name", "")).casefold() for metric in metrics if isinstance(metric, dict)}
        if "count" in metric_terms:
            metric_terms.update({"count", "row_count", "transaction_count"})
        if "net_total_thb" in metric_terms:
            metric_terms.update({"total", "net_total_thb", "sum_net_total_thb", "revenue"})
        if "msrp_thb" in metric_terms:
            metric_terms.update({"msrp_thb", "price", "value"})
        if "units_sold" in metric_terms:
            metric_terms.update({"units_sold", "quantity", "qty"})
        if "amount_thb" in metric_terms:
            metric_terms.update({"amount_thb", "max_amount_thb", "deposit_amount"})
        if "points" in metric_terms:
            metric_terms.update({"points", "total_points_earned", "earned_points"})
        if "stockout_events" in metric_terms:
            metric_terms.update({"stockout_events", "stockout_count", "branch_count", "affected_branch_count"})
        metric_present = any(any(term and term in key for term in metric_terms) for key in row_keys)
        if "count" in metric_terms and rows:
            metric_present = True
        if metric_terms and not metric_present:
            missing.append("requested metric value")

    if primary == "policy_as_of":
        policy_keys = {
            "policy_variable",
            "policy_value",
            "value",
            "effective_date",
            "effective_from",
            "effective_to",
            "end_date",
            "version",
            "policy_version_id",
        }
        if not row_keys.intersection(policy_keys):
            missing.append("policy value/effective version columns")

    accepted = not warnings and not missing
    return {
        "accepted": accepted,
        "warnings": warnings,
        "missing": list(dict.fromkeys(missing)),
        "reason": "Result shape matches query understanding." if accepted else "; ".join([*warnings, *missing]),
        "contract": contract,
    }


def choose_sql_tool_with_llm(
    args: argparse.Namespace,
    *,
    config: dict[str, Any],
    question: str,
    tables_hint: list[str],
    schemas: list[dict[str, Any]],
    previous_attempts: list[dict[str, Any]],
    understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if args.llm_mode != "openai_compatible":
        raise OrchestratorError("Agentic SQL tool mode requires --llm-mode openai_compatible.")
    response = chat_completion(
        args=args,
        config=config,
        messages=[
            {
                "role": "system",
                "content": (
                    "You choose one safe read-only FahMai SQL tool to answer the business question. "
                    "Prefer generic semantic tools for unseen analytics instead of memorized benchmark tools. "
                    "Return JSON only with keys: tool_name, args, reason. "
                    "Use null for tool_name only if no listed tool can help. Do not invent tools or columns."
                ),
            },
            {
                "role": "user",
                "content": "\n\n".join(
                    [
                        f"Question: {question}",
                        f"Query understanding JSON: {json.dumps(compact_understanding(understanding), ensure_ascii=False, default=str)}",
                        f"Tables hint: {json.dumps(tables_hint, ensure_ascii=False)}",
                        f"Available schemas: {compact_schemas(schemas, max_sample_rows=1)}",
                        f"Tool catalog JSON: {json.dumps(compact_tool_catalog(), ensure_ascii=False)}",
                        f"Previous attempts JSON: {json.dumps(previous_attempts, ensure_ascii=False, default=str)}",
                        "Choose the next tool call as JSON.",
                    ]
                ),
            },
        ],
        temperature=args.llm_sql_temperature,
        max_tokens=args.llm_sql_max_tokens,
    )
    payload = extract_json_object(response["content"])
    payload["_llm_response"] = response["content"]
    payload["_llm_settings"] = response["settings"]
    return payload


def validate_sql_tool_result_with_llm(
    args: argparse.Namespace,
    *,
    config: dict[str, Any],
    question: str,
    tool_result: dict[str, Any],
    previous_attempts: list[dict[str, Any]],
    understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if args.llm_mode != "openai_compatible":
        return {"answer_ready": bool(tool_result.get("rows")), "reason": "No LLM validation.", "next_tool": None}
    compact_result = {
        "tool_call": tool_result.get("tool_call"),
        "row_count": tool_result.get("row_count"),
        "rows": (tool_result.get("rows") or [])[: args.sql_limit],
    }
    response = chat_completion(
        args=args,
        config=config,
        messages=[
            {
                "role": "system",
                "content": (
                    "Validate whether the SQL tool result fully answers the question. "
                    "Check all requested slots, dates, tie handling, entity/brand scope, and before/as-of semantics. "
                    f"{DATE_AXIS_CONVENTION} "
                    "Return JSON only: {\"answer_ready\": boolean, \"reason\": string, "
                    "\"missing\": [string], \"next_tool\": {\"tool_name\": string, \"args\": object, \"reason\": string} or null}."
                ),
            },
            {
                "role": "user",
                "content": "\n\n".join(
                    [
                        f"Question: {question}",
                        f"Query understanding JSON: {json.dumps(compact_understanding(understanding), ensure_ascii=False, default=str)}",
                        f"Validation contract JSON: {json.dumps((understanding or {}).get('validation_contract', {}), ensure_ascii=False, default=str)}",
                        f"Current tool result JSON: {json.dumps(compact_result, ensure_ascii=False, default=str)}",
                        f"Previous attempts JSON: {json.dumps(previous_attempts, ensure_ascii=False, default=str)}",
                        f"Tool catalog JSON: {json.dumps(compact_tool_catalog(), ensure_ascii=False)}",
                        "Validate and optionally propose exactly one next tool call.",
                    ]
                ),
            },
        ],
        temperature=0.0,
        max_tokens=args.llm_sql_max_tokens,
    )
    payload = extract_json_object(response["content"])
    payload["_llm_response"] = response["content"]
    payload["_llm_settings"] = response["settings"]
    return payload


def run_agent_sql_tools(
    args: argparse.Namespace,
    *,
    config: dict[str, Any],
    question: str,
    tables_hint: list[str],
    schemas: list[dict[str, Any]],
    understanding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    attempts: list[dict[str, Any]] = []
    next_request: dict[str, Any] | None = None
    max_attempts = max(1, int(args.sql_tool_agent_max_attempts))

    def useful_attempt_rows(current_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Carry forward useful successful rows from prior retry attempts.

        The validator may ask for a follow-up tool call when a question asks
        for multiple entities/windows. Those earlier rows are still evidence
        and should be available to answer synthesis.
        """
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in attempts:
            rows = item.get("rows_preview") or []
            if item.get("status") not in {"retry", "ok"} or not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                if key not in seen:
                    merged.append(row)
                    seen.add(key)
        for row in current_rows:
            key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                merged.append(row)
                seen.add(key)
        return merged

    for attempt_idx in range(1, max_attempts + 1):
        if next_request:
            request_payload = next_request
        else:
            try:
                request_payload = choose_sql_tool_with_llm(
                    args,
                    config=config,
                    question=question,
                    tables_hint=tables_hint,
                    schemas=schemas,
                    previous_attempts=attempts,
                    understanding=understanding,
                )
            except Exception as exc:
                fallback_tool = query_tools.select_tool(question)
                fallback_request = tool_call_to_request(fallback_tool) if fallback_tool else None
                attempts.append(
                    {
                        "attempt": attempt_idx,
                        "status": "planner_error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "fallback_request": fallback_request,
                    }
                )
                next_request = fallback_request
                continue
        tool_name, tool_args, reason = normalize_tool_request(request_payload)
        if not tool_name:
            attempts.append({"attempt": attempt_idx, "request": request_payload, "status": "no_tool", "reason": reason})
            break
        if (
            tool_name == "policy_value_as_of"
            and isinstance(tool_args, dict)
            and tool_args.get("as_of_date")
            and ("before" in question.casefold() or "ก่อนวันที่" in question.casefold())
        ):
            tool_args = dict(tool_args)
            tool_args["as_of_date"] = query_tools.previous_day(str(tool_args["as_of_date"]))
            request_payload = dict(request_payload)
            if "arguments" in request_payload and isinstance(request_payload["arguments"], dict):
                request_payload["arguments"] = dict(request_payload["arguments"])
                request_payload["arguments"]["as_of_date"] = tool_args["as_of_date"]
            elif "args" in request_payload and isinstance(request_payload["args"], dict):
                request_payload["args"] = dict(request_payload["args"])
                request_payload["args"]["as_of_date"] = tool_args["as_of_date"]
        try:
            tool_result = execute_llm_tool_request(args.database, request_payload)
            prevalidation = prevalidate_sql_tool_result(
                question=question,
                tool_result=tool_result,
                understanding=understanding,
            )
            if not prevalidation["accepted"]:
                trace_item = {
                    "attempt": attempt_idx,
                    "request": {k: v for k, v in request_payload.items() if not k.startswith("_")},
                    "llm_request": request_payload.get("_llm_response"),
                    "tool_call": tool_result.get("tool_call"),
                    "row_count": tool_result.get("row_count"),
                    "rows_preview": (tool_result.get("rows") or [])[:5],
                    "prevalidation": prevalidation,
                    "status": "prevalidation_reject",
                }
                attempts.append(trace_item)
                next_request = None
                continue
            validation = validate_sql_tool_result_with_llm(
                args,
                config=config,
                question=question,
                tool_result=tool_result,
                previous_attempts=attempts,
                understanding=understanding,
            )
            validation_for_trace = {k: v for k, v in validation.items() if not k.startswith("_")}
            trusted_specific_tool = bool(
                prevalidation.get("accepted")
                and tool_name
                and not str(tool_name).startswith("semantic_")
                and tool_result.get("rows")
            )
            if trusted_specific_tool and not validation.get("answer_ready"):
                validation = dict(validation)
                validation["answer_ready"] = True
                validation["validator_override"] = "Curated deterministic tool passed query-understanding prevalidation."
                validation_for_trace = {k: v for k, v in validation.items() if not k.startswith("_")}
            trace_item = {
                "attempt": attempt_idx,
                "request": {k: v for k, v in request_payload.items() if not k.startswith("_")},
                "llm_request": request_payload.get("_llm_response"),
                "tool_call": tool_result.get("tool_call"),
                "row_count": tool_result.get("row_count"),
                "rows_preview": (tool_result.get("rows") or [])[:5],
                "prevalidation": prevalidation,
                "validation": validation_for_trace,
                "llm_validation": validation.get("_llm_response"),
                "status": "ok" if validation.get("answer_ready") else "retry",
            }
            attempts.append(trace_item)
            if validation.get("answer_ready"):
                rows = tool_result["rows"]
                if str(tool_name).startswith("semantic_"):
                    rows = useful_attempt_rows(rows)
                return {
                    "tool": "query_sql",
                    "status": "ok" if tool_result.get("rows") else "weak",
                    "tables_hint": tables_hint,
                    "schemas": schemas,
                    "missing_tables": [],
                    "sql_tool_mode": "agent",
                    "agent_tool_trace": attempts,
                    "sql_tool_call": tool_result["tool_call"],
                    "sql": tool_result["sql"],
                    "params": tool_result.get("params", []),
                    "rows": rows,
                    "row_count": len(rows),
                    "warnings": date_axis_warnings(question, tool_result.get("sql")),
                }
            proposed = validation.get("next_tool")
            next_request = proposed if isinstance(proposed, dict) else None
            if not next_request:
                break
        except Exception as exc:
            attempts.append(
                {
                    "attempt": attempt_idx,
                    "request": {k: v for k, v in request_payload.items() if not k.startswith("_")},
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            next_request = None

    if attempts:
        last_rows = []
        last_sql = None
        last_call = None
        for item in reversed(attempts):
            if item.get("rows_preview") is not None:
                last_rows = item.get("rows_preview") or []
                last_call = item.get("tool_call")
                break
        return {
            "tool": "query_sql",
            "status": "weak" if last_rows else "needs_sql_generation",
            "tables_hint": tables_hint,
            "schemas": schemas,
            "missing_tables": [],
            "sql_tool_mode": "agent",
            "agent_tool_trace": attempts,
            "sql_tool_call": last_call,
            "sql": last_sql,
            "params": [],
            "rows": last_rows,
            "row_count": len(last_rows),
            "warnings": date_axis_warnings(question, last_sql),
            "message": "Agentic SQL tool loop did not validate an answer-ready result.",
        }
    return None


def generate_sql_with_llm(
    args: argparse.Namespace,
    *,
    config: dict[str, Any],
    question: str,
    schemas: list[dict[str, Any]],
    tables_hint: list[str],
    rag_chunks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    context_chunks = []
    for chunk in (rag_chunks or [])[: args.llm_context_chunks]:
        context_chunks.append(
            {
                "chunk_id": chunk.get("chunk_id"),
                "source": chunk.get("staging_source"),
                "title": chunk.get("title"),
                "snippet": chunk.get("snippet"),
            }
        )
    messages = [
        {
            "role": "system",
            "content": (
                "You generate safe DuckDB SQL for FahMai analytics. "
                "Return exactly one read-only SQL query and nothing else. "
                "Use only SELECT or WITH. Do not use INSERT, UPDATE, DELETE, DROP, CREATE, COPY, PRAGMA, or CALL. "
                "Use only tables and columns shown in the schema context. Prefer exact IDs from the question. "
                f"{DATE_AXIS_CONVENTION}"
            ),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    f"Question: {question}",
                    f"Tables hint: {', '.join(tables_hint) if tables_hint else '(none)'}",
                    "Schema context:",
                    compact_schemas(schemas),
                    f"RAG context JSON: {json.dumps(context_chunks, ensure_ascii=False)}",
                    "Return one DuckDB SQL query.",
                ]
            ),
        },
    ]
    response = chat_completion(
        args=args,
        config=config,
        messages=messages,
        temperature=args.llm_sql_temperature,
        max_tokens=args.llm_sql_max_tokens,
    )
    sql = extract_sql(response["content"])
    return {"sql": sql, "llm_response": response["content"], "llm_settings": response["settings"]}


def make_rag_args(args: argparse.Namespace, *, query: str, rag_cfg: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        database=args.database,
        query=query,
        top_k=args.top_k,
        candidate_k=args.candidate_k,
        mode=rag_cfg.get("mode", "hybrid"),
        staging_source=list(rag_cfg.get("staging_source", [])),
        source_type=[],
        source_name=[],
        model=args.model,
        model_path=args.model_path,
        device=args.device,
        offline=args.offline,
        snippet_chars=args.snippet_chars,
        wait_for_db_seconds=args.wait_for_db_seconds,
        vector_weight=args.vector_weight,
        text_weight=args.text_weight,
        exact_weight=args.exact_weight,
        source_weight=args.source_weight,
        rerank=False,
        reranker_model="BAAI/bge-reranker-v2-m3",
        expand_entities=args.expand_entities,
        resolver_config=args.router_config,
    )


def run_rag_step(args: argparse.Namespace, *, query: str, rag_cfg: dict[str, Any]) -> dict[str, Any]:
    rag_args = make_rag_args(args, query=query, rag_cfg=rag_cfg)
    try:
        result = query_rag.run_query(rag_args)
        return {
            "tool": "query_rag",
            "status": "ok" if result.get("results") else "weak",
            "mode": rag_args.mode,
            "filters": {"staging_source": rag_args.staging_source},
            "result_count": len(result.get("results", [])),
            "chunks": result.get("results", []),
            "retrieval": result.get("retrieval", {}),
            "duration_ms": result.get("duration_ms"),
        }
    except Exception as exc:
        return {
            "tool": "query_rag",
            "status": "error",
            "mode": rag_cfg.get("mode", "hybrid"),
            "filters": {"staging_source": rag_cfg.get("staging_source", [])},
            "error_type": type(exc).__name__,
            "message": str(exc),
        }


def run_sql_step(
    args: argparse.Namespace,
    *,
    config: dict[str, Any],
    question: str,
    tables_hint: list[str],
    rag_chunks: list[dict[str, Any]] | None = None,
    understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        tables_hint = expand_related_tables(args.database, tables_hint, question)
        if args.sql:
            result = execute_sql(args.database, args.sql, args.sql_limit)
            return {
                "tool": "query_sql",
                "status": "ok" if result["rows"] else "weak",
                "warnings": date_axis_warnings(question, args.sql),
                **result,
            }

        schema_result = inspect_tables(args.database, tables_hint, args.sample_rows)
        if args.enable_sql_tools:
            if args.sql_tool_mode == "agent":
                agent_result = run_agent_sql_tools(
                    args,
                    config=config,
                    question=question,
                    tables_hint=tables_hint,
                    schemas=schema_result["schemas"],
                    understanding=understanding,
                )
                if agent_result:
                    agent_result["missing_tables"] = schema_result["missing_tables"]
                    return agent_result

            tool_call = query_tools.select_tool(question)
            if tool_call and args.sql_tool_mode in {"deterministic", "deterministic_agent"}:
                tool_result = query_tools.execute_tool(args.database, tool_call)
                if args.sql_tool_mode == "deterministic_agent" and not tool_result["rows"]:
                    agent_result = run_agent_sql_tools(
                        args,
                        config=config,
                        question=question,
                        tables_hint=tables_hint,
                        schemas=schema_result["schemas"],
                        understanding=understanding,
                    )
                    if agent_result:
                        agent_result["deterministic_tool_call"] = tool_result["tool_call"]
                        agent_result["missing_tables"] = schema_result["missing_tables"]
                        return agent_result
                return {
                    "tool": "query_sql",
                    "status": "ok" if tool_result["rows"] else "weak",
                    "sql_tool_mode": args.sql_tool_mode,
                    "tables_hint": tables_hint,
                    "schemas": schema_result["schemas"],
                    "missing_tables": schema_result["missing_tables"],
                    "sql_tool_call": tool_result["tool_call"],
                    "sql": tool_result["sql"],
                    "params": tool_result.get("params", []),
                    "rows": tool_result["rows"],
                    "row_count": tool_result["row_count"],
                    "warnings": date_axis_warnings(question, tool_result.get("sql")),
                }

            if args.sql_tool_mode == "deterministic_agent":
                agent_result = run_agent_sql_tools(
                    args,
                    config=config,
                    question=question,
                    tables_hint=tables_hint,
                    schemas=schema_result["schemas"],
                    understanding=understanding,
                )
                if agent_result:
                    agent_result["missing_tables"] = schema_result["missing_tables"]
                    return agent_result

        if args.llm_mode == "openai_compatible" and args.enable_sql_generation:
            generated = generate_sql_with_llm(
                args,
                config=config,
                question=question,
                schemas=schema_result["schemas"],
                tables_hint=tables_hint,
                rag_chunks=rag_chunks,
            )
            result = execute_sql(args.database, generated["sql"], args.sql_limit)
            return {
                "tool": "query_sql",
                "status": "ok" if result["rows"] else "weak",
                "tables_hint": tables_hint,
                "schemas": schema_result["schemas"],
                "missing_tables": schema_result["missing_tables"],
                "generated_sql": generated["sql"],
                "llm_response": generated["llm_response"],
                "llm_settings": generated["llm_settings"],
                "warnings": date_axis_warnings(question, generated["sql"]),
                **result,
            }

        return {
            "tool": "query_sql",
            "status": "needs_sql_generation",
            "tables_hint": tables_hint,
            "schemas": schema_result["schemas"],
            "missing_tables": schema_result["missing_tables"],
            "message": "LLM SQL generation is disabled or no --sql was provided.",
        }
    except Exception as exc:
        return {
            "tool": "query_sql",
            "status": "error",
            "tables_hint": tables_hint,
            "error_type": type(exc).__name__,
            "message": str(exc),
        }


def extract_tables_hint(router_payload: dict[str, Any], understanding: dict[str, Any] | None = None) -> list[str]:
    tables: list[str] = []
    for tool in router_payload.get("tools", []):
        for table in tool.get("tables_hint", []) or []:
            if table not in tables:
                tables.append(table)
    for table in router_payload.get("entity_resolution", {}).get("likely_tables", []) or []:
        if table not in tables:
            tables.append(table)
    for table in (understanding or {}).get("hints", {}).get("tables_hint", []) or []:
        if table not in tables:
            tables.append(table)
    return tables


def extract_rag_query(router_payload: dict[str, Any], fallback_question: str, understanding: dict[str, Any] | None = None) -> str:
    for tool in router_payload.get("tools", []):
        if tool.get("name") == "query_rag" and tool.get("query_hint"):
            return str(tool["query_hint"])
    if (understanding or {}).get("hints", {}).get("rag_query"):
        return str(understanding["hints"]["rag_query"])
    return str(router_payload.get("entity_resolution", {}).get("rewritten_query") or fallback_question)


def execute_route_attempt(
    args: argparse.Namespace,
    *,
    config: dict[str, Any],
    route: str,
    router_payload: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = route_profile_for_question(
        route=route,
        question=router_payload["question"],
        profiles=profiles,
        router_tools=router_payload.get("tools", []),
    )
    steps: list[dict[str, Any]] = []
    tables_hint = extract_tables_hint(router_payload, understanding)
    rag_query = extract_rag_query(router_payload, router_payload["question"], understanding)

    if profile.get("rag"):
        steps.append(run_rag_step(args, query=rag_query, rag_cfg=profile["rag"]))
    if profile.get("sql"):
        rag_chunks: list[dict[str, Any]] = []
        for step in steps:
            if step.get("tool") == "query_rag":
                rag_chunks = step.get("chunks", []) or []
        steps.append(
            run_sql_step(
                args,
                config=config,
                question=router_payload["question"],
                tables_hint=tables_hint,
                rag_chunks=rag_chunks,
                understanding=understanding,
            )
        )

    return {"route": route, "question": router_payload["question"], "profile": profile, "steps": steps}


def validate_attempt(attempt: dict[str, Any], *, require_answer_ready: bool) -> dict[str, Any]:
    warnings: list[str] = []
    question = str(attempt.get("question") or "")
    has_ok_rag = False
    has_ok_sql = False
    has_ok_react = False
    has_sql_context = False
    has_error = False

    for step in attempt["steps"]:
        status = step.get("status")
        for warning in step.get("warnings", []) or []:
            warnings.append(str(warning))
        if question:
            warnings.extend(date_axis_warnings(question, step.get("sql") or step.get("generated_sql")))
        if status == "error":
            has_error = True
            warnings.append(f"{step.get('tool')} error: {step.get('message')}")
        if step.get("tool") == "query_rag" and status == "ok":
            has_ok_rag = True
        if step.get("tool") == "query_sql" and status == "ok":
            has_ok_sql = True
        if step.get("tool") == "query_react" and status == "ok":
            has_ok_react = True
        if step.get("tool") == "query_sql" and status == "needs_sql_generation":
            has_sql_context = True

    route = attempt["route"]
    if route == "sql_only":
        ok = has_ok_sql or has_sql_context
    elif route == "rag_only":
        ok = has_ok_rag
    elif route in {"rag_assisted_sql", "hybrid_sql_rag"}:
        ok = has_ok_rag or has_ok_sql or has_sql_context
    elif route == "safe_refuse_or_verify":
        ok = has_ok_rag or not attempt["steps"]
    else:
        ok = has_ok_react or not has_error

    answer_ready = has_ok_react or has_ok_sql or (route == "rag_only" and has_ok_rag)
    if has_sql_context and not has_ok_sql:
        warnings.append("SQL generation is needed before an exact structured answer can be produced.")
    if require_answer_ready and not answer_ready:
        ok = False
    deduped_warnings = list(dict.fromkeys(warnings))

    return {
        "ok": bool(ok),
        "answer_ready": bool(answer_ready),
        "has_ok_rag": has_ok_rag,
        "has_ok_sql": has_ok_sql,
        "has_ok_react": has_ok_react,
        "has_sql_context": has_sql_context,
        "warnings": deduped_warnings,
    }


# --- Final-answer guards (see agent_error_analysis_handoff.md) -------------------
# Priority 1: completeness checker for multi-part questions.
# Priority 2: refusal guard when non-empty evidence exists.

REFUSAL_PHRASES: tuple[str, ...] = (
    "ไม่พบข้อมูล",
    "ไม่สามารถตอบ",
    "ข้อมูลไม่เพียงพอ",
    "ไม่พบหลักฐาน",
    "ไม่มีข้อมูล",
    "ไม่มีผลลัพธ์",
    "not found",
    "insufficient data",
    "insufficient evidence",
    "cannot answer",
    "unable to answer",
    "no data",
)

REFUSAL_REPAIR_INSTRUCTION = (
    "Evidence is available. Do not refuse. Summarize only the evidence values. "
    "If a subpart is truly missing, state only that subpart as missing."
)

COMPLETENESS_REPAIR_INSTRUCTION = (
    "The previous answer omitted required parts. Use only the provided evidence. "
    "Answer every requested part explicitly. Do not invent new facts."
)

_THAI_DIGITS = {
    "๐": "0", "๑": "1", "๒": "2", "๓": "3", "๔": "4",
    "๕": "5", "๖": "6", "๗": "7", "๘": "8", "๙": "9",
}


def _thai_to_arabic(text: str) -> str:
    return "".join(_THAI_DIGITS.get(ch, ch) for ch in text)


def answer_contains_refusal(text: str | None) -> bool:
    """Return True if the final answer reads as a refusal / "no data" response."""
    if not text:
        return True
    lowered = str(text).casefold()
    return any(phrase.casefold() in lowered for phrase in REFUSAL_PHRASES)


def required_part_count(question: str, understanding: dict[str, Any] | None = None) -> int:
    """Best-effort count of explicitly requested sub-parts in a multi-part question.

    Returns 0/1 for single-answer questions. Light heuristics only: explicit
    ``(1) (2) (3)`` markers, Thai ``ตอบ N ข้อ`` phrasing, and the query-understanding
    validation contract when present.
    """
    q = _thai_to_arabic(str(question or ""))
    counts: list[int] = []

    explicit_markers = {int(n) for n in re.findall(r"\((\d+)\)", q)}
    if explicit_markers:
        counts.append(max(explicit_markers))

    for n in re.findall(r"ตอบ\s*(\d+)\s*ข้อ", q):
        counts.append(int(n))
    for n in re.findall(r"(\d+)\s*ข้อ", q):
        counts.append(int(n))

    if understanding:
        contract = understanding.get("validation_contract")
        if isinstance(contract, dict):
            for key in ("required_parts", "required_fields", "must_include"):
                value = contract.get(key)
                if isinstance(value, list) and len(value) > 1:
                    counts.append(len(value))

    return max(counts) if counts else 0


def _has_part_marker(answer: str, index: int) -> bool:
    return any(
        marker in answer
        for marker in (
            f"({index})",
            f"{index})",
            f"{index}.",
            f"ข้อ {index}",
            f"ข้อที่ {index}",
        )
    )


def answer_covers_parts(text: str | None, required_parts: int) -> bool:
    """Heuristic: does the answer enumerate every requested sub-part?"""
    if required_parts <= 1:
        return True
    if not text:
        return False
    answer = _thai_to_arabic(str(text))
    covered = sum(1 for i in range(1, required_parts + 1) if _has_part_marker(answer, i))
    return covered >= required_parts


def synthesize_answer_package(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    attempts: list[dict[str, Any]],
    validation: dict[str, Any],
    question: str,
    understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_steps = attempts[-1]["steps"] if attempts else []
    sql_results: list[dict[str, Any]] = []
    rag_citations: list[str] = []
    notes: list[str] = list(validation.get("warnings", []))

    for step in final_steps:
        if step.get("tool") == "query_sql" and step.get("rows") is not None:
            sql_results = step.get("rows", [])
        if step.get("tool") == "query_react":
            if step.get("rows") is not None:
                sql_results = step.get("rows", [])
            if step.get("rag_citations"):
                rag_citations = [str(chunk_id) for chunk_id in step.get("rag_citations", []) if chunk_id]
        if step.get("tool") == "query_rag":
            rag_citations = [chunk["chunk_id"] for chunk in step.get("chunks", []) if chunk.get("chunk_id")]

    react_answer = None
    for step in final_steps:
        if step.get("tool") == "query_react" and step.get("answer_text"):
            react_answer = str(step["answer_text"])
            break

    total_output_token_count = None
    if react_answer:
        answer_text = react_answer
    elif args.llm_mode == "none":
        notes.append("LLM answer synthesis is disabled.")
        answer_text = None
    elif args.llm_mode == "mock":
        answer_text = "Mock synthesis: use the returned SQL rows and cited chunks to write the final answer."
    elif args.llm_mode == "openai_compatible" and args.enable_answer_synthesis:
        try:
            evidence_chunks = []
            for step in final_steps:
                if step.get("tool") == "query_rag":
                    for chunk in step.get("chunks", [])[: args.llm_context_chunks]:
                        evidence_chunks.append(
                            {
                                "chunk_id": chunk.get("chunk_id"),
                                "source": chunk.get("staging_source"),
                                "title": chunk.get("title"),
                                "snippet": chunk.get("snippet"),
                            }
                        )
                if step.get("tool") == "query_react":
                    for chunk in step.get("rag_chunks", [])[: args.llm_context_chunks]:
                        evidence_chunks.append(
                            {
                                "chunk_id": chunk.get("chunk_id"),
                                "source": chunk.get("staging_source"),
                                "title": chunk.get("title"),
                                "snippet": chunk.get("snippet"),
                            }
                        )
            sql_payload = []
            for step in final_steps:
                if step.get("tool") == "query_sql" and step.get("status") == "ok":
                    sql_payload.append(
                        {
                            "sql": step.get("sql") or step.get("generated_sql"),
                            "sql_tool_call": step.get("sql_tool_call"),
                            "rows": step.get("rows", []),
                            "tables_hint": step.get("tables_hint", []),
                        }
                    )
                if step.get("tool") == "query_react" and step.get("rows"):
                    sql_payload.append(
                        {
                            "sql": step.get("sql"),
                            "sql_tool_call": {"name": "query_react"},
                            "rows": step.get("rows", []),
                            "tables_hint": [],
                        }
                    )
            public_context = {
                "route": attempts[-1].get("route") if attempts else None,
                "has_sql_results": bool(sql_payload),
                "has_rag_evidence": bool(evidence_chunks),
            }
            base_system_content = (
                "Answer FahMai business questions using only the provided SQL results and RAG evidence. "
                "The company name is FahMai in English and ฟ้าใหม่ in Thai; preserve it exactly. "
                "Answer in the user's language and do not mix in unrelated languages. "
                "Be concise. Cite supporting chunk IDs when using RAG evidence. "
                "If the provided evidence is incomplete, say which business facts could not be found. "
                "Do not mention internal pipeline state, validation flags, tool status, JSON field names, "
                "or SQL-generation mechanics."
            )
            user_content = "\n\n".join(
                [
                    f"Question: {question}",
                    f"Answer requirements JSON: {json.dumps(compact_understanding(understanding), ensure_ascii=False, default=str)}",
                    f"Available context summary: {json.dumps(public_context, ensure_ascii=False, default=str)}",
                    f"SQL tool JSON: {json.dumps(sql_payload, ensure_ascii=False, default=str)}",
                    f"RAG evidence JSON: {json.dumps(evidence_chunks, ensure_ascii=False, default=str)}",
                    "Write the final answer.",
                ]
            )

            def _run_synthesis(repair_instruction: str | None = None) -> dict[str, Any]:
                system_content = base_system_content
                if repair_instruction:
                    system_content = f"{system_content} {repair_instruction}"
                return chat_completion(
                    args=args,
                    config=config,
                    messages=[
                        {"role": "system", "content": system_content},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=args.llm_answer_temperature,
                    max_tokens=args.llm_answer_max_tokens,
                )

            def _record_tokens(resp: dict[str, Any]) -> None:
                nonlocal total_output_token_count
                usage = resp.get("raw", {}).get("usage", {})
                completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
                if isinstance(completion_tokens, int):
                    total_output_token_count = (total_output_token_count or 0) + completion_tokens

            response = _run_synthesis()
            answer_text = response["content"]
            _record_tokens(response)

            # Final-answer guards: retry synthesis once (same evidence) when the
            # answer either refuses despite available evidence, or omits requested
            # sub-parts of a multi-part question.
            has_evidence = bool(sql_payload) or bool(evidence_chunks)
            required_parts = required_part_count(question, understanding)
            repair_instruction: str | None = None
            if has_evidence and answer_contains_refusal(answer_text):
                repair_instruction = REFUSAL_REPAIR_INSTRUCTION
                notes.append(
                    "refusal_guard: evidence was available but the answer refused; retried synthesis once."
                )
            elif required_parts > 1 and not answer_covers_parts(answer_text, required_parts):
                repair_instruction = COMPLETENESS_REPAIR_INSTRUCTION
                notes.append(
                    f"completeness_checker: answer did not cover all {required_parts} requested parts; "
                    "retried synthesis once."
                )

            if repair_instruction:
                try:
                    retry_response = _run_synthesis(repair_instruction)
                    retry_text = retry_response["content"]
                    _record_tokens(retry_response)
                    if retry_text and retry_text.strip():
                        answer_text = retry_text
                except Exception as exc:  # best-effort repair; keep the first answer
                    notes.append(
                        f"final-answer repair retry failed: {type(exc).__name__}: {exc}"
                    )
        except Exception as exc:
            answer_text = None
            notes.append(f"LLM answer synthesis failed: {type(exc).__name__}: {exc}")
    else:
        answer_text = None
        notes.append(f"LLM mode {args.llm_mode} is configured as a placeholder in v1.")

    return {
        "answer_ready": bool(validation["answer_ready"] and (args.llm_mode in {"none", "mock"} or answer_text)),
        "answer_text": answer_text,
        "total_output_token_count": total_output_token_count,
        "sql_results": sql_results,
        "rag_citations": rag_citations,
        "notes": notes,
    }


def run_orchestrator(args: argparse.Namespace) -> dict[str, Any]:
    started = now_ms()
    config = load_json(args.router_config)
    profiles = merge_route_profiles(config)
    fallbacks = load_fallbacks(config)
    guard_payload = None
    if any(getattr(args, name, None) for name in ("guard_json", "guard_json_inline", "guard_json_b64")):
        guard_payload = query_understanding.parse_guard_payload(args)

    understanding: dict[str, Any] | None = None
    if getattr(args, "enable_query_understanding", True):
        understanding = query_understanding.understand_question(
            args.question,
            config=config,
            sanitized_question=args.sanitized_question,
            guard=guard_payload,
        )
        effective_question = understanding["normalized_question"].strip()
    else:
        effective_question = args.sanitized_question.strip() if args.sanitized_question else args.question.strip()

    guard_for_router = None
    if understanding and understanding.get("guard", {}).get("provided"):
        guard_for_router = {
            "source": "input_guard",
            "status": understanding["guard"].get("status"),
            "attack_detected": understanding["guard"].get("attack_detected"),
            "attack_types": understanding["guard"].get("attack_types", []),
            "safe_to_route": understanding["guard"].get("safe_to_route", True),
            "sanitized_question": effective_question,
            "notes": understanding["guard"].get("notes", []),
            "raw": guard_payload,
        }

    router_payload = query_router.route_question(
        effective_question,
        question_id=args.question_id,
        config=config,
        mode=args.router_mode,
        guard=guard_for_router,
    )
    initial_route = router_payload["route"]

    if args.mode == "plan":
        profile = route_profile_for_question(
            route=initial_route,
            question=effective_question,
            profiles=profiles,
            router_tools=router_payload.get("tools", []),
        )
        return {
            "ok": True,
            "question": args.question,
            "effective_question": effective_question,
            "route": initial_route,
            "query_understanding": understanding,
            "router": router_payload,
            "planned_profile": profile,
            "validation": {"ok": True, "answer_ready": False, "fallback_used": False, "warnings": []},
            "fallback_trace": [],
            "answer_package": {"answer_ready": False, "notes": ["Plan mode does not execute tools."]},
            "duration_ms": now_ms() - started,
        }

    attempts: list[dict[str, Any]] = []
    fallback_trace: list[dict[str, Any]] = []
    routes_to_try = [initial_route, *fallbacks.get(initial_route, [])]
    final_validation: dict[str, Any] | None = None

    for index, route in enumerate(routes_to_try):
        attempt = execute_route_attempt(
            args,
            config=config,
            route=route,
            router_payload=router_payload,
            profiles=profiles,
            understanding=understanding,
        )
        validation = validate_attempt(attempt, require_answer_ready=args.require_answer_ready)
        attempts.append(attempt)
        fallback_trace.append(
            {
                "route": route,
                "ok": validation["ok"],
                "answer_ready": validation["answer_ready"],
                "warnings": validation["warnings"],
            }
        )
        final_validation = validation
        if validation["ok"]:
            break
        if index + 1 >= len(routes_to_try):
            break

    assert final_validation is not None
    final_validation = dict(final_validation)
    final_validation["fallback_used"] = len(attempts) > 1

    if (
        getattr(args, "enable_react_fallback", False)
        and args.mode == "execute"
        and not final_validation.get("answer_ready")
        and initial_route != "safe_refuse_or_verify"
    ):
        react_step = query_react_agent.run_react_agent(
            args,
            config=config,
            question=effective_question,
            understanding=understanding,
            router_payload=router_payload,
            prior_attempts=attempts,
        )
        react_attempt = {
            "route": "react_fallback",
            "question": effective_question,
            "profile": {
                "description": "ReAct fallback over schema, RAG, curated SQL tools, and guarded read-only SQL.",
                "rag": True,
                "sql": True,
            },
            "steps": [react_step],
        }
        react_validation = validate_attempt(react_attempt, require_answer_ready=args.require_answer_ready)
        attempts.append(react_attempt)
        fallback_trace.append(
            {
                "route": "react_fallback",
                "ok": react_validation["ok"],
                "answer_ready": react_validation["answer_ready"],
                "warnings": react_validation["warnings"],
            }
        )
        final_validation = dict(react_validation)
        final_validation["fallback_used"] = True

    answer_package = synthesize_answer_package(
        args=args,
        config=config,
        attempts=attempts,
        validation=final_validation,
        question=effective_question,
        understanding=understanding,
    )

    return {
        "ok": bool(final_validation["ok"]),
        "question": args.question,
        "effective_question": effective_question,
        "route": attempts[-1]["route"] if attempts else initial_route,
        "initial_route": initial_route,
        "query_understanding": understanding,
        "router": router_payload,
        "entity_resolution": router_payload.get("entity_resolution"),
        "steps": attempts[-1]["steps"] if attempts else [],
        "attempts": attempts,
        "validation": final_validation,
        "fallback_trace": fallback_trace,
        "answer_package": answer_package,
        "duration_ms": now_ms() - started,
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def print_pretty(payload: dict[str, Any]) -> None:
    if not payload.get("ok"):
        print(f"ERROR/WEAK: route={payload.get('route')} validation={payload.get('validation')}")
    else:
        print(f"route={payload.get('route')} initial={payload.get('initial_route', payload.get('route'))}")
    print(f"question={payload.get('effective_question')}")
    validation = payload.get("validation", {})
    print(
        f"validation ok={validation.get('ok')} answer_ready={validation.get('answer_ready')} "
        f"fallback_used={validation.get('fallback_used')}"
    )
    for item in payload.get("fallback_trace", []):
        print(f"fallback route={item['route']} ok={item['ok']} answer_ready={item['answer_ready']}")
    for step in payload.get("steps", []):
        if step.get("tool") == "query_rag":
            print(
                f"rag status={step.get('status')} mode={step.get('mode')} "
                f"results={step.get('result_count')} filters={step.get('filters')}"
            )
            for chunk in step.get("chunks", [])[:3]:
                print(f"  - {chunk.get('chunk_id')} score={chunk.get('score')} source={chunk.get('staging_source')}")
        elif step.get("tool") == "query_sql":
            print(f"sql status={step.get('status')} tables={step.get('tables_hint')}")
            if step.get("sql_tool_call"):
                print(f"  sql_tool={step['sql_tool_call']}")
            if step.get("sql") or step.get("generated_sql"):
                print(f"  sql={step.get('sql') or step.get('generated_sql')}")
            if step.get("rows") is not None:
                print(f"  rows={step.get('row_count')}")
            if step.get("schemas"):
                print(f"  schemas={[schema.get('table') for schema in step['schemas']]}")
            if step.get("message"):
                print(f"  {step['message']}")
        elif step.get("tool") == "query_react":
            print(f"react status={step.get('status')} rows={step.get('row_count')} citations={step.get('rag_citations', [])[:5]}")
            if step.get("sql"):
                print(f"  sql={step.get('sql')}")
            if step.get("answer_text"):
                print(f"  react_answer={step.get('answer_text')}")
            for item in step.get("trace", [])[:5]:
                action = item.get("action", {})
                print(f"  trace step={item.get('step')} status={item.get('status')} action={action.get('action') or action.get('tool')}")
    package = payload.get("answer_package", {})
    print(f"answer_ready={package.get('answer_ready')} citations={package.get('rag_citations', [])[:5]}")
    if package.get("answer_text"):
        print("answer:")
        print(package["answer_text"])
    for note in package.get("notes", []):
        print(f"note={note}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the post-guard FahMai query workflow.")
    parser.add_argument("--question", default=None)
    parser.add_argument("--sanitized-question", default=None)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--guard-json", type=Path, default=None)
    parser.add_argument("--guard-json-inline", default=None)
    parser.add_argument("--guard-json-b64", default=None)
    parser.add_argument("--enable-query-understanding", dest="enable_query_understanding", action="store_true", default=True)
    parser.add_argument("--disable-query-understanding", dest="enable_query_understanding", action="store_false")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--router-config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--router-mode", choices=["rules", "rules_model"], default="rules")
    parser.add_argument("--mode", choices=["plan", "execute"], default="execute")
    parser.add_argument("--sql", default=None)
    parser.add_argument("--sql-limit", type=int, default=50)
    parser.add_argument("--sample-rows", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=60)
    parser.add_argument("--snippet-chars", type=int, default=500)
    parser.add_argument("--wait-for-db-seconds", type=int, default=0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-path", type=Path, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--offline", dest="offline", action="store_true", default=True)
    parser.add_argument("--no-offline", dest="offline", action="store_false")
    parser.add_argument("--vector-weight", type=float, default=query_rag.DEFAULT_VECTOR_WEIGHT)
    parser.add_argument("--text-weight", type=float, default=query_rag.DEFAULT_TEXT_WEIGHT)
    parser.add_argument("--exact-weight", type=float, default=query_rag.DEFAULT_EXACT_WEIGHT)
    parser.add_argument("--source-weight", type=float, default=query_rag.DEFAULT_SOURCE_WEIGHT)
    parser.add_argument("--expand-entities", action="store_true", default=True)
    parser.add_argument("--no-expand-entities", dest="expand_entities", action="store_false")
    parser.add_argument("--llm-mode", choices=["none", "mock", "local_transformers", "openai_compatible"], default="none")
    parser.add_argument("--llm-api-base", default=None)
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--enable-sql-generation", action="store_true")
    parser.add_argument("--enable-answer-synthesis", action="store_true")
    parser.add_argument("--enable-sql-tools", dest="enable_sql_tools", action="store_true", default=True)
    parser.add_argument("--disable-sql-tools", dest="enable_sql_tools", action="store_false")
    parser.add_argument("--sql-tool-mode", choices=["deterministic", "agent", "deterministic_agent"], default="deterministic")
    parser.add_argument("--sql-tool-agent-max-attempts", type=int, default=2)
    parser.add_argument("--enable-react-fallback", action="store_true")
    parser.add_argument("--react-max-steps", type=int, default=6)
    parser.add_argument("--react-temperature", type=float, default=0.0)
    parser.add_argument("--react-max-tokens", type=int, default=900)
    parser.add_argument("--llm-sql-temperature", type=float, default=0.0)
    parser.add_argument("--llm-sql-max-tokens", type=int, default=512)
    parser.add_argument("--llm-answer-temperature", type=float, default=0.2)
    parser.add_argument("--llm-answer-max-tokens", type=int, default=1024)
    parser.add_argument("--llm-context-chunks", type=int, default=5)
    parser.add_argument("--require-answer-ready", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    if args.question is None:
        args.question = sys.stdin.read().strip()
    else:
        args.question = args.question.strip()
    if not args.question:
        parser.error("--question is required unless provided on stdin")
    if args.sql_limit < 1:
        parser.error("--sql-limit must be >= 1")
    if args.sample_rows < 0:
        parser.error("--sample-rows must be >= 0")
    if args.top_k < 1:
        parser.error("--top-k must be >= 1")
    if args.candidate_k < args.top_k:
        parser.error("--candidate-k must be >= --top-k")
    if args.llm_timeout <= 0:
        parser.error("--llm-timeout must be > 0")
    if args.llm_sql_max_tokens < 1 or args.llm_answer_max_tokens < 1:
        parser.error("--llm max token values must be >= 1")
    if args.llm_context_chunks < 0:
        parser.error("--llm-context-chunks must be >= 0")
    if args.sql_tool_agent_max_attempts < 1:
        parser.error("--sql-tool-agent-max-attempts must be >= 1")
    if args.react_max_steps < 1:
        parser.error("--react-max-steps must be >= 1")
    return args


def main() -> int:
    configure_stdio()
    args = parse_args()
    try:
        payload = run_orchestrator(args)
        if args.pretty:
            print_pretty(payload)
        else:
            print_json(payload)
        return 0 if payload.get("ok") else 2
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}
        if args.pretty:
            print_pretty(payload)
        else:
            print_json(payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
