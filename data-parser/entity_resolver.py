#!/usr/bin/env python3
"""
Lightweight entity and alias resolver for FahMai questions.

This module is intentionally deterministic and config-driven. It expands
business codes and domain terms into canonical hints before routing/retrieval,
without using question IDs or benchmark labels.
"""

from __future__ import annotations

import re
from typing import Any


DEFAULT_RESOLVER_CONFIG: dict[str, Any] = {
    "enabled": True,
    "max_expanded_terms": 40,
    "patterns": [
        {
            "name": "policy_id",
            "regex": r"\bPOL-\d{3}\b",
            "type": "policy",
            "expanded_terms": ["policy", "policy version", "effective date", "signing authority"],
            "likely_tables": ["DIM_POLICY"],
            "rag_filters": ["docs", "renders", "tables_guide", "tables_summary"],
        },
        {
            "name": "memo_id",
            "regex": r"\bMEMO-\d{3}\b",
            "type": "document",
            "expanded_terms": ["memo", "document evidence", "policy memo"],
            "likely_tables": [],
            "rag_filters": ["docs", "renders"],
        },
        {
            "name": "minutes_id",
            "regex": r"\bMIN-[A-Z]+-\d{4}-\d{2}\b",
            "type": "meeting_minutes",
            "expanded_terms": ["meeting minutes", "action items", "decision log", "operations report"],
            "likely_tables": [],
            "rag_filters": ["docs", "reports"],
        },
        {
            "name": "vendor_id",
            "regex": r"\bV-\d{3}\b",
            "type": "vendor",
            "expanded_terms": ["vendor", "supplier", "payment", "invoice"],
            "likely_tables": ["DIM_VENDOR", "FACT_VENDOR_PAYMENT"],
            "rag_filters": ["docs", "logs", "tables_guide", "tables_summary"],
        },
        {
            "name": "campaign_id",
            "regex": r"\b[A-Z]{2,}-LAUNCH-\d{4}\b",
            "type": "campaign",
            "expanded_terms": ["campaign", "promotion", "redemption", "promo ROI"],
            "likely_tables": ["DIM_CAMPAIGN", "FACT_PROMO_REDEMPTION", "FACT_SALES"],
            "rag_filters": ["docs", "reports", "tables_guide", "tables_summary"],
        },
        {
            "name": "product_code",
            "regex": r"\b[A-Z]{2,}(?:-[A-Za-z0-9]+){2,}\b",
            "type": "product_or_sku",
            "expanded_terms": ["product", "SKU", "product master", "model", "brand"],
            "likely_tables": ["DIM_PRODUCT", "FACT_SALES"],
            "rag_filters": ["docs", "reports", "tables_guide", "tables_summary"],
        },
    ],
    "term_aliases": [
        {
            "terms": ["msrp", "manufacturer suggested retail price", "ราคาขายปลีกแนะนำ", "list price"],
            "canonical": "MSRP",
            "expanded_terms": ["MSRP", "list price", "selling price", "product master", "DIM_PRODUCT"],
            "likely_tables": ["DIM_PRODUCT"],
            "route_bias": "sql",
        },
        {
            "terms": ["refund threshold", "refund limit", "เพดาน refund", "refund approval"],
            "canonical": "refund_threshold",
            "expanded_terms": ["refund threshold", "refund policy", "approval limit", "signing authority"],
            "likely_tables": ["DIM_POLICY", "FACT_REFUND"],
            "route_bias": "rag_assisted_sql",
        },
        {
            "terms": ["nps", "net promoter score"],
            "canonical": "NPS",
            "expanded_terms": ["NPS", "Net Promoter Score", "customer survey", "board report", "customer satisfaction"],
            "likely_tables": [],
            "rag_filters": ["reports", "docs"],
            "route_bias": "rag",
        },
        {
            "terms": ["line works", "line oa", "chat", "thread"],
            "canonical": "chat_thread",
            "expanded_terms": ["LINE WORKS", "chat thread", "message log", "conversation evidence"],
            "likely_tables": [],
            "rag_filters": ["docs", "logs"],
            "route_bias": "rag",
        },
        {
            "terms": ["phantom redemption", "phantom", "double-logging", "duplicate"],
            "canonical": "audit_anomaly",
            "expanded_terms": ["audit anomaly", "reconciliation", "duplicate record", "promo redemption"],
            "likely_tables": ["FACT_PROMO_REDEMPTION", "FACT_SALES"],
            "rag_filters": ["docs", "logs", "tables_guide", "tables_summary"],
            "route_bias": "hybrid",
        },
    ],
}


