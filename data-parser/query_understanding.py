#!/usr/bin/env python3
"""
Deterministic query understanding for the FahMai post-guard pipeline.

This stage sits after the future input guard and before routing/tool planning.
It extracts intent, constraints, table/entity hints, and an answer-validation
contract. It does not execute SQL/RAG and does not use question IDs.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any

from entity_resolver import resolve_entities


DEFAULT_CONFIG = Path(__file__).with_name("router_config.json")

TABLE_RE = re.compile(r"\b(?:FACT|DIM|RAW|AUX|VW|vw)_[A-Za-z0-9_]+\b")
COLUMN_RE = re.compile(
    r"\b(?:net_total_thb|business_event_date|posting_date|loyalty_tier|sku_id|branch_code|"
    r"customer_id|vendor_id|campaign_id|policy_variable|effective_from|effective_to|"
    r"return_window_days|msrp_thb|invoice_id|transaction_id|order_id)\b",
    flags=re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
YEAR_RE = re.compile(r"\b(?:20\d{2}|25\d{2})\b")

THAI_MONTHS = {
    "มกราคม": "01",
    "กุมภาพันธ์": "02",
    "มีนาคม": "03",
    "เมษายน": "04",
    "พฤษภาคม": "05",
    "มิถุนายน": "06",
    "กรกฎาคม": "07",
    "สิงหาคม": "08",
    "กันยายน": "09",
    "ตุลาคม": "10",
    "พฤศจิกายน": "11",
    "ธันวาคม": "12",
}

TERM_GROUPS: dict[str, list[str]] = {
    "ranking": ["top", "highest", "largest", "most", "มากที่สุด", "สูงที่สุด", "อันดับ", "rank"],
    "lowest": ["lowest", "smallest", "least", "ต่ำที่สุด", "น้อยที่สุด"],
    "count": ["count", "number of", "how many", "กี่", "จำนวน", "นับ", "transaction count"],
    "sum": ["sum", "total", "revenue", "sales", "ยอดขาย", "รวม", "net_total_thb"],
    "percentage": ["percent", "percentage", "share", "%", "สัดส่วน", "เปอร์เซ็นต์"],
    "average": ["average", "avg", "mean", "เฉลี่ย"],
    "compare": ["compare", "versus", "vs", "difference", "changed", "growth", "เทียบ", "เปรียบเทียบ"],
    "policy": ["policy", "นโยบาย", "effective", "มีผล", "as-of", "current version", "threshold", "authority"],
    "document": [
        "email",
        "memo",
        "line works",
        "line oa",
        "chat",
        "thread",
        "report",
        "เอกสาร",
        "บันทึก",
        "minutes",
        "ocr",
        "render",
        "pdf",
    ],
    "evidence": ["evidence", "reason", "root cause", "why", "audit", "reconciliation", "flag", "หลักฐาน", "สาเหตุ"],
    "refund": ["refund", "คืนเงิน", "คืนสินค้า"],
    "vendor": ["vendor", "supplier", "shipping", "invoice"],
    "product": ["sku", "product", "สินค้า", "msrp", "warranty"],
    "branch": ["branch", "store", "สาขา"],
    "customer": ["customer", "loyalty", "tier", "ลูกค้า"],
    "campaign": ["campaign", "promotion", "promo", "redemption"],
}


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def folded_contains(question: str, terms: list[str]) -> list[str]:
    folded = question.casefold()
    hits: list[str] = []
    for term in terms:
        needle = term.casefold()
        if re.fullmatch(r"[a-z0-9_]+", needle):
            if re.search(rf"\b{re.escape(needle)}\b", folded):
                hits.append(term)
        elif needle in folded:
            hits.append(term)
    return hits


def append_unique(items: list[str], values: list[Any]) -> None:
    seen = {str(item).casefold() for item in items}
    for value in values:
        text = str(value).strip()
        if text and text.casefold() not in seen:
            items.append(text)
            seen.add(text.casefold())


def parse_guard_payload(args: argparse.Namespace) -> dict[str, Any] | None:
    sources = [bool(args.guard_json), bool(args.guard_json_inline), bool(args.guard_json_b64)]
    if sum(sources) > 1:
        raise ValueError("Use only one of --guard-json, --guard-json-inline, or --guard-json-b64.")
    if args.guard_json:
        return load_json(args.guard_json)
    if args.guard_json_inline:
        return json.loads(args.guard_json_inline)
    if args.guard_json_b64:
        return json.loads(base64.b64decode(args.guard_json_b64).decode("utf-8"))
    return None


def normalize_guard(
    raw_guard: dict[str, Any] | None,
    *,
    raw_question: str,
    sanitized_question: str | None = None,
) -> tuple[dict[str, Any], str]:
    if raw_guard is None:
        effective = sanitized_question or raw_question
        return {
            "provided": False,
            "status": "not_integrated",
            "safe_to_route": True,
            "attack_detected": False,
            "attack_types": [],
            "notes": ["Input guard placeholder only. External guard can provide sanitized_question later."],
        }, effective

    source = raw_guard.get("guard") if isinstance(raw_guard.get("guard"), dict) else raw_guard
    status = source.get("status") or source.get("decision") or "provided"
    safe_to_route = source.get("safe_to_route", source.get("allow_route", source.get("safe_to_answer", True)))
    attack_detected = source.get("attack_detected", source.get("injection_like", source.get("is_attack", False)))
    attack_types = source.get("attack_types", source.get("attack_type", []))
    if isinstance(attack_types, str):
        attack_types = [attack_types]
    notes = source.get("notes", source.get("reasons", []))
    if isinstance(notes, str):
        notes = [notes]
    effective = (
        sanitized_question
        or source.get("sanitized_question")
        or source.get("clean_question")
        or raw_guard.get("sanitized_question")
        or raw_question
    )
    return {
        "provided": True,
        "status": status,
        "safe_to_route": bool(safe_to_route),
        "attack_detected": bool(attack_detected),
        "attack_types": list(attack_types or []),
        "notes": list(notes or []),
    }, str(effective)


def normalize_year(raw: str) -> int:
    year = int(raw)
    if year >= 2500:
        year -= 543
    return year


def extract_dates(question: str) -> list[dict[str, Any]]:
    dates: list[dict[str, Any]] = []
    for raw in ISO_DATE_RE.findall(question):
        role = "as_of" if folded_contains(question, ["as-of", "as of", "current", "ณ", "ก่อน"]) else "exact"
        dates.append({"raw": raw, "iso": raw, "role": role})

    thai_pattern = r"(\d{1,2})\s+(" + "|".join(THAI_MONTHS) + r")\s+(25\d{2}|20\d{2})"
    for match in re.finditer(thai_pattern, question):
        day = int(match.group(1))
        year = normalize_year(match.group(3))
        iso = f"{year:04d}-{THAI_MONTHS[match.group(2)]}-{day:02d}"
        dates.append({"raw": match.group(0), "iso": iso, "role": "as_of"})

    seen_iso = {item["iso"] for item in dates}
    for raw in YEAR_RE.findall(question):
        year = normalize_year(raw)
        if 2000 <= year <= 2100 and str(year) not in seen_iso:
            dates.append({"raw": raw, "iso": str(year), "role": "year"})
            seen_iso.add(str(year))
    return dates


def extract_tables_and_columns(question: str, entity_resolution: dict[str, Any]) -> tuple[list[str], list[str]]:
    tables: list[str] = []
    columns: list[str] = []
    append_unique(tables, TABLE_RE.findall(question))
    append_unique(columns, COLUMN_RE.findall(question))
    append_unique(tables, entity_resolution.get("likely_tables", []))
    return tables, columns


def infer_metrics(question: str) -> list[dict[str, str]]:
    metrics: list[dict[str, str]] = []
    if folded_contains(question, ["msrp", "manufacturer suggested retail price", "ราคาขายปลีกแนะนำ", "list price"]):
        metrics.append({"name": "msrp_thb", "aggregation": "lookup"})
    if folded_contains(question, ["units sold", "จำนวนชิ้น", "ชิ้นที่ขาย"]):
        metrics.append({"name": "units_sold", "aggregation": "sum"})
    if folded_contains(question, ["amount_thb", "จำนวนเงิน"]):
        metrics.append({"name": "amount_thb", "aggregation": "max"})
    if folded_contains(question, ["คะแนน", "points"]):
        metrics.append({"name": "points", "aggregation": "sum"})
    if folded_contains(question, ["stockout", "closing_units"]):
        metrics.append({"name": "stockout_events", "aggregation": "count"})
    if folded_contains(question, TERM_GROUPS["count"]):
        metrics.append({"name": "count", "aggregation": "count"})
    if folded_contains(question, TERM_GROUPS["sum"]):
        metrics.append({"name": "net_total_thb", "aggregation": "sum"})
    if folded_contains(question, TERM_GROUPS["percentage"]):
        metrics.append({"name": "share", "aggregation": "ratio"})
    if folded_contains(question, TERM_GROUPS["average"]):
        metrics.append({"name": "average", "aggregation": "avg"})
    if not metrics and re.search(r"\bกี่|เท่าไหร่|how much\b", question, flags=re.IGNORECASE):
        metrics.append({"name": "value", "aggregation": "lookup"})
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for metric in metrics:
        key = metric["name"].casefold()
        if key not in seen:
            deduped.append(metric)
            seen.add(key)
    return deduped


def infer_dimensions(question: str, tables: list[str]) -> list[str]:
    dimensions: list[str] = []
    folded = question.casefold()
    if folded_contains(question, TERM_GROUPS["branch"]) or "DIM_BRANCH" in tables:
        dimensions.append("branch")
    if folded_contains(question, TERM_GROUPS["product"]) or "DIM_PRODUCT" in tables:
        dimensions.append("product")
    if folded_contains(question, TERM_GROUPS["vendor"]) or "DIM_VENDOR" in tables:
        dimensions.append("vendor")
    if folded_contains(question, TERM_GROUPS["customer"]) or "DIM_CUSTOMER" in tables:
        dimensions.append("customer")
    if folded_contains(question, TERM_GROUPS["campaign"]) or "DIM_CAMPAIGN" in tables:
        dimensions.append("campaign")
    if "employee" in folded or "พนักงาน" in folded:
        dimensions.append("employee")
    return dimensions


def infer_ranking(question: str, metrics: list[dict[str, str]]) -> dict[str, Any] | None:
    has_top = bool(folded_contains(question, TERM_GROUPS["ranking"]))
    has_low = bool(folded_contains(question, TERM_GROUPS["lowest"]))
    if not has_top and not has_low:
        return None
    limit_match = re.search(r"\btop\s*(\d+)\b", question, flags=re.IGNORECASE)
    limit = int(limit_match.group(1)) if limit_match else 1
    by = metrics[0]["name"] if metrics else "count"
    return {"direction": "asc" if has_low else "desc", "limit": limit, "by": by}


def infer_intent(
    question: str,
    *,
    tables: list[str],
    columns: list[str],
    metrics: list[dict[str, str]],
    ranking: dict[str, Any] | None,
) -> dict[str, Any]:
    doc_terms = folded_contains(question, TERM_GROUPS["document"])
    policy_terms = folded_contains(question, TERM_GROUPS["policy"])
    evidence_terms = folded_contains(question, TERM_GROUPS["evidence"])
    compare_terms = folded_contains(question, TERM_GROUPS["compare"])
    sql_signals = bool(tables or columns or metrics or ranking)

    if policy_terms:
        primary = "policy_as_of"
    elif ranking:
        primary = "ranking"
    elif compare_terms:
        primary = "comparison"
    elif evidence_terms and sql_signals:
        primary = "audit"
    elif doc_terms and not sql_signals:
        primary = "document_evidence"
    elif metrics and all(metric.get("aggregation") == "lookup" for metric in metrics):
        primary = "metric_lookup"
    elif metrics:
        primary = "aggregation"
    elif tables or columns:
        primary = "metric_lookup"
    else:
        primary = "unknown"

    needs_sql = primary in {"policy_as_of", "ranking", "comparison", "audit", "aggregation", "metric_lookup"}
    needs_rag = primary == "document_evidence" or (bool(doc_terms or evidence_terms) and primary != "ranking")
    if primary == "policy_as_of":
        needs_rag = True
    return {
        "primary": primary,
        "needs_sql": needs_sql,
        "needs_rag": needs_rag,
        "needs_policy_context": primary == "policy_as_of",
        "needs_explanation": bool(evidence_terms or compare_terms),
    }


def preferred_tools(intent: dict[str, Any], dimensions: list[str], metrics: list[dict[str, str]]) -> list[str]:
    primary = intent["primary"]
    metric_names = {str(metric.get("name", "")).casefold() for metric in metrics if isinstance(metric, dict)}
    if "product" in dimensions and "msrp_thb" in metric_names:
        return ["product_msrp", "semantic_entity_profile"]
    if primary == "ranking":
        return ["semantic_top_n", "branch_sales_top", "top_selling_sku_by_units", "stockout_top_sku"]
    if primary in {"aggregation", "comparison"}:
        return ["semantic_metric_aggregate", "semantic_time_window_compare"]
    if primary == "metric_lookup":
        if "product" in dimensions:
            return ["product_msrp", "product_warranty", "semantic_entity_profile"]
        return ["semantic_metric_aggregate", "semantic_entity_profile"]
    if primary == "policy_as_of":
        return ["policy_value_as_of"]
    if primary == "audit":
        return ["semantic_duplicate_check", "semantic_metric_aggregate", "semantic_top_n"]
    return []


def build_validation_contract(
    question: str,
    *,
    intent: dict[str, Any],
    metrics: list[dict[str, str]],
    dimensions: list[str],
    ranking: dict[str, Any] | None,
) -> dict[str, Any]:
    must_include: list[str] = []
    reject_if: list[str] = []
    checks: list[str] = []
    answer_slots: list[str] = []

    primary = intent["primary"]
    if ranking:
        must_include.extend(["ranked entity", ranking.get("by", "ranking metric")])
        answer_slots.extend(["winner_or_ranked_rows", "ranking_metric"])
        reject_if.append("result only profiles a single entity without proving it is top/bottom")
        reject_if.append("result lacks the metric used for ranking")
        checks.append("rows should be ordered consistently with ranking direction")
    if metrics:
        for metric in metrics:
            must_include.append(metric["name"])
        answer_slots.append("metric_value")
    for dimension in dimensions:
        must_include.append(dimension)
    if primary == "policy_as_of":
        must_include.extend(["policy variable/value", "effective date or active version"])
        reject_if.append("policy answer has no effective-date/version evidence")
        answer_slots.extend(["policy_value", "effective_date"])
    if intent.get("needs_rag"):
        checks.append("include cited chunk ids when textual evidence is used")
    if "FahMai" in question or "ฟ้าใหม่" in question:
        checks.append("preserve company scope/name exactly")

    return {
        "must_include": list(dict.fromkeys(must_include)),
        "reject_if": list(dict.fromkeys(reject_if)),
        "answer_ready_checks": list(dict.fromkeys(checks)),
        "answer_slots": list(dict.fromkeys(answer_slots)),
    }


def understand_question(
    question: str,
    *,
    config: dict[str, Any] | None = None,
    sanitized_question: str | None = None,
    guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config or {}
    guard_info, effective_question = normalize_guard(guard, raw_question=question, sanitized_question=sanitized_question)
    resolver_cfg = cfg.get("entity_resolver") if isinstance(cfg.get("entity_resolver"), dict) else None
    entity_resolution = resolve_entities(effective_question, resolver_cfg)

    dates = extract_dates(effective_question)
    tables, columns = extract_tables_and_columns(effective_question, entity_resolution)
    metrics = infer_metrics(effective_question)
    dimensions = infer_dimensions(effective_question, tables)
    ranking = infer_ranking(effective_question, metrics)
    intent = infer_intent(effective_question, tables=tables, columns=columns, metrics=metrics, ranking=ranking)
    validation_contract = build_validation_contract(
        effective_question,
        intent=intent,
        metrics=metrics,
        dimensions=dimensions,
        ranking=ranking,
    )

    document_refs: list[str] = []
    append_unique(document_refs, re.findall(r"\b(?:MEMO|MIN|POL|EMAIL|DOC)-[A-Za-z0-9-]+\b", effective_question))
    evidence_terms: list[str] = []
    for group in ("document", "evidence", "policy"):
        append_unique(evidence_terms, folded_contains(effective_question, TERM_GROUPS[group]))

    preferred = preferred_tools(intent, dimensions, metrics)
    avoid_tools: list[str] = []
    if intent["primary"] in {"ranking", "comparison"}:
        avoid_tools.append("semantic_entity_profile")

    rag_terms: list[str] = [effective_question]
    append_unique(rag_terms, entity_resolution.get("expanded_terms", []))
    append_unique(rag_terms, tables)
    append_unique(rag_terms, evidence_terms)

    return {
        "ok": True,
        "question": question,
        "normalized_question": effective_question.strip(),
        "guard": guard_info,
        "intent": intent,
        "constraints": {
            "answer_slots": validation_contract["answer_slots"],
            "metrics": metrics,
            "dimensions": dimensions,
            "filters": [],
            "dates": dates,
            "ranking": ranking,
            "tables": tables,
            "columns": columns,
            "entities": entity_resolution.get("entities", []),
            "term_matches": entity_resolution.get("term_matches", []),
            "document_refs": document_refs,
            "evidence_terms": evidence_terms,
        },
        "validation_contract": validation_contract,
        "hints": {
            "tables_hint": tables,
            "rag_query": " ".join(rag_terms),
            "preferred_tools": preferred,
            "avoid_tools": avoid_tools,
            "rag_filters": entity_resolution.get("rag_filters", []),
        },
        "entity_resolution": entity_resolution,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Understand a FahMai question before routing/tool planning.")
    parser.add_argument("--question", required=True)
    parser.add_argument("--sanitized-question", default=None)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--guard-json", type=Path, default=None)
    parser.add_argument("--guard-json-inline", default=None)
    parser.add_argument("--guard-json-b64", default=None)
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    try:
        config = load_json(args.config) if args.config.exists() else {}
        guard = parse_guard_payload(args)
        payload = understand_question(args.question, config=config, sanitized_question=args.sanitized_question, guard=guard)
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
