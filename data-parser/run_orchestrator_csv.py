#!/usr/bin/env python3
"""
Run the FahMai post-guard orchestrator over a CSV of questions.

The output is JSONL so long runs can be resumed/inspected incrementally.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import query_orchestrator
import query_rag


DEFAULT_DATABASE = Path("data-parser/output/fahmai.duckdb")
DEFAULT_CONFIG = Path(__file__).with_name("router_config.json")
DEFAULT_OUTPUT = Path("data-parser/output/orchestrator_results.jsonl")


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def now_ms() -> int:
    return int(time.time() * 1000)


def read_questions(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "question" not in reader.fieldnames:
            raise ValueError(f"{path} must have a question column.")
        rows: list[dict[str, str]] = []
        for idx, row in enumerate(reader):
            question = (row.get("question") or "").strip()
            if not question:
                continue
            rows.append(
                {
                    "id": (row.get("id") or row.get("question_id") or f"row-{idx+1:04d}").strip(),
                    "question": question,
                }
            )
    return rows


def load_safety_pipeline(safety_route_dir: Path):
    src_dir = safety_route_dir / "src"
    if not src_dir.exists():
        raise FileNotFoundError(f"safety_route src directory not found: {src_dir}")
    src_text = str(src_dir.resolve())
    if src_text not in sys.path:
        sys.path.insert(0, src_text)
    from safety_route import SafetyPipeline  # type: ignore

    return SafetyPipeline()


def apply_input_guard(args: argparse.Namespace, row: dict[str, str], pipeline: Any | None) -> dict[str, str]:
    if pipeline is None:
        return row

    result = pipeline.run(row["question"])
    guard = result.to_dict()
    guarded = dict(row)
    guarded["guard_json_inline"] = json.dumps(guard, ensure_ascii=False)
    guarded["sanitized_question"] = guard.get("clean_question") or row["question"]
    guarded["guard_route_allowed"] = "true" if guard.get("route_allowed", True) else "false"
    guarded["guard_final_safe_response"] = guard.get("final_safe_response") or ""
    return guarded


def make_orchestrator_args(args: argparse.Namespace, row: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        question=row["question"],
        sanitized_question=row.get("sanitized_question") or None,
        question_id=row["id"],
        guard_json=None,
        guard_json_inline=row.get("guard_json_inline") or None,
        guard_json_b64=None,
        enable_query_understanding=args.enable_query_understanding,
        database=args.database,
        router_config=args.router_config,
        router_mode=args.router_mode,
        mode=args.mode,
        sql=None,
        sql_limit=args.sql_limit,
        sample_rows=args.sample_rows,
        top_k=args.top_k,
        candidate_k=args.candidate_k,
        snippet_chars=args.snippet_chars,
        wait_for_db_seconds=args.wait_for_db_seconds,
        model=args.model,
        model_path=args.model_path,
        device=args.device,
        offline=args.offline,
        vector_weight=args.vector_weight,
        text_weight=args.text_weight,
        exact_weight=args.exact_weight,
        source_weight=args.source_weight,
        expand_entities=args.expand_entities,
        llm_mode=args.llm_mode,
        llm_api_base=args.llm_api_base,
        llm_model=args.llm_model,
        llm_timeout=args.llm_timeout,
        enable_sql_generation=args.enable_sql_generation,
        enable_answer_synthesis=args.enable_answer_synthesis,
        enable_sql_tools=args.enable_sql_tools,
        sql_tool_mode=args.sql_tool_mode,
        sql_tool_agent_max_attempts=args.sql_tool_agent_max_attempts,
        enable_react_fallback=args.enable_react_fallback,
        react_max_steps=args.react_max_steps,
        react_temperature=args.react_temperature,
        react_max_tokens=args.react_max_tokens,
        llm_sql_temperature=args.llm_sql_temperature,
        llm_sql_max_tokens=args.llm_sql_max_tokens,
        llm_answer_temperature=args.llm_answer_temperature,
        llm_answer_max_tokens=args.llm_answer_max_tokens,
        llm_context_chunks=args.llm_context_chunks,
        require_answer_ready=args.require_answer_ready,
        pretty=False,
    )


def compact_payload(payload: dict[str, Any], *, include_full: bool) -> dict[str, Any]:
    if include_full:
        return payload
    steps = []
    for step in payload.get("steps", []):
        item = {
            "tool": step.get("tool"),
            "status": step.get("status"),
        }
        if step.get("tool") == "query_rag":
            item.update(
                {
                    "mode": step.get("mode"),
                    "filters": step.get("filters"),
                    "result_count": step.get("result_count"),
                    "chunk_ids": [chunk.get("chunk_id") for chunk in step.get("chunks", [])],
                }
            )
        if step.get("tool") == "query_sql":
            item.update(
                {
                    "tables_hint": step.get("tables_hint"),
                    "sql_tool_mode": step.get("sql_tool_mode"),
                    "row_count": step.get("row_count"),
                    "generated_sql": step.get("generated_sql"),
                    "sql": step.get("sql"),
                    "sql_tool_call": step.get("sql_tool_call"),
                    "agent_tool_trace": step.get("agent_tool_trace"),
                    "schemas": [schema.get("table") for schema in step.get("schemas", [])],
                    "message": step.get("message"),
                    "error_type": step.get("error_type"),
                }
            )
        if step.get("tool") == "query_react":
            item.update(
                {
                    "row_count": step.get("row_count"),
                    "answer_text": step.get("answer_text"),
                    "rag_citations": step.get("rag_citations"),
                    "sql": step.get("sql"),
                    "trace": step.get("trace"),
                    "message": step.get("message"),
                    "error_type": step.get("error_type"),
                }
            )
        steps.append(item)
    return {
        "ok": payload.get("ok"),
        "question_id": payload.get("router", {}).get("question_id"),
        "question": payload.get("question"),
        "query_understanding": payload.get("query_understanding"),
        "route": payload.get("route"),
        "initial_route": payload.get("initial_route"),
        "validation": payload.get("validation"),
        "fallback_trace": payload.get("fallback_trace"),
        "steps": steps,
        "answer_package": payload.get("answer_package"),
        "duration_ms": payload.get("duration_ms"),
    }


def run_batch(args: argparse.Namespace) -> dict[str, Any]:
    started = now_ms()
    rows = read_questions(args.questions_csv)
    if args.only_id:
        wanted = set(args.only_id)
        rows = [row for row in rows if row["id"] in wanted]
    if args.limit is not None:
        rows = rows[: args.limit]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    route_counts: Counter[str] = Counter()
    ok_counts: Counter[str] = Counter()
    guard_counts: Counter[str] = Counter()
    answer_ready = 0
    failures: list[dict[str, Any]] = []
    safety_pipeline = load_safety_pipeline(args.safety_route_dir) if args.enable_input_guard else None

    mode = "a" if args.append else "w"
    with args.output.open(mode, encoding="utf-8", newline="\n") as handle:
        for idx, row in enumerate(rows, start=1):
            item_started = now_ms()
            try:
                guarded_row = apply_input_guard(args, row, safety_pipeline)
                guard = json.loads(guarded_row["guard_json_inline"]) if guarded_row.get("guard_json_inline") else None
                if guard:
                    guard_counts["injection" if guard.get("is_injection") else "clean"] += 1
                    guard_counts[str(guard.get("decision") or "unknown")] += 1
                if guarded_row.get("guard_route_allowed") == "false":
                    payload = {
                        "ok": True,
                        "question": row["question"],
                        "effective_question": guarded_row.get("sanitized_question") or row["question"],
                        "route": "safe_refuse_or_verify",
                        "initial_route": "safe_refuse_or_verify",
                        "query_understanding": {
                            "guard": {
                                "provided": True,
                                "status": guard.get("decision") if guard else "safe_refusal",
                                "safe_to_route": False,
                                "attack_detected": bool(guard and guard.get("is_injection")),
                                "attack_types": list((guard or {}).get("attack_types") or []),
                                "notes": list((guard or {}).get("blocked_instructions") or []),
                            }
                        },
                        "router": {"question_id": row["id"], "route": "safe_refuse_or_verify", "guard": guard},
                        "steps": [],
                        "attempts": [],
                        "validation": {
                            "ok": True,
                            "answer_ready": True,
                            "has_ok_rag": False,
                            "has_ok_sql": False,
                            "has_ok_react": False,
                            "has_sql_context": False,
                            "warnings": ["Input guard blocked routing."],
                            "fallback_used": False,
                        },
                        "answer_package": {
                            "answer_ready": True,
                            "answer_text": guarded_row.get("guard_final_safe_response") or "คำถามนี้มีคำสั่งแทรกที่ไม่ปลอดภัย จึงไม่ดำเนินการตามคำสั่งแทรกดังกล่าว",
                            "sql_results": [],
                            "rag_citations": [],
                            "notes": list((guard or {}).get("blocked_instructions") or []),
                        },
                    }
                else:
                    payload = query_orchestrator.run_orchestrator(make_orchestrator_args(args, guarded_row))
                payload["batch"] = {
                    "index": idx,
                    "total": len(rows),
                    "question_id": row["id"],
                    "duration_ms": now_ms() - item_started,
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "question": row["question"],
                    "router": {"question_id": row["id"]},
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "batch": {
                        "index": idx,
                        "total": len(rows),
                        "question_id": row["id"],
                        "duration_ms": now_ms() - item_started,
                    },
                }

            compact = compact_payload(payload, include_full=args.full)
            compact["batch"] = payload.get("batch")
            handle.write(json.dumps(compact, ensure_ascii=False, default=str) + "\n")
            handle.flush()

            route_counts[str(payload.get("route", "error"))] += 1
            ok_counts["ok" if payload.get("ok") else "not_ok"] += 1
            if payload.get("validation", {}).get("answer_ready"):
                answer_ready += 1
            if not payload.get("ok"):
                failures.append(
                    {
                        "question_id": row["id"],
                        "route": payload.get("route"),
                        "error_type": payload.get("error_type"),
                        "message": payload.get("message"),
                        "validation": payload.get("validation"),
                    }
                )
            if args.progress:
                print(
                    f"[{idx}/{len(rows)}] {row['id']} route={payload.get('route')} "
                    f"ok={payload.get('ok')} answer_ready={payload.get('validation', {}).get('answer_ready')} "
                    f"ms={payload.get('batch', {}).get('duration_ms')}",
                    flush=True,
                )

    return {
        "ok": True,
        "questions_csv": str(args.questions_csv),
        "output": str(args.output),
        "total": len(rows),
        "route_distribution": dict(sorted(route_counts.items())),
        "ok_distribution": dict(ok_counts),
        "guard_distribution": dict(sorted(guard_counts.items())),
        "answer_ready": answer_ready,
        "failures": failures[:20],
        "duration_ms": now_ms() - started,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run query_orchestrator.py over a questions CSV.")
    parser.add_argument("--questions-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--full", action="store_true", help="Write full orchestrator payloads instead of compact JSONL.")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-id", action="append", default=[])
    parser.add_argument("--enable-input-guard", action="store_true", help="Run safety_route before routing each question.")
    parser.add_argument("--safety-route-dir", type=Path, default=Path("safety_route"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--router-config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--router-mode", choices=["rules", "rules_model"], default="rules")
    parser.add_argument("--enable-query-understanding", dest="enable_query_understanding", action="store_true", default=True)
    parser.add_argument("--disable-query-understanding", dest="enable_query_understanding", action="store_false")
    parser.add_argument("--mode", choices=["plan", "execute"], default="plan")
    parser.add_argument("--sql-limit", type=int, default=50)
    parser.add_argument("--sample-rows", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--candidate-k", type=int, default=30)
    parser.add_argument("--snippet-chars", type=int, default=360)
    parser.add_argument("--wait-for-db-seconds", type=int, default=0)
    parser.add_argument("--model", default=query_orchestrator.DEFAULT_MODEL)
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
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.candidate_k < args.top_k:
        parser.error("--candidate-k must be >= --top-k")
    if args.sql_tool_agent_max_attempts < 1:
        parser.error("--sql-tool-agent-max-attempts must be >= 1")
    if args.react_max_steps < 1:
        parser.error("--react-max-steps must be >= 1")
    return args


def main() -> int:
    configure_stdio()
    args = parse_args()
    try:
        payload = run_batch(args)
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
