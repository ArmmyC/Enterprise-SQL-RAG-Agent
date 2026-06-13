#!/usr/bin/env python3
"""
Query FahMai RAG chunks from the DuckDB companion embedding table.

The CLI is JSON-first so it can be called by an agent, but it also has a
--pretty mode for quick manual inspection. It never writes to the database.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any


DEFAULT_DATABASE = Path("data-parser/output/fahmai.duckdb")
DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_TOP_K = 8
DEFAULT_CANDIDATE_K = 80
DEFAULT_SNIPPET_CHARS = 700
DEFAULT_VECTOR_WEIGHT = 0.85
DEFAULT_TEXT_WEIGHT = 0.15
DEFAULT_EXACT_WEIGHT = 0.18
DEFAULT_SOURCE_WEIGHT = 0.06
MODEL_PATH_ENV = "MODEL_PATH"
DEFAULT_ROUTER_CONFIG = Path(__file__).with_name("router_config.json")

from entity_resolver import resolve_entities


class QueryError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def now_ms() -> int:
    return int(time.time() * 1000)


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


def tokenize_query(query: str, max_terms: int = 24) -> list[str]:
    pattern = r"[A-Za-z0-9_]+(?:[-./:][A-Za-z0-9_]+)*|[\u0E00-\u0E7F]+"
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        token = token.strip().lower()
        if len(token) < 2 or token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for match in re.findall(pattern, query):
        add(match)
        if any(sep in match for sep in "-./:"):
            for part in re.split(r"[-./:]+", match):
                add(part)
        if len(tokens) >= max_terms:
            break
    return tokens[:max_terms]


def extract_exact_terms(query: str) -> list[str]:
    patterns = [
        r"\b[A-Z]{2,}(?:-[A-Z0-9]+)+\b",
        r"\b(?:FACT|DIM|VW|vw|dim)_[A-Za-z0-9_]+\b",
        r"\b[A-Z]+-[A-Z]+-\d{4}-\d{2}\b",
        r"\bCHAT-[A-Z]+-\d{4}-\d{2}-\d{2}-[A-Za-z0-9]+\b",
        r"\bTHREAD-[A-Z]+-\d{8}-[A-Za-z0-9]+\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.findall(pattern, query, flags=re.IGNORECASE):
            value = match.strip()
            key = value.lower()
            if value and key not in seen:
                out.append(value)
                seen.add(key)
    return out


def likely_source_hints(query: str) -> list[str]:
    folded = query.casefold()
    hints: list[str] = []
    if any(term in folded for term in ["min-", "ops report", "report", "ประชุม", "minutes", "board"]):
        hints.extend(["reports", "docs"])
    if any(term in folded for term in ["line works", "line oa", "chat", "thread"]):
        hints.extend(["docs", "logs"])
    if any(term in folded for term in ["ocr", "pdf", "render", "receipt", "รูป"]):
        hints.append("renders")
    if any(term in folded for term in ["fact_", "dim_", "vw_", "schema", "table", "column"]):
        hints.extend(["tables_guide", "tables_summary"])
    return list(dict.fromkeys(hints))


def compact_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def make_snippet(content: Any, limit: int) -> str:
    text = compact_text(content)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def parse_metadata(value: Any) -> Any:
    if value is None or value == "":
        return {}
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def apply_entity_expansion(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.expand_entities:
        return None
    config: dict[str, Any] = {}
    if args.resolver_config and args.resolver_config.exists():
        raw_config = load_json(args.resolver_config)
        config = raw_config.get("entity_resolver", raw_config)
    entity_resolution = resolve_entities(args.query, config)
    expanded = str(entity_resolution.get("rewritten_query") or args.query).strip()
    if expanded:
        args.original_query = args.query
        args.query = expanded
    return entity_resolution


def payload_error(error_type: str, message: str, *, started_ms: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error_type": error_type,
        "message": message,
    }
    if started_ms is not None:
        payload["duration_ms"] = now_ms() - started_ms
    return payload


def is_lock_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "conflicting lock" in text or "could not set lock" in text or "database is locked" in text


def connect_duckdb(database_path: Path, wait_seconds: int):
    import duckdb

    deadline = time.monotonic() + wait_seconds
    last_exc: Exception | None = None
    while True:
        try:
            return duckdb.connect(str(database_path), read_only=True)
        except Exception as exc:  # DuckDB exposes lock failures as IOException.
            last_exc = exc
            if not is_lock_error(exc) or time.monotonic() >= deadline:
                break
            time.sleep(min(5.0, max(0.25, deadline - time.monotonic())))

    if last_exc and is_lock_error(last_exc):
        raise QueryError(
            "database_locked",
            f"Could not open {database_path} read-only because another DuckDB process holds the file lock.",
        )
    if last_exc:
        raise QueryError("database_open_failed", str(last_exc))
    raise QueryError("database_open_failed", f"Could not open {database_path}")


def table_exists(con: Any, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'main'
          AND table_name = ?
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0])


def get_index_info(con: Any) -> dict[str, Any]:
    if not table_exists(con, "rag_chunks"):
        raise QueryError("missing_table", "Required table rag_chunks was not found.")

    rag_rows = int(con.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0] or 0)
    embeddings_exist = table_exists(con, "rag_chunk_embeddings")
    embedded_rows = 0
    embedding_model = None
    embedding_dim = None
    embedding_normalized = None

    if embeddings_exist:
        embedded_rows = int(con.execute("SELECT COUNT(*) FROM rag_chunk_embeddings").fetchone()[0] or 0)
        meta = con.execute(
            """
            SELECT embedding_model, embedding_dim, embedding_normalized, COUNT(*) AS rows
            FROM rag_chunk_embeddings
            GROUP BY embedding_model, embedding_dim, embedding_normalized
            ORDER BY rows DESC
            LIMIT 1
            """
        ).fetchone()
        if meta:
            embedding_model = meta[0]
            embedding_dim = int(meta[1]) if meta[1] is not None else None
            embedding_normalized = bool(meta[2]) if meta[2] is not None else None

    return {
        "rag_rows": rag_rows,
        "embedded_rows": embedded_rows,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "embedding_normalized": embedding_normalized,
        "partial_index": embeddings_exist and embedded_rows < rag_rows,
    }


def build_filters(args: argparse.Namespace, table_alias: str = "rc") -> tuple[list[str], list[Any], dict[str, list[str]]]:
    staging_sources = split_values(args.staging_source)
    source_types = split_values(args.source_type)
    source_names = split_values(args.source_name)
    clauses: list[str] = []
    params: list[Any] = []

    def add_in_filter(column: str, values: list[str]) -> None:
        if not values:
            return
        placeholders = ", ".join(["?"] * len(values))
        clauses.append(f"{table_alias}.{column} IN ({placeholders})")
        params.extend(values)

    add_in_filter("staging_source", staging_sources)
    add_in_filter("source_type", source_types)
    add_in_filter("source_name", source_names)
    return clauses, params, {
        "staging_source": staging_sources,
        "source_type": source_types,
        "source_name": source_names,
    }


def text_score_expression(tokens: list[str]) -> tuple[str, list[Any]]:
    if not tokens:
        return "0.0", []
    text_blob = (
        "lower("
        "coalesce(rc.bm25_text, '') || ' ' || "
        "coalesce(rc.content, '') || ' ' || "
        "coalesce(rc.title, '') || ' ' || "
        "coalesce(rc.heading, '') || ' ' || "
        "coalesce(rc.source_name, '')"
        ")"
    )
    parts = [f"CASE WHEN {text_blob} LIKE ? THEN 1.0 ELSE 0.0 END" for _ in tokens]
    params = [f"%{token}%" for token in tokens]
    return "(" + " + ".join(parts) + f") / {float(len(tokens))}", params


def exact_term_weight(term: str) -> float:
    upper = term.upper()
    if upper.startswith(("MIN-", "MEMO-", "POL-", "THREAD-", "CHAT-", "RC-", "T3-")):
        return 3.0
    if re.match(r"^(FACT|DIM|VW)_", upper):
        return 2.0
    if re.match(r"^\d{4}-\d{2}-\d{2}$", term):
        return 0.35
    if re.match(r"^[A-Z]{2,5}-[A-Z0-9]{1,5}$", upper):
        return 0.75
    return 1.25


def exact_score_expression(exact_terms: list[str]) -> tuple[str, list[Any]]:
    if not exact_terms:
        return "0.0", []
    text_blob = (
        "lower("
        "coalesce(rc.chunk_id, '') || ' ' || "
        "coalesce(rc.source_name, '') || ' ' || "
        "coalesce(rc.source_path, '') || ' ' || "
        "coalesce(rc.title, '') || ' ' || "
        "coalesce(rc.heading, '') || ' ' || "
        "coalesce(rc.content, '') || ' ' || "
        "coalesce(CAST(rc.metadata AS VARCHAR), '')"
        ")"
    )
    weights = [exact_term_weight(term) for term in exact_terms]
    parts = [f"(CASE WHEN {text_blob} LIKE ? THEN {weight:.4f} ELSE 0.0 END)" for term, weight in zip(exact_terms, weights)]
    params = [f"%{term.lower()}%" for term in exact_terms]
    return "(" + " + ".join(parts) + f") / {sum(weights):.4f}", params


def source_score_expression(source_hints: list[str]) -> tuple[str, list[Any]]:
    if not source_hints:
        return "0.0", []
    parts = ["CASE WHEN rc.staging_source = ? THEN 1.0 ELSE 0.0 END" for _ in source_hints]
    return "greatest(" + ", ".join(parts) + ")", source_hints


def load_query_embedding(args: argparse.Namespace, expected_dim: int) -> list[float]:
    if args.offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    model_ref = args.model_path or os.environ.get(MODEL_PATH_ENV) or args.model
    stderr_sink = io.StringIO()
    with contextlib.redirect_stderr(stderr_sink), warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(str(model_ref), device=args.device) if args.device else SentenceTransformer(str(model_ref))
        get_dim = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
        model_dim = get_dim()
        if model_dim != expected_dim:
            raise QueryError("embedding_dim_mismatch", f"Model produced dim={model_dim}; expected dim={expected_dim}.")

        vector = model.encode(
            [args.query],
            batch_size=1,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )[0]
    values = vector.tolist() if hasattr(vector, "tolist") else list(vector)
    if len(values) != expected_dim:
        raise QueryError("embedding_dim_mismatch", f"Query vector has dim={len(values)}; expected dim={expected_dim}.")
    return [float(value) for value in values]


def fetch_dicts(con: Any, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    cursor = con.execute(sql, params)
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def select_columns() -> str:
    return """
        rc.chunk_id,
        rc.staging_source,
        rc.source_type,
        rc.source_name,
        rc.source_path,
        rc.title,
        rc.heading,
        rc.page,
        rc.chunk_index,
        rc.content,
        CAST(rc.metadata AS VARCHAR) AS metadata_json
    """


def retrieve_vector(
    con: Any,
    *,
    args: argparse.Namespace,
    index_info: dict[str, Any],
    filters: tuple[list[str], list[Any], dict[str, list[str]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if index_info["embedded_rows"] <= 0:
        raise QueryError("missing_embeddings", "rag_chunk_embeddings exists but contains no rows.")

    expected_dim = int(index_info["embedding_dim"] or DEFAULT_EMBEDDING_DIM)
    query_vector = load_query_embedding(args, expected_dim)
    exact_terms = extract_exact_terms(args.query)
    source_hints = likely_source_hints(args.query)
    filter_clauses, filter_params, _filter_payload = filters
    clauses = ["e.embedding_dim = ?"]
    clauses.extend(filter_clauses)
    where_sql = " AND ".join(clauses)
    score_expr = f"array_inner_product(e.embedding, ?::FLOAT[{expected_dim}])"
    exact_expr, exact_params = exact_score_expression(exact_terms)
    source_expr, source_params = source_score_expression(source_hints)
    sql = f"""
        SELECT
            {select_columns()},
            ({score_expr} + (? * {exact_expr}) + (? * {source_expr})) AS score,
            {score_expr} AS vector_score,
            CAST(NULL AS DOUBLE) AS text_score,
            {exact_expr} AS exact_score,
            {source_expr} AS source_score
        FROM rag_chunk_embeddings AS e
        JOIN rag_chunks AS rc
          ON rc.chunk_id = e.chunk_id
        WHERE {where_sql}
        ORDER BY score DESC, rc.chunk_id
        LIMIT ?
    """
    params = [
        query_vector,
        args.exact_weight,
        *exact_params,
        args.source_weight,
        *source_params,
        query_vector,
        *exact_params,
        *source_params,
        expected_dim,
        *filter_params,
        args.candidate_k,
    ]
    return fetch_dicts(con, sql, params), {
        "query_embedding_dim": expected_dim,
        "exact_terms": exact_terms,
        "source_hints": source_hints,
    }


def retrieve_text(
    con: Any,
    *,
    args: argparse.Namespace,
    filters: tuple[list[str], list[Any], dict[str, list[str]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tokens = tokenize_query(args.query)
    if not tokens:
        raise QueryError("no_text_terms", "Could not extract keyword terms from the query.")

    exact_terms = extract_exact_terms(args.query)
    source_hints = likely_source_hints(args.query)
    filter_clauses, filter_params, _filter_payload = filters
    text_expr, text_params = text_score_expression(tokens)
    exact_expr, exact_params = exact_score_expression(exact_terms)
    source_expr, source_params = source_score_expression(source_hints)
    clauses = list(filter_clauses)
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"""
        WITH scored AS (
            SELECT
                {select_columns()},
                ({text_expr} + (? * {exact_expr}) + (? * {source_expr})) AS score,
                CAST(NULL AS DOUBLE) AS vector_score,
                {text_expr} AS text_score,
                {exact_expr} AS exact_score,
                {source_expr} AS source_score
            FROM rag_chunks AS rc
            {where_sql}
        )
        SELECT *
        FROM scored
        WHERE score > 0
        ORDER BY score DESC, length(content) ASC NULLS LAST, chunk_id
        LIMIT ?
    """
    params = [
        *text_params,
        args.exact_weight,
        *exact_params,
        args.source_weight,
        *source_params,
        *text_params,
        *exact_params,
        *source_params,
        *filter_params,
        args.candidate_k,
    ]
    return fetch_dicts(con, sql, params), {"query_terms": tokens, "exact_terms": exact_terms, "source_hints": source_hints}


def retrieve_hybrid(
    con: Any,
    *,
    args: argparse.Namespace,
    index_info: dict[str, Any],
    filters: tuple[list[str], list[Any], dict[str, list[str]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if index_info["embedded_rows"] <= 0:
        raise QueryError("missing_embeddings", "rag_chunk_embeddings exists but contains no rows.")

    expected_dim = int(index_info["embedding_dim"] or DEFAULT_EMBEDDING_DIM)
    query_vector = load_query_embedding(args, expected_dim)
    tokens = tokenize_query(args.query)
    exact_terms = extract_exact_terms(args.query)
    source_hints = likely_source_hints(args.query)
    text_expr, text_params = text_score_expression(tokens)
    exact_expr, exact_params = exact_score_expression(exact_terms)
    source_expr, source_params = source_score_expression(source_hints)
    filter_clauses, filter_params, _filter_payload = filters
    clauses = ["e.embedding_dim = ?"]
    clauses.extend(filter_clauses)
    where_sql = " AND ".join(clauses)
    vector_expr = f"array_inner_product(e.embedding, ?::FLOAT[{expected_dim}])"
    sql = f"""
        SELECT
            {select_columns()},
            ((? * {vector_expr}) + (? * {text_expr}) + (? * {exact_expr}) + (? * {source_expr})) AS score,
            {vector_expr} AS vector_score,
            {text_expr} AS text_score,
            {exact_expr} AS exact_score,
            {source_expr} AS source_score
        FROM rag_chunk_embeddings AS e
        JOIN rag_chunks AS rc
          ON rc.chunk_id = e.chunk_id
        WHERE {where_sql}
        ORDER BY score DESC, rc.chunk_id
        LIMIT ?
    """
    params = [
        args.vector_weight,
        query_vector,
        args.text_weight,
        *text_params,
        args.exact_weight,
        *exact_params,
        args.source_weight,
        *source_params,
        query_vector,
        *text_params,
        *exact_params,
        *source_params,
        expected_dim,
        *filter_params,
        args.candidate_k,
    ]
    return fetch_dicts(con, sql, params), {
        "query_embedding_dim": expected_dim,
        "query_terms": tokens,
        "exact_terms": exact_terms,
        "source_hints": source_hints,
    }


def format_results(rows: list[dict[str, Any]], snippet_chars: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        results.append(
            {
                "rank": rank,
                "chunk_id": row.get("chunk_id"),
                "score": row.get("score"),
                "score_components": {
                    "vector": row.get("vector_score"),
                    "text": row.get("text_score"),
                    "exact": row.get("exact_score"),
                    "source": row.get("source_score"),
                },
                "staging_source": row.get("staging_source"),
                "source_type": row.get("source_type"),
                "source_name": row.get("source_name"),
                "source_path": row.get("source_path"),
                "title": row.get("title"),
                "heading": row.get("heading"),
                "page": row.get("page"),
                "chunk_index": row.get("chunk_index"),
                "snippet": make_snippet(row.get("content"), snippet_chars),
                "metadata": parse_metadata(row.get("metadata_json")),
            }
        )
    return results


def dedupe_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        content = compact_text(row.get("content")).lower()
        key = (
            str(row.get("source_path") or row.get("source_name") or ""),
            str(row.get("heading") or row.get("title") or ""),
            content[:500],
        )
        existing = deduped.get(key)
        if existing is None or float(row.get("score") or 0.0) > float(existing.get("score") or 0.0):
            deduped[key] = row
    return sorted(deduped.values(), key=lambda item: (float(item.get("score") or 0.0), str(item.get("chunk_id"))), reverse=True)[:limit]


def run_query(args: argparse.Namespace) -> dict[str, Any]:
    started_ms = now_ms()
    args.original_query = getattr(args, "original_query", args.query)
    entity_resolution = apply_entity_expansion(args)
    database_path = args.database.resolve()
    if not database_path.exists():
        raise QueryError("database_not_found", f"Database not found: {database_path}")

    con = connect_duckdb(database_path, args.wait_for_db_seconds)
    try:
        index_info = get_index_info(con)
        filters = build_filters(args)
        if args.mode in {"vector", "hybrid"} and not table_exists(con, "rag_chunk_embeddings"):
            raise QueryError("missing_table", "Required table rag_chunk_embeddings was not found.")

        if args.mode == "vector":
            rows, query_info = retrieve_vector(con, args=args, index_info=index_info, filters=filters)
        elif args.mode == "text":
            rows, query_info = retrieve_text(con, args=args, filters=filters)
        elif args.mode == "hybrid":
            rows, query_info = retrieve_hybrid(con, args=args, index_info=index_info, filters=filters)
        else:
            raise QueryError("invalid_mode", f"Unsupported mode: {args.mode}")

        _filter_clauses, _filter_params, filter_payload = filters
        return {
            "ok": True,
            "query": args.original_query,
            "effective_query": args.query,
            "entity_resolution": entity_resolution,
            "mode": args.mode,
            "database": str(database_path),
            "index": index_info,
            "filters": {key: value for key, value in filter_payload.items() if value},
            "retrieval": {
                "top_k": args.top_k,
                "candidate_k": args.candidate_k,
                "model": args.model,
                "model_path": str(args.model_path) if args.model_path else os.environ.get(MODEL_PATH_ENV),
                "offline": args.offline,
                "vector_weight": args.vector_weight if args.mode == "hybrid" else None,
                "text_weight": args.text_weight if args.mode == "hybrid" else None,
                "exact_weight": args.exact_weight,
                "source_weight": args.source_weight,
                "rerank": args.rerank,
                "reranker_model": args.reranker_model if args.rerank else None,
                **query_info,
            },
            "results": format_results(dedupe_rows(rows, args.top_k), args.snippet_chars),
            "duration_ms": now_ms() - started_ms,
        }
    finally:
        con.close()


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def print_pretty(payload: dict[str, Any]) -> None:
    if not payload.get("ok"):
        print(f"ERROR [{payload.get('error_type')}]: {payload.get('message')}")
        return

    index = payload["index"]
    print(f"Query: {payload['query']}")
    if payload.get("effective_query") and payload["effective_query"] != payload["query"]:
        print(f"Expanded query: {payload['effective_query']}")
    entity_resolution = payload.get("entity_resolution") or {}
    if entity_resolution.get("entities") or entity_resolution.get("term_matches"):
        print(f"Entities: {json.dumps(entity_resolution, ensure_ascii=False)}")
    print(
        "Index: "
        f"rag_rows={index['rag_rows']} embedded_rows={index['embedded_rows']} "
        f"model={index['embedding_model']} dim={index['embedding_dim']} "
        f"partial={index['partial_index']}"
    )
    filters = payload.get("filters") or {}
    if filters:
        print(f"Filters: {json.dumps(filters, ensure_ascii=False)}")
    print()
    for item in payload["results"]:
        source = " / ".join(
            str(part)
            for part in [item.get("staging_source"), item.get("source_type"), item.get("source_name")]
            if part
        )
        components = item.get("score_components") or {}
        vector = components.get("vector")
        text = components.get("text")
        exact = components.get("exact")
        source_component = components.get("source")
        print(
            f"[{item['rank']}] score={item['score']:.6f} "
            f"vector={vector if vector is not None else '-'} text={text if text is not None else '-'} "
            f"exact={exact if exact is not None else '-'} source_boost={source_component if source_component is not None else '-'} "
            f"chunk_id={item['chunk_id']}"
        )
        if source:
            print(f"    source={source}")
        title_bits = [item.get("title"), item.get("heading")]
        title = " | ".join(str(bit) for bit in title_bits if bit)
        if title:
            print(f"    title={title}")
        if item.get("source_path"):
            print(f"    path={item['source_path']}")
        print(f"    {item['snippet']}")
        print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retrieve FahMai RAG chunks from DuckDB.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--query", default=None, help="Natural-language query. If omitted, stdin is used.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--candidate-k", type=int, default=DEFAULT_CANDIDATE_K)
    parser.add_argument("--mode", choices=["vector", "text", "hybrid"], default="vector")
    parser.add_argument("--staging-source", action="append", default=[])
    parser.add_argument("--source-type", action="append", default=[])
    parser.add_argument("--source-name", action="append", default=[])
    parser.add_argument("--expand-entities", action="store_true", help="Expand business IDs and aliases before retrieval.")
    parser.add_argument("--resolver-config", type=Path, default=DEFAULT_ROUTER_CONFIG)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-path", type=Path, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--offline", dest="offline", action="store_true", default=True)
    parser.add_argument("--no-offline", dest="offline", action="store_false")
    parser.add_argument("--snippet-chars", type=int, default=DEFAULT_SNIPPET_CHARS)
    parser.add_argument("--wait-for-db-seconds", type=int, default=0)
    parser.add_argument("--vector-weight", type=float, default=DEFAULT_VECTOR_WEIGHT)
    parser.add_argument("--text-weight", type=float, default=DEFAULT_TEXT_WEIGHT)
    parser.add_argument("--exact-weight", type=float, default=DEFAULT_EXACT_WEIGHT)
    parser.add_argument("--source-weight", type=float, default=DEFAULT_SOURCE_WEIGHT)
    parser.add_argument("--rerank", action="store_true", help="Reserved for cross-encoder reranking; not active yet.")
    parser.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    if args.query is None:
        args.query = sys.stdin.read().strip()
    else:
        args.query = args.query.strip()
    if not args.query:
        parser.error("--query is required unless a query is provided on stdin")
    if args.top_k < 1:
        parser.error("--top-k must be >= 1")
    if args.candidate_k < args.top_k:
        parser.error("--candidate-k must be >= --top-k")
    if args.snippet_chars < 0:
        parser.error("--snippet-chars must be >= 0")
    if args.wait_for_db_seconds < 0:
        parser.error("--wait-for-db-seconds must be >= 0")
    if args.vector_weight < 0 or args.text_weight < 0:
        parser.error("--vector-weight and --text-weight must be non-negative")
    if args.exact_weight < 0 or args.source_weight < 0:
        parser.error("--exact-weight and --source-weight must be non-negative")
    if args.rerank:
        parser.error("--rerank is reserved but not implemented yet; omit it for current retrieval.")
    return args


def main() -> int:
    started_ms = now_ms()
    args = parse_args()
    try:
        payload = run_query(args)
        if args.pretty:
            print_pretty(payload)
        else:
            print_json(payload)
        return 0
    except QueryError as exc:
        payload = payload_error(exc.error_type, exc.message, started_ms=started_ms)
    except Exception as exc:
        payload = payload_error(type(exc).__name__, str(exc), started_ms=started_ms)

    if args.pretty:
        print_pretty(payload)
    else:
        print_json(payload)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
