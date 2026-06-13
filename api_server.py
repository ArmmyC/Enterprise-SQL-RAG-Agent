#!/usr/bin/env python3
"""
HTTP API wrapper for the FahMai guarded SQL/RAG pipeline.

Endpoints (same request/response contract, different generation backend):
  POST /agent/local    -> default LLM   (FAHMAI_LLM_API_BASE / FAHMAI_LLM_MODEL)
  POST /agent/thaillm  -> ThaiLLM model (FAHMAI_THAILLM_LLM_API_BASE / FAHMAI_THAILLM_LLM_MODEL)
  {"question": "..."} -> {"id": "...", "answer": "...", "total_output_token_count": int}

Both routes share the same DuckDB + bge-m3 RAG layer and the same input guard;
only the OpenAI-compatible chat backend differs. If the THAILLM_* vars are unset,
/agent/thaillm falls back to the same backend as /agent/local.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field


ROOT_DIR = Path(__file__).resolve().parent
DATA_PARSER_DIR = ROOT_DIR / "data-parser"
SAFETY_ROUTE_DIR = ROOT_DIR / "safety_route"
DEFAULT_DB = DATA_PARSER_DIR / "output" / "fahmai_no_ocr.duckdb"
FALLBACK_DB = DATA_PARSER_DIR / "output" / "fahmai.duckdb"

for import_path in (DATA_PARSER_DIR, SAFETY_ROUTE_DIR / "src"):
    import_text = str(import_path)
    if import_text not in sys.path:
        sys.path.insert(0, import_text)

import query_orchestrator  # noqa: E402
import query_rag  # noqa: E402
import run_orchestrator_csv  # noqa: E402


class AgentRequest(BaseModel):
    question: str = Field(..., min_length=1)


class AgentResponse(BaseModel):
    id: str
    answer: str
    total_output_token_count: int


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


def env_path(name: str, default: Path | None = None) -> Path | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    path = Path(value.strip())
    return path if path.is_absolute() else ROOT_DIR / path


def configured_database() -> Path:
    explicit = env_path("FAHMAI_DATABASE")
    if explicit:
        return explicit
    return DEFAULT_DB if DEFAULT_DB.exists() else FALLBACK_DB


def build_base_args() -> argparse.Namespace:
    model_path = env_path("FAHMAI_EMBEDDING_MODEL_PATH") or env_path("MODEL_PATH")
    return SimpleNamespace(
        enable_query_understanding=env_bool("FAHMAI_ENABLE_QUERY_UNDERSTANDING", True),
        database=configured_database(),
        router_config=env_path("FAHMAI_ROUTER_CONFIG", DATA_PARSER_DIR / "router_config.json"),
        router_mode=os.environ.get("FAHMAI_ROUTER_MODE", "rules"),
        mode=os.environ.get("FAHMAI_PIPELINE_MODE", "execute"),
        sql_limit=env_int("FAHMAI_SQL_LIMIT", 50),
        sample_rows=env_int("FAHMAI_SAMPLE_ROWS", 3),
        top_k=env_int("FAHMAI_TOP_K", 3),
        candidate_k=env_int("FAHMAI_CANDIDATE_K", 30),
        snippet_chars=env_int("FAHMAI_SNIPPET_CHARS", 360),
        wait_for_db_seconds=env_int("FAHMAI_WAIT_FOR_DB_SECONDS", 0),
        model=os.environ.get("FAHMAI_EMBEDDING_MODEL", query_orchestrator.DEFAULT_MODEL),
        model_path=model_path,
        device=os.environ.get("FAHMAI_EMBEDDING_DEVICE") or None,
        offline=env_bool("FAHMAI_OFFLINE", True),
        vector_weight=env_float("FAHMAI_VECTOR_WEIGHT", query_rag.DEFAULT_VECTOR_WEIGHT),
        text_weight=env_float("FAHMAI_TEXT_WEIGHT", query_rag.DEFAULT_TEXT_WEIGHT),
        exact_weight=env_float("FAHMAI_EXACT_WEIGHT", query_rag.DEFAULT_EXACT_WEIGHT),
        source_weight=env_float("FAHMAI_SOURCE_WEIGHT", query_rag.DEFAULT_SOURCE_WEIGHT),
        expand_entities=env_bool("FAHMAI_EXPAND_ENTITIES", True),
        llm_mode=os.environ.get("FAHMAI_LLM_MODE", "openai_compatible"),
        llm_api_base=os.environ.get("FAHMAI_LLM_API_BASE") or os.environ.get("LLM_API_BASE"),
        llm_model=os.environ.get("FAHMAI_LLM_MODEL") or os.environ.get("LLM_MODEL"),
        llm_timeout=env_float("FAHMAI_LLM_TIMEOUT", 180.0),
        enable_sql_generation=env_bool("FAHMAI_ENABLE_SQL_GENERATION", True),
        enable_answer_synthesis=env_bool("FAHMAI_ENABLE_ANSWER_SYNTHESIS", True),
        enable_sql_tools=env_bool("FAHMAI_ENABLE_SQL_TOOLS", True),
        sql_tool_mode=os.environ.get("FAHMAI_SQL_TOOL_MODE", "deterministic"),
        sql_tool_agent_max_attempts=env_int("FAHMAI_SQL_TOOL_AGENT_MAX_ATTEMPTS", 2),
        enable_react_fallback=env_bool("FAHMAI_ENABLE_REACT_FALLBACK", False),
        react_max_steps=env_int("FAHMAI_REACT_MAX_STEPS", 6),
        react_temperature=env_float("FAHMAI_REACT_TEMPERATURE", 0.0),
        react_max_tokens=env_int("FAHMAI_REACT_MAX_TOKENS", 900),
        llm_sql_temperature=env_float("FAHMAI_LLM_SQL_TEMPERATURE", 0.0),
        llm_sql_max_tokens=env_int("FAHMAI_LLM_SQL_MAX_TOKENS", 512),
        llm_answer_temperature=env_float("FAHMAI_LLM_ANSWER_TEMPERATURE", 0.2),
        llm_answer_max_tokens=env_int("FAHMAI_LLM_ANSWER_MAX_TOKENS", 1024),
        llm_context_chunks=env_int("FAHMAI_LLM_CONTEXT_CHUNKS", 5),
        require_answer_ready=env_bool("FAHMAI_REQUIRE_ANSWER_READY", False),
        pretty=False,
    )


def build_thaillm_args(base: argparse.Namespace) -> argparse.Namespace:
    """Copy of the base args overridden to talk to the ThaiLLM generation backend.

    Only the OpenAI-compatible chat endpoint differs; RAG/DuckDB/guard settings are
    shared. Falls back to the base backend when the THAILLM_* vars are not set."""
    args = copy.copy(base)
    args.llm_api_base = (
        os.environ.get("FAHMAI_THAILLM_LLM_API_BASE") or base.llm_api_base
    )
    args.llm_model = os.environ.get("FAHMAI_THAILLM_LLM_MODEL") or base.llm_model
    return args


def estimate_output_tokens(text: str) -> int:
    if not text:
        return 0
    latin_like = re.findall(r"\w+|[^\s\w]", text, flags=re.UNICODE)
    byte_estimate = math.ceil(len(text.encode("utf-8")) / 4)
    return max(1, min(max(len(latin_like), byte_estimate), len(text.encode("utf-8"))))


def answer_from_payload(payload: dict[str, Any]) -> str:
    package = payload.get("answer_package") or {}
    answer = package.get("answer_text")
    if answer:
        return str(answer).strip()

    sql_results = package.get("sql_results") or []
    if sql_results:
        return json.dumps(sql_results, ensure_ascii=False, default=str)

    notes = package.get("notes") or payload.get("validation", {}).get("warnings") or []
    if notes:
        return "ไม่สามารถสร้างคำตอบสุดท้ายได้: " + "; ".join(str(note) for note in notes)
    if payload.get("message"):
        return "ไม่สามารถสร้างคำตอบได้: " + str(payload["message"])
    return "ไม่สามารถสร้างคำตอบได้จากข้อมูลที่มี"


def token_count_from_payload(payload: dict[str, Any], answer: str) -> int:
    package = payload.get("answer_package") or {}
    count = package.get("total_output_token_count")
    if isinstance(count, int) and count >= 0:
        return count
    return estimate_output_tokens(answer)


def make_safe_refusal(row: dict[str, str], guarded_row: dict[str, str], generation_id: str) -> AgentResponse:
    answer = guarded_row.get("guard_final_safe_response") or (
        "คำถามนี้มีคำสั่งแทรกที่ไม่ปลอดภัย จึงไม่ดำเนินการตามคำสั่งแทรกดังกล่าว"
    )
    return AgentResponse(id=generation_id, answer=answer, total_output_token_count=estimate_output_tokens(answer))


app = FastAPI(title="FahMai Agent API", version="1.0.0")
_base_args = build_base_args()
_thaillm_args = build_thaillm_args(_base_args)
_safety_pipeline = (
    run_orchestrator_csv.load_safety_pipeline(SAFETY_ROUTE_DIR)
    if env_bool("FAHMAI_ENABLE_INPUT_GUARD", True)
    else None
)


def _backend_info(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "llm_api_base": args.llm_api_base or query_orchestrator.DEFAULT_LLM_API_BASE,
        "llm_model": args.llm_model or query_orchestrator.DEFAULT_LLM_MODEL,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "database": str(_base_args.database),
        "embedding_model": _base_args.model,
        "embedding_model_path": str(_base_args.model_path) if _base_args.model_path else None,
        "backends": {
            "/agent/local": _backend_info(_base_args),
            "/agent/thaillm": _backend_info(_thaillm_args),
        },
    }


def _run_agent(request: AgentRequest, base_args: argparse.Namespace) -> AgentResponse:
    generation_id = uuid.uuid4().hex
    question = request.question.strip()
    if not question:
        answer = "กรุณาส่งคำถามที่ไม่ว่าง"
        return AgentResponse(id=generation_id, answer=answer, total_output_token_count=estimate_output_tokens(answer))

    row = {"id": generation_id, "question": question}
    guarded_row = run_orchestrator_csv.apply_input_guard(base_args, row, _safety_pipeline)

    if guarded_row.get("guard_route_allowed") == "false":
        return make_safe_refusal(row, guarded_row, generation_id)

    orchestrator_args = run_orchestrator_csv.make_orchestrator_args(base_args, guarded_row)
    payload = query_orchestrator.run_orchestrator(orchestrator_args)
    answer = answer_from_payload(payload)
    return AgentResponse(
        id=generation_id,
        answer=answer,
        total_output_token_count=token_count_from_payload(payload, answer),
    )


@app.post("/agent/local", response_model=AgentResponse)
def agent_local(request: AgentRequest) -> AgentResponse:
    return _run_agent(request, _base_args)


@app.post("/agent/thaillm", response_model=AgentResponse)
def agent_thaillm(request: AgentRequest) -> AgentResponse:
    return _run_agent(request, _thaillm_args)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "api_server:app",
        host=os.environ.get("FAHMAI_API_HOST", "0.0.0.0"),
        port=env_int("PORT", env_int("FAHMAI_API_PORT", 8888)),
        reload=env_bool("FAHMAI_API_RELOAD", False),
    )


if __name__ == "__main__":
    main()