def _fold(value: str) -> str:
    return value.casefold()


def _append_unique(items: list[str], values: list[Any]) -> None:
    seen = {_fold(str(item)) for item in items}
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        key = _fold(text)
        if key not in seen:
            items.append(text)
            seen.add(key)


def _compact_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).casefold()


def _canonical_product_code(raw: str) -> str | None:
    text = re.sub(r"\s+", " ", raw.strip())
    upper = text.upper()
    compact = _compact_code(upper)
    if not compact:
        return None
    match = re.fullmatch(r"([A-Z]{2})([A-Z]{2})(\d{3})", compact)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.fullmatch(r"(SKU)(MASS)(\d{3})", compact)
    if match:
        return f"SKU-MASS-{match.group(3)}"
    match = re.fullmatch(r"(SF)(GALAXY)(PRO)(\d{4})", compact)
    if match:
        return f"SF-Galaxy-Pro-{match.group(4)}"
    match = re.fullmatch(r"([A-Z]{2})[-_/.\s]+([A-Z]{2})[-_/.\s]+(\d{3})", upper)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.fullmatch(r"(SKU)[-_/.\s]+(MASS)[-_/.\s]+(\d{3})", upper)
    if match:
        return f"SKU-MASS-{match.group(3)}"
    match = re.fullmatch(r"(SF)[-_/.\s]+(GALAXY)[-_/.\s]+(PRO)[-_/.\s]+(\d{4})", upper)
    if match:
        return f"SF-Galaxy-Pro-{match.group(4)}"
    return None


def _canonical_vendor_id(raw: str) -> str | None:
    compact = _compact_code(raw)
    match = re.fullmatch(r"v(\d{3})", compact)
    if match:
        return f"V-{match.group(1)}"
    return None


def _normalized_id_entities(question: str) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    candidates = [
        (r"\b[A-Za-z]{2}\s*[-_/.\s]\s*[A-Za-z]{2}\s*[-_/.\s]\s*\d{3}\b", "product_or_sku"),
        (r"\b[A-Za-z]{2}[A-Za-z]{2}\d{3}\b", "product_or_sku"),
        (r"\bSKU\s*[-_/.\s]?\s*MASS\s*[-_/.\s]?\s*\d{3}\b", "product_or_sku"),
        (r"\bSF\s*[-_/.\s]?\s*Galaxy\s*[-_/.\s]?\s*Pro\s*[-_/.\s]?\s*\d{4}\b", "product_or_sku"),
        (r"\bV\s*[-_/.\s]?\s*\d{3}\b", "vendor"),
    ]
    for regex, entity_type in candidates:
        for match in re.finditer(regex, question, flags=re.IGNORECASE):
            raw = match.group(0)
            canonical = _canonical_vendor_id(raw) if entity_type == "vendor" else _canonical_product_code(raw)
            if not canonical or canonical == raw:
                continue
            key = (entity_type, canonical)
            if key in seen:
                continue
            seen.add(key)
            if entity_type == "vendor":
                expanded_terms = ["vendor", "supplier", "payment", "invoice"]
                likely_tables = ["DIM_VENDOR", "FACT_VENDOR_PAYMENT"]
            else:
                expanded_terms = ["product", "SKU", "product master", "model", "brand"]
                likely_tables = ["DIM_PRODUCT", "FACT_SALES", "FACT_SALES_LINE_ITEM"]
            entities.append(
                {
                    "raw": raw,
                    "type": entity_type,
                    "canonical": canonical,
                    "pattern": "normalized_id",
                    "expanded_terms": expanded_terms,
                    "likely_tables": likely_tables,
                    "rag_filters": ["docs", "reports", "tables_guide", "tables_summary"],
                }
            )
    return entities


