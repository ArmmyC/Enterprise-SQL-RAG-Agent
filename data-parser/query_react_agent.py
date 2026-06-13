#!/usr/bin/env python3
"""
Guarded ReAct fallback agent for FahMai.

This is the slow, flexible fallback after deterministic/curated SQL tools miss.
The model may inspect schema, call allowlisted SQL tools, run guarded read-only
SQL, retrieve RAG chunks, and finally answer from observations.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import query_rag
import query_tools


DEFAULT_LLM_API_BASE = "http://x1000c2s2b0n0:8000/v1"
DEFAULT_LLM_MODEL = "qwen-local"
DATE_AXIS_CONVENTION = (
    "Date-axis convention for FACT_* period filters: when a question says in year/month/quarter/date range "
    "without explicitly naming a date column, filter by business_event_date. Use posting_date only when the "
    "question explicitly asks about posting_date, accounting posting, GL cycles, month-end close, or accounting cutoffs. "
    "Use effective_date only when the question says effective. Use as_of_date only when the question says as-of. "
    "FACT_VENDOR_PAYMENT is the important exception where posting_date often lags business_event_date."
)
BLOCKED_SQL_WORDS = [
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


class ReactAgentError(Exception):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def connect_duckdb(database: Path):
    import duckdb

    return duckdb.connect(str(database), read_only=True)


def fetch_dicts(con: Any, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cursor = con.execute(sql, params or [])
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def reject_unsafe_sql(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", stripped):
        raise ReactAgentError("Only read-only SELECT/WITH SQL is allowed.")
    for word in BLOCKED_SQL_WORDS:
        if re.search(rf"(?is)\b{word}\b", stripped):
            raise ReactAgentError(f"Unsafe SQL keyword is not allowed: {word}")


def run_readonly_sql(database: Path, sql: str, limit: int) -> dict[str, Any]:
    reject_unsafe_sql(sql)
    limited_sql = sql.strip().rstrip(";")
    con = connect_duckdb(database)
    try:
        rows = fetch_dicts(con, f"SELECT * FROM ({limited_sql}) AS react_query LIMIT ?", [limit])
        return {"sql": sql, "rows": rows, "row_count": len(rows)}
    finally:
        con.close()


def quote_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ReactAgentError(f"Unsafe identifier: {name}")
    return '"' + name.replace('"', '""') + '"'


def schema_action(database: Path, tables: list[str] | None = None, sample_rows: int = 2) -> dict[str, Any]:
    con = connect_duckdb(database)
    try:
        available = fetch_dicts(
            con,
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            ORDER BY table_name
            """,
        )
        table_map = {row["table_name"].casefold(): row["table_name"] for row in available}
        if not tables:
            return {"tables": [row["table_name"] for row in available], "table_count": len(available)}

        schemas: list[dict[str, Any]] = []
        missing: list[str] = []
        for table in tables:
            actual = table_map.get(str(table).casefold())
            if not actual:
                missing.append(str(table))
                continue
            columns = fetch_dicts(
                con,
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = ?
                ORDER BY ordinal_position
                """,
                [actual],
            )
            count = con.execute(f"SELECT COUNT(*) FROM {quote_identifier(actual)}").fetchone()[0]
            samples = fetch_dicts(con, f"SELECT * FROM {quote_identifier(actual)} LIMIT ?", [sample_rows])
            schemas.append({"table": actual, "row_count": int(count), "columns": columns, "sample_rows": samples})
        return {"schemas": schemas, "missing_tables": missing}
    finally:
        con.close()


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


def extract_json_object(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    candidate = (fenced.group(1) if fenced else text).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        balanced = first_balanced_json_object(candidate)
        if not balanced:
            raise
        data = json.loads(balanced)
    if not isinstance(data, dict):
        raise ReactAgentError("LLM ReAct output must be a JSON object.")
    return data


def llm_settings(args: SimpleNamespace, config: dict[str, Any]) -> dict[str, Any]:
    llm_cfg = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    compatible = llm_cfg.get("openai_compatible", {}) if isinstance(llm_cfg.get("openai_compatible"), dict) else {}
    return {
        "api_base": args.llm_api_base or compatible.get("api_base") or DEFAULT_LLM_API_BASE,
        "model": args.llm_model or compatible.get("model") or DEFAULT_LLM_MODEL,
        "timeout": float(args.llm_timeout),
    }


def chat_completion(
    *,
    args: SimpleNamespace,
    config: dict[str, Any],
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    settings = llm_settings(args, config)
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
        with urllib.request.urlopen(request, timeout=settings["timeout"]) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ReactAgentError(f"LLM HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise ReactAgentError(f"LLM connection failed: {exc}") from exc
    data = json.loads(body)
    choices = data.get("choices") or []
    if not choices:
        raise ReactAgentError("LLM returned no choices.")
    content = str(choices[0].get("message", {}).get("content", "")).strip()
    if not content:
        raise ReactAgentError("LLM returned empty content.")
    return {"content": content, "settings": settings, "raw": data}


def compact_tool_catalog(max_tools: int = 80) -> list[dict[str, Any]]:
    preferred = [
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
    for name in preferred:
        if name in by_name:
            ordered.append(by_name.pop(name))
    ordered.extend(sorted(by_name.values(), key=lambda item: item["name"]))
    return ordered[:max_tools]


def compact_observation(value: Any, max_chars: int = 3500) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"


def make_rag_args(args: SimpleNamespace, *, query: str, staging_source: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        database=args.database,
        query=query,
        top_k=min(args.top_k, 8),
        candidate_k=max(args.candidate_k, 80),
        mode="hybrid",
        staging_source=staging_source or [],
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


def dispatch_action(args: SimpleNamespace, action: dict[str, Any]) -> dict[str, Any]:
    name = str(action.get("action") or action.get("tool") or "").strip()
    payload = action.get("args") if isinstance(action.get("args"), dict) else action

    if name == "schema":
        tables = payload.get("tables")
        if isinstance(tables, str):
            tables = [tables]
        return {"ok": True, "observation": schema_action(args.database, tables=tables or None, sample_rows=args.sample_rows)}

    if name == "sql_tool":
        tool_name = payload.get("tool_name") or payload.get("name")
        tool_args = payload.get("tool_args") or payload.get("arguments") or payload.get("args") or {}
        if not isinstance(tool_args, dict):
            raise ReactAgentError("sql_tool args must be an object.")
        if tool_name not in query_tools.TOOL_FUNCS:
            raise ReactAgentError(f"Unknown SQL tool: {tool_name}")
        call = query_tools.ToolCall(name=str(tool_name), args=tool_args, reason=str(action.get("reason") or "ReAct tool call"), confidence=0.5)
        return {"ok": True, "observation": query_tools.execute_tool(args.database, call)}

    if name == "run_sql":
        sql = str(payload.get("sql") or "")
        if not sql:
            raise ReactAgentError("run_sql requires sql.")
        return {"ok": True, "observation": run_readonly_sql(args.database, sql, args.sql_limit)}

    if name == "query_rag":
        query = str(payload.get("query") or "")
        if not query:
            raise ReactAgentError("query_rag requires query.")
        sources = payload.get("staging_source") or payload.get("sources") or []
        if isinstance(sources, str):
            sources = [sources]
        result = query_rag.run_query(make_rag_args(args, query=query, staging_source=list(sources)))
        return {"ok": True, "observation": {"results": result.get("results", []), "retrieval": result.get("retrieval", {})}}

    if name == "final":
        answer = str(payload.get("answer") or action.get("answer") or "")
        return {"ok": bool(answer.strip()), "final": answer.strip(), "observation": {"answer": answer.strip()}}

    raise ReactAgentError(f"Unsupported ReAct action: {name}")


def compact_prior_attempts(prior_attempts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for attempt in (prior_attempts or [])[-3:]:
        steps = []
        for step in attempt.get("steps", []):
            item = {
                "tool": step.get("tool"),
                "status": step.get("status"),
                "message": step.get("message"),
            }
            if step.get("tool") == "query_sql":
                item["sql_tool_call"] = step.get("sql_tool_call")
                item["row_count"] = step.get("row_count")
                item["rows_preview"] = (step.get("rows") or [])[:3]
            if step.get("tool") == "query_rag":
                item["result_count"] = step.get("result_count")
                item["chunk_ids"] = [chunk.get("chunk_id") for chunk in step.get("chunks", [])[:5]]
            steps.append(item)
        out.append({"route": attempt.get("route"), "steps": steps})
    return out


def run_react_agent(
    args: SimpleNamespace,
    *,
    config: dict[str, Any],
    question: str,
    understanding: dict[str, Any] | None,
    router_payload: dict[str, Any] | None,
    prior_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = now_ms()
    if args.llm_mode != "openai_compatible":
        return {
            "tool": "query_react",
            "status": "disabled",
            "message": "ReAct fallback requires --llm-mode openai_compatible.",
            "duration_ms": now_ms() - started,
        }

    trace: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    messages = [
        {
            "role": "system",
            "content": (
                "You are a guarded FahMai ReAct fallback agent. Use one action at a time. "
                "Return JSON only. Allowed actions: "
                "{\"action\":\"schema\",\"args\":{\"tables\":[...]}}; "
                "{\"action\":\"sql_tool\",\"args\":{\"tool_name\":\"...\",\"tool_args\":{...}}}; "
                "{\"action\":\"run_sql\",\"args\":{\"sql\":\"SELECT ...\"}}; "
                "{\"action\":\"query_rag\",\"args\":{\"query\":\"...\",\"staging_source\":[...]}}; "
                "{\"action\":\"final\",\"args\":{\"answer\":\"...\"}}. "
                "Use only read-only SELECT/WITH SQL. Do not invent data. If evidence is insufficient, final answer must say so. "
                "Prefer curated SQL tools when they directly answer the question; use run_sql only when no tool fits. "
                f"{DATE_AXIS_CONVENTION}"
            ),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    f"Question: {question}",
                    f"Query understanding JSON: {json.dumps(understanding or {}, ensure_ascii=False, default=str)}",
                    f"Router JSON: {json.dumps(router_payload or {}, ensure_ascii=False, default=str)}",
                    f"Prior attempts JSON: {json.dumps(compact_prior_attempts(prior_attempts), ensure_ascii=False, default=str)}",
                    f"SQL tool catalog JSON: {json.dumps(compact_tool_catalog(), ensure_ascii=False)}",
                    "Pick the next single action.",
                ]
            ),
        },
    ]

    final_answer = ""
    max_steps = max(1, int(args.react_max_steps))
    for step_idx in range(1, max_steps + 1):
        try:
            response = chat_completion(
                args=args,
                config=config,
                messages=messages,
                temperature=args.react_temperature,
                max_tokens=args.react_max_tokens,
            )
            action = extract_json_object(response["content"])
            dispatched = dispatch_action(args, action)
            observation = dispatched.get("observation")
            trace_item = {
                "step": step_idx,
                "action": action,
                "llm_response": response["content"],
                "observation": observation,
                "status": "ok" if dispatched.get("ok") else "weak",
            }
            trace.append(trace_item)
            observations.append({"action": action, "observation": observation})
            if "final" in dispatched:
                final_answer = dispatched["final"]
                break
            messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False, default=str)})
            messages.append(
                {
                    "role": "user",
                    "content": "Observation JSON:\n"
                    + compact_observation(observation)
                    + "\n\nChoose the next action. If the observation has enough evidence, use final.",
                }
            )
        except Exception as exc:
            trace.append({"step": step_idx, "status": "error", "error_type": type(exc).__name__, "message": str(exc)})
            messages.append(
                {
                    "role": "user",
                    "content": f"The previous action failed with {type(exc).__name__}: {exc}. Choose a different valid action.",
                }
            )

    rows: list[dict[str, Any]] = []
    rag_chunks: list[dict[str, Any]] = []
    sql_statements: list[str] = []
    for item in observations:
        obs = item.get("observation")
        if not isinstance(obs, dict):
            continue
        if "rows" in obs:
            rows = obs.get("rows") or rows
            if obs.get("sql"):
                sql_statements.append(str(obs["sql"]))
        if obs.get("results"):
            rag_chunks = obs.get("results") or rag_chunks
        if obs.get("tool_call") and obs.get("rows") is not None:
            rows = obs.get("rows") or rows
            if obs.get("sql"):
                sql_statements.append(str(obs["sql"]))

    status = "ok" if final_answer or rows or rag_chunks else "weak"
    return {
        "tool": "query_react",
        "status": status,
        "answer_text": final_answer or None,
        "rows": rows,
        "row_count": len(rows),
        "rag_chunks": rag_chunks,
        "rag_citations": [chunk.get("chunk_id") for chunk in rag_chunks if chunk.get("chunk_id")],
        "sql": sql_statements[-1] if sql_statements else None,
        "trace": trace,
        "duration_ms": now_ms() - started,
    }
