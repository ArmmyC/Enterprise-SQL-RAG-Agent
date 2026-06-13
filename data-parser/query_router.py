#!/usr/bin/env python3
"""
Route FahMai questions to SQL, RAG, hybrid, or safety handling.

This is intentionally deterministic-first. A model-router config is accepted
but disabled by default until the model/provider decision is made.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from entity_resolver import resolve_entities


DEFAULT_CONFIG = Path(__file__).with_name("router_config.json")
DEFAULT_CONFIDENCE = 0.5


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


def load_guard_payload(args: argparse.Namespace) -> dict[str, Any] | None:
    guard_args = [bool(args.guard_json), bool(args.guard_json_inline), bool(args.guard_json_b64)]
    if sum(guard_args) > 1:
        raise ValueError("Use only one of --guard-json, --guard-json-inline, or --guard-json-b64.")
    if args.guard_json:
        return load_json(args.guard_json)
    if args.guard_json_inline:
        return json.loads(args.guard_json_inline)
    if args.guard_json_b64:
        decoded = base64.b64decode(args.guard_json_b64).decode("utf-8")
        return json.loads(decoded)
    return None


def guard_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        folded = value.casefold().strip()
        if folded in {"true", "1", "yes", "y"}:
            return True
        if folded in {"false", "0", "no", "n"}:
            return False
    return bool(value)


def normalize_guard(raw_guard: dict[str, Any] | None, *, sanitized_question: str | None = None) -> dict[str, Any] | None:
    if raw_guard is None and sanitized_question is None:
        return None

    guard = raw_guard or {}
    if guard.get("source") == "input_guard" and "safe_to_route" in guard:
        out = dict(guard)
        if sanitized_question:
            out["sanitized_question"] = sanitized_question
        return out

    nested = guard.get("guard") if isinstance(guard.get("guard"), dict) else {}
    source = nested or guard

    status = source.get("status") or source.get("decision") or ("cleaned" if sanitized_question else "provided")
    attack_detected = guard_bool(
        source.get("attack_detected", source.get("injection_like", source.get("is_attack")))
    )
    safe_to_route = guard_bool(
        source.get("safe_to_route", source.get("safe_to_answer", source.get("allow_route")))
    )
    if safe_to_route is None:
        safe_to_route = str(status).casefold() not in {"blocked", "deny", "refuse", "unsafe"}

    attack_types = source.get("attack_types", source.get("attack_type", []))
    if isinstance(attack_types, str):
        attack_types = [attack_types]
    elif attack_types is None:
        attack_types = []
    else:
        attack_types = list(attack_types)

    notes = source.get("notes", source.get("reasons", []))
    if isinstance(notes, str):
        notes = [notes]
    elif notes is None:
        notes = []
    else:
        notes = list(notes)

    candidate_sanitized = sanitized_question or source.get("sanitized_question") or source.get("clean_question")
    if not candidate_sanitized and isinstance(guard.get("question"), str):
        candidate_sanitized = guard["question"]

    return {
        "source": "input_guard",
        "status": status,
        "attack_detected": bool(attack_detected),
        "attack_types": attack_types,
        "safe_to_route": bool(safe_to_route),
        "sanitized_question": candidate_sanitized,
        "notes": notes,
        "raw": guard if raw_guard is not None else None,
    }


def lower_text(value: str) -> str:
    return value.casefold()


def contains_any(text: str, terms: list[str]) -> list[str]:
    folded = lower_text(text)
    hits: list[str] = []
    seen: set[str] = set()
    for term in terms:
        needle = lower_text(term)
        if needle and needle in folded and term not in seen:
            hits.append(term)
            seen.add(term)
    return hits


def find_tables(question: str) -> list[str]:
    tables = re.findall(r"\b(?:FACT|DIM|dim|vw|VW)_[A-Za-z0-9_]+\b", question)
    out: list[str] = []
    seen: set[str] = set()
    for table in tables:
        normalized = table if table.startswith("vw_") else table.upper()
        if normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    return out


def find_sku_or_ids(question: str) -> list[str]:
    pattern = r"\b[A-Z]{1,5}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b"
    ids = re.findall(pattern, question)
    out: list[str] = []
    seen: set[str] = set()
    for value in ids:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def count_multi_part_requirements(question: str) -> int:
    numbered = len(re.findall(r"\(\d+\)", question))
    thai_markers = len(re.findall(r"ข้อ\s*\d+", question))
    return max(numbered, thai_markers)


def score_hits(hits: list[str], weight: float, cap: float | None = None) -> float:
    score = len(hits) * weight
    return min(score, cap) if cap is not None else score


def clamp_confidence(score: float, *, floor: float = DEFAULT_CONFIDENCE) -> float:
    if score <= 0:
        return floor
    return round(min(0.98, floor + (score / 14.0)), 3)


def choose_rag_filter(
    config: dict[str, Any],
    *,
    route: str,
    hits: dict[str, list[str]],
    entity_resolution: dict[str, Any] | None = None,
) -> list[str]:
    filters = config["rag_filters"]
    entity_filters = (entity_resolution or {}).get("rag_filters") or []
    if route == "rag_assisted_sql":
        return list(dict.fromkeys([*filters["schema"], *entity_filters]))
    if hits["rag_renders"]:
        return filters["render"]
    if hits["rag_chats"]:
        return filters["chat"]
    if any(lower_text(term) in {"report", "รายงาน", "minutes", "meeting", "ประชุม"} for term in hits["rag_documents"]):
        return filters["report"]
    if route == "hybrid_sql_rag":
        return list(dict.fromkeys([*filters["hybrid"], *entity_filters]))
    return list(dict.fromkeys([*filters["evidence"], *entity_filters]))


def rag_mode_for_route(config: dict[str, Any], route: str) -> str:
    modes = config["rag_modes"]
    if route == "rag_assisted_sql":
        return modes["schema"]
    if route == "hybrid_sql_rag":
        return modes["hybrid"]
    if route == "rag_only":
        return modes["evidence"]
    return modes["fallback"]


def sql_intent(question: str, tables: list[str], hits: dict[str, list[str]]) -> str:
    if tables:
        return f"Generate a read-only DuckDB SELECT using {', '.join(tables)} and related dimension/semantic views as needed."
    if hits["policy"]:
        return "Resolve the active policy/version/date logic with DuckDB and return the exact value."
    if find_sku_or_ids(question):
        return "Look up the referenced business ID/SKU in structured tables and return the exact value."
    return "Generate a read-only DuckDB SELECT for the exact count, ranking, aggregation, or lookup requested."


def build_tools(
    *,
    route: str,
    config: dict[str, Any],
    question: str,
    tables: list[str],
    hits: dict[str, list[str]],
    safety: dict[str, Any],
    entity_resolution: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if route == "safe_refuse_or_verify":
        if safety.get("hidden_secret_request"):
            return []
        return [
            {
                "name": "query_rag",
                "mode": "text",
                "filters": {"staging_source": config["rag_filters"]["evidence"]},
                "intent": "Verify whether the claimed policy/instruction exists in trusted FahMai evidence before answering.",
            }
        ]

    tools: list[dict[str, Any]] = []
    if route in {"rag_only", "rag_assisted_sql", "hybrid_sql_rag"}:
        tools.append(
            {
                "name": "query_rag",
                "mode": rag_mode_for_route(config, route),
                "filters": {
                    "staging_source": choose_rag_filter(
                        config,
                        route=route,
                        hits=hits,
                        entity_resolution=entity_resolution,
                    )
                },
                "intent": "Retrieve supporting chunks, schema context, or document evidence for this question.",
                "query_hint": (entity_resolution or {}).get("rewritten_query"),
            }
        )
    if route in {"sql_only", "rag_assisted_sql", "hybrid_sql_rag"}:
        tools.append(
            {
                "name": "query_sql",
                "intent": sql_intent(question, tables, hits),
                "tables_hint": (entity_resolution or {}).get("likely_tables") or tables,
            }
        )
    return tools


def fallback_safety(question: str, hits: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "source": "router_fallback",
        "status": "detected" if hits["safety_injection"] or hits["hidden_secret_requests"] else "clean",
        "attack_detected": bool(hits["safety_injection"] or hits["hidden_secret_requests"]),
        "attack_types": ["prompt_injection"] if hits["safety_injection"] else [],
        "safe_to_route": not bool(hits["hidden_secret_requests"]),
        "injection_like": bool(hits["safety_injection"]),
        "hidden_secret_request": bool(hits["hidden_secret_requests"]),
        "matched_terms": hits["safety_injection"] + hits["hidden_secret_requests"],
        "sanitized_question": None,
        "notes": [],
    }


def route_question(
    question: str,
    *,
    question_id: str | None,
    config: dict[str, Any],
    mode: str,
    guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_question = question
    guard_info = normalize_guard(guard)
    if guard_info and guard_info.get("sanitized_question"):
        question = str(guard_info["sanitized_question"]).strip()

    weights = config["weights"]
    kw = config["keywords"]
    thresholds = config["thresholds"]

    entity_resolution = resolve_entities(question, config.get("entity_resolver"))
    analysis_question = str(entity_resolution.get("rewritten_query") or question)

    tables = find_tables(analysis_question)
    for table in entity_resolution.get("likely_tables", []):
        if table not in tables:
            tables.append(table)
    ids = find_sku_or_ids(question)
    views = [table for table in tables if table.startswith("vw_")]

    hits: dict[str, list[str]] = {
        "sql_metrics": contains_any(analysis_question, kw["sql_metrics"]),
        "sql_aggregations": contains_any(analysis_question, kw["sql_aggregations"]),
        "sql_ranking": contains_any(analysis_question, kw["sql_ranking"]),
        "sql_columns": contains_any(analysis_question, kw["sql_columns"]),
        "sql_lookup_entities": contains_any(analysis_question, kw.get("sql_lookup_entities", [])),
        "date_filters": contains_any(analysis_question, kw["date_filters"]),
        "rag_documents": contains_any(analysis_question, kw["rag_documents"]),
        "rag_chats": contains_any(analysis_question, kw["rag_chats"]),
        "rag_renders": contains_any(analysis_question, kw["rag_renders"]),
        "rag_evidence": contains_any(analysis_question, kw["rag_evidence"]),
        "policy": contains_any(analysis_question, kw["policy"]),
        "hybrid": contains_any(analysis_question, kw["hybrid"]),
        "safety_injection": contains_any(question, kw["safety_injection"]),
        "hidden_secret_requests": contains_any(question, kw["hidden_secret_requests"]),
    }

    multi_part_count = count_multi_part_requirements(question)
    sql_score = 0.0
    sql_score += score_hits(tables, weights["explicit_table"], cap=9.0)
    sql_score += score_hits(views, weights["explicit_view"], cap=5.0)
    sql_score += score_hits(hits["sql_columns"], weights["explicit_column"], cap=7.0)
    sql_score += score_hits(hits["sql_lookup_entities"], weights["explicit_column"], cap=4.0)
    sql_score += score_hits(hits["sql_metrics"], weights["sql_metric"], cap=5.0)
    sql_score += score_hits(hits["sql_aggregations"], weights["sql_aggregation"], cap=4.0)
    sql_score += score_hits(hits["sql_ranking"], weights["sql_ranking"], cap=4.0)
    sql_score += score_hits(hits["date_filters"], weights["date_filter"], cap=3.0)
    sql_score += score_hits(entity_resolution.get("likely_tables", []), weights.get("entity_sql_table", 1.2), cap=4.0)
    if ids and any(metric in lower_text(question) for metric in ["msrp", "ราคา", "warranty", "รับประกัน"]):
        sql_score += 2.0

    rag_score = 0.0
    rag_score += score_hits(hits["rag_documents"], weights["rag_document"], cap=6.0)
    rag_score += score_hits(hits["rag_chats"], weights["rag_chat"], cap=6.0)
    rag_score += score_hits(hits["rag_renders"], weights["rag_render"], cap=4.0)
    rag_score += score_hits(hits["rag_evidence"], weights["rag_evidence"], cap=4.0)

    policy_score = score_hits(hits["policy"], weights["policy"], cap=7.0)

    hybrid_score = score_hits(hits["hybrid"], weights["hybrid"], cap=7.0)
    route_biases = set(entity_resolution.get("route_biases", []))
    if "sql" in route_biases:
        sql_score += weights.get("entity_route_bias", 1.0)
    if "rag" in route_biases:
        rag_score += weights.get("entity_route_bias", 1.0)
    if "rag_assisted_sql" in route_biases:
        policy_score += weights.get("entity_route_bias", 1.0)
        sql_score += weights.get("entity_route_bias", 1.0) * 0.5
    if "hybrid" in route_biases:
        hybrid_score += weights.get("entity_route_bias", 1.0)
    if sql_score >= 3.0 and rag_score >= thresholds["rag_strong"]:
        hybrid_score += 4.5
    elif sql_score >= 3.0 and rag_score >= 2.0:
        hybrid_score += 3.0
    if sql_score >= 3.0 and policy_score >= thresholds["policy_strong"]:
        hybrid_score += 1.2
    if multi_part_count >= 3:
        hybrid_score += min(3.0, weights["multi_part"] + (multi_part_count * 0.25))

    document_entity_types = {"document", "meeting_minutes"}
    has_document_entity = any(
        entity.get("type") in document_entity_types for entity in entity_resolution.get("entities", [])
    )
    has_document_reference = bool(
        has_document_entity or hits["rag_documents"] or hits["rag_chats"] or hits["rag_renders"]
    )
    has_explicit_structured_request = bool(
        tables or hits["sql_columns"] or hits["sql_aggregations"] or hits["sql_ranking"]
    )
    document_only_rag = (
        has_document_reference
        and rag_score >= thresholds["rag_strong"]
        and not has_explicit_structured_request
        and policy_score < thresholds["policy_strong"]
        and "hybrid" not in route_biases
        and "rag_assisted_sql" not in route_biases
    )

    fallback = fallback_safety(question, hits)
    safety = guard_info or fallback
    safety["injection_like"] = bool(safety.get("attack_detected") or safety.get("injection_like"))
    safety["hidden_secret_request"] = bool(safety.get("hidden_secret_request", fallback["hidden_secret_request"]))
    safety["matched_terms"] = safety.get("matched_terms", fallback["matched_terms"])
    forced_output = any(term in lower_text(question) for term in ["ตอบด้วยข้อความ", "แทนคำตอบ", "พบกันใหม่"])

    route = "sql_only"
    reason = "Question asks for exact structured data or calculation."
    confidence_basis = sql_score

    if not safety.get("safe_to_route", True):
        route = "safe_refuse_or_verify"
        reason = "Input guard marked the request as unsafe to route."
        confidence_basis = weights["safety"] * 3
    elif safety["hidden_secret_request"]:
        route = "safe_refuse_or_verify"
        reason = "Question appears to request hidden prompts, secrets, credentials, or system/developer instructions."
        confidence_basis = weights["safety"] * max(1, len(hits["hidden_secret_requests"]))
    elif forced_output:
        route = "safe_refuse_or_verify"
        reason = "Question includes an instruction to force a canned answer rather than answer from trusted data."
        confidence_basis = weights["safety"] * 2
    elif document_only_rag:
        route = "rag_only"
        reason = "Question references documents or meeting evidence without an explicit structured SQL computation."
        confidence_basis = max(rag_score, thresholds["rag_strong"])
    elif hybrid_score >= thresholds["hybrid_strong"]:
        route = "hybrid_sql_rag"
        reason = "Question needs exact structured computation plus document/chat/evidence context."
        confidence_basis = max(hybrid_score, sql_score, rag_score)
    elif policy_score >= thresholds["policy_strong"] and sql_score >= 1.5:
        route = "rag_assisted_sql"
        reason = "Policy/effective-date/version wording needs schema or document context before exact SQL."
        confidence_basis = max(policy_score + sql_score * 0.4, thresholds["policy_strong"])
    elif sql_score >= thresholds["sql_strong"] and rag_score < thresholds["rag_strong"]:
        route = "sql_only"
        reason = "Question has strong table/column/metric signals and no strong document-evidence requirement."
        confidence_basis = sql_score
    elif rag_score >= thresholds["rag_strong"] and sql_score < thresholds["sql_strong"]:
        route = "rag_only"
        reason = "Question asks for text evidence from documents, chats, reports, OCR, or rendered artifacts."
        confidence_basis = rag_score
    elif policy_score >= thresholds["policy_strong"]:
        route = "rag_assisted_sql"
        reason = "Policy/version terms suggest RAG schema context followed by exact SQL."
        confidence_basis = policy_score
    elif rag_score > sql_score:
        route = "rag_only"
        reason = "Document/chat evidence signals are stronger than structured SQL signals."
        confidence_basis = rag_score

    if mode == "rules_model" and config.get("model_router", {}).get("enabled"):
        model_cfg = config["model_router"]
        if clamp_confidence(confidence_basis) < float(model_cfg["fallback_when_confidence_below"]):
            reason += " Model fallback is configured but not implemented in this deterministic router CLI."

    all_signals: list[str] = []
    for key, values in hits.items():
        if key in {"hidden_secret_requests"}:
            continue
        all_signals.extend([f"{key}:{value}" for value in values])
    all_signals.extend([f"table:{table}" for table in tables])
    all_signals.extend([f"id:{value}" for value in ids])
    for entity in entity_resolution.get("entities", []):
        all_signals.append(f"entity:{entity.get('type')}:{entity.get('raw')}")
    for term_match in entity_resolution.get("term_matches", []):
        all_signals.append(f"alias:{term_match.get('canonical')}")
    if multi_part_count:
        all_signals.append(f"multi_part:{multi_part_count}")

    confidence = clamp_confidence(confidence_basis)
    tools = build_tools(
        route=route,
        config=config,
        question=question,
        tables=tables,
        hits=hits,
        safety=safety,
        entity_resolution=entity_resolution,
    )

    return {
        "ok": True,
        "question_id": question_id,
        "question": question,
        "raw_question": raw_question if raw_question != question else None,
        "route": route,
        "confidence": confidence,
        "reason": reason,
        "signals": all_signals,
        "entity_resolution": entity_resolution,
        "scores": {
            "sql": round(sql_score, 3),
            "rag": round(rag_score, 3),
            "policy": round(policy_score, 3),
            "hybrid": round(hybrid_score, 3),
        },
        "safety": safety,
        "tools": tools,
        "model_router": {
            "requested_mode": mode,
            "enabled": bool(config.get("model_router", {}).get("enabled")),
            "provider": config.get("model_router", {}).get("provider"),
            "model": config.get("model_router", {}).get("model"),
        },
    }


def route_csv(
    path: Path,
    *,
    config: dict[str, Any],
    mode: str,
    limit: int | None = None,
    only_ids: set[str] | None = None,
    use_guard_columns: bool = False,
) -> dict[str, Any]:
    started = now_ms()
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for idx, row in enumerate(reader):
            question_id = row.get("id") or row.get("question_id") or None
            if only_ids and question_id not in only_ids:
                continue
            if limit is not None and len(rows) >= limit:
                break
            question = row.get("question") or ""
            guard = None
            if use_guard_columns:
                guard = normalize_guard(
                    {
                        "status": row.get("guard_status"),
                        "attack_detected": row.get("attack_detected"),
                        "attack_types": [part.strip() for part in (row.get("attack_types") or "").split(",") if part.strip()],
                        "safe_to_route": row.get("safe_to_route"),
                        "notes": row.get("guard_notes"),
                    },
                    sanitized_question=row.get("sanitized_question") or None,
                )
            rows.append(route_question(question, question_id=question_id, config=config, mode=mode, guard=guard))

    route_counts = Counter(row["route"] for row in rows)
    bucket_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        question_id = row.get("question_id") or ""
        parts = question_id.split("-")
        bucket = parts[2] if len(parts) >= 3 else "UNKNOWN"
        bucket_counts[bucket][row["route"]] += 1

    return {
        "ok": True,
        "questions_csv": str(path),
        "total": len(rows),
        "route_distribution": dict(sorted(route_counts.items())),
        "bucket_distribution": {bucket: dict(counts) for bucket, counts in sorted(bucket_counts.items())},
        "rows": rows,
        "duration_ms": now_ms() - started,
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def print_pretty(payload: dict[str, Any]) -> None:
    if not payload.get("ok"):
        print(f"ERROR: {payload.get('message')}")
        return
    if "rows" in payload:
        print(f"Routed {payload['total']} questions from {payload['questions_csv']}")
        print("Route distribution:")
        for route, count in payload["route_distribution"].items():
            print(f"  {route}: {count}")
        print("Bucket distribution:")
        for bucket, counts in payload["bucket_distribution"].items():
            joined = ", ".join(f"{route}={count}" for route, count in counts.items())
            print(f"  {bucket}: {joined}")
        return

    print(f"{payload.get('question_id') or '-'} -> {payload['route']} confidence={payload['confidence']}")
    print(payload["reason"])
    print(f"scores={payload['scores']}")
    entity_resolution = payload.get("entity_resolution") or {}
    if entity_resolution.get("entities") or entity_resolution.get("term_matches"):
        print(f"entities={json.dumps(entity_resolution, ensure_ascii=False)}")
    if payload["safety"]["injection_like"] or payload["safety"]["hidden_secret_request"]:
        print(f"safety={payload['safety']}")
    print("tools:")
    for tool in payload["tools"]:
        print(f"  - {tool}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Route FahMai benchmark questions to SQL/RAG/hybrid tools.")
    parser.add_argument("--question", default=None)
    parser.add_argument("--sanitized-question", default=None)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--questions-csv", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--guard-json", type=Path, default=None)
    parser.add_argument("--guard-json-inline", default=None)
    parser.add_argument("--guard-json-b64", default=None)
    parser.add_argument("--use-guard-columns", action="store_true")
    parser.add_argument("--mode", choices=["rules", "rules_model"], default="rules")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only-id", action="append", default=[])
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    if args.questions_csv is None:
        if args.question is None:
            args.question = sys.stdin.read().strip()
        else:
            args.question = args.question.strip()
        if not args.question:
            parser.error("--question or --questions-csv is required")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    return args


def main() -> int:
    configure_stdio()
    args = parse_args()
    try:
        config = load_json(args.config)
        guard_payload = load_guard_payload(args)
        guard = normalize_guard(guard_payload, sanitized_question=args.sanitized_question)
        if args.questions_csv:
            payload = route_csv(
                args.questions_csv,
                config=config,
                mode=args.mode,
                limit=args.limit,
                only_ids=set(args.only_id) if args.only_id else None,
                use_guard_columns=args.use_guard_columns,
            )
        else:
            payload = route_question(args.question, question_id=args.question_id, config=config, mode=args.mode, guard=guard)
        if args.pretty:
            print_pretty(payload)
        else:
            print_json(payload)
        return 0
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}
        if args.pretty:
            print_pretty(payload)
        else:
            print_json(payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