def _merge_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not config:
        return DEFAULT_RESOLVER_CONFIG
    merged = dict(DEFAULT_RESOLVER_CONFIG)
    merged.update(config)
    if "patterns" not in config:
        merged["patterns"] = DEFAULT_RESOLVER_CONFIG["patterns"]
    if "term_aliases" not in config:
        merged["term_aliases"] = DEFAULT_RESOLVER_CONFIG["term_aliases"]
    return merged


def resolve_entities(question: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    resolver_config = _merge_config(config)
    if not resolver_config.get("enabled", True):
        return {
            "enabled": False,
            "entities": [],
            "term_matches": [],
            "expanded_terms": [],
            "likely_tables": [],
            "rag_filters": [],
            "route_biases": [],
            "rewritten_query": question,
        }

    entities: list[dict[str, Any]] = []
    term_matches: list[dict[str, Any]] = []
    expanded_terms: list[str] = []
    likely_tables: list[str] = []
    rag_filters: list[str] = []
    route_biases: list[str] = []
    occupied_spans: list[tuple[int, int]] = []

    for entity in _normalized_id_entities(question):
        entities.append(entity)
        _append_unique(expanded_terms, [entity["canonical"], entity["raw"], *entity["expanded_terms"]])
        _append_unique(likely_tables, entity["likely_tables"])
        _append_unique(rag_filters, entity["rag_filters"])
        _append_unique(route_biases, ["sql"])

    for pattern_cfg in resolver_config.get("patterns", []):
        regex = pattern_cfg.get("regex")
        if not regex:
            continue
        for match in re.finditer(regex, question, flags=re.IGNORECASE):
            span = match.span()
            if any(span[0] >= old[0] and span[1] <= old[1] for old in occupied_spans):
                continue
            raw = match.group(0)
            entity = {
                "raw": raw,
                "type": pattern_cfg.get("type", pattern_cfg.get("name", "entity")),
                "canonical": raw,
                "pattern": pattern_cfg.get("name"),
                "expanded_terms": list(pattern_cfg.get("expanded_terms", [])),
                "likely_tables": list(pattern_cfg.get("likely_tables", [])),
                "rag_filters": list(pattern_cfg.get("rag_filters", [])),
            }
            entities.append(entity)
            occupied_spans.append(span)
            _append_unique(expanded_terms, [raw, *entity["expanded_terms"]])
            _append_unique(likely_tables, entity["likely_tables"])
            _append_unique(rag_filters, entity["rag_filters"])

    folded = _fold(question)
    for alias_cfg in resolver_config.get("term_aliases", []):
        terms = [str(term) for term in alias_cfg.get("terms", [])]
        matched = [term for term in terms if term and _fold(term) in folded]
        if not matched:
            continue
        item = {
            "canonical": alias_cfg.get("canonical") or matched[0],
            "matched_terms": matched,
            "expanded_terms": list(alias_cfg.get("expanded_terms", [])),
            "likely_tables": list(alias_cfg.get("likely_tables", [])),
            "rag_filters": list(alias_cfg.get("rag_filters", [])),
            "route_bias": alias_cfg.get("route_bias"),
        }
        term_matches.append(item)
        _append_unique(expanded_terms, matched)
        _append_unique(expanded_terms, item["expanded_terms"])
        _append_unique(likely_tables, item["likely_tables"])
        _append_unique(rag_filters, item["rag_filters"])
        if item["route_bias"]:
            _append_unique(route_biases, [item["route_bias"]])

    max_terms = int(resolver_config.get("max_expanded_terms", 40))
    expanded_terms = expanded_terms[:max_terms]
    rewrite_parts = [question]
    _append_unique(rewrite_parts, likely_tables)
    _append_unique(rewrite_parts, expanded_terms)

    return {
        "enabled": True,
        "entities": entities,
        "term_matches": term_matches,
        "expanded_terms": expanded_terms,
        "likely_tables": likely_tables,
        "rag_filters": rag_filters,
        "route_biases": route_biases,
        "rewritten_query": " ".join(rewrite_parts),
    }
