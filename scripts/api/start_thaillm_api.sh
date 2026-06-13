#!/usr/bin/env bash
# Start the FahMai guarded SQL/RAG agent API on :8888.
#   external:  http://swarm-manager.modelharbor.com:47378/agent/thaillm
#       -> 127.0.0.1:8888/agent/thaillm
# /agent/thaillm routes generation to the Typhoon-S ThaiLLM vLLM endpoint on :8002.
# The API server itself is CPU-only (embeddings on CPU), so it runs in the fahmai env.
set -euo pipefail

ENV=/root/data/miniforge3/envs/fahmai
APP_DIR="${FAHMAI_APP_DIR:-/root/data/API-Ready}"
cd "$APP_DIR"

export CUDA_VISIBLE_DEVICES=0

# --- Data + embedding model (RAG) ---
export FAHMAI_DATABASE="${FAHMAI_DATABASE:-$APP_DIR/data-parser/output/fahmai.duckdb}"
export FAHMAI_EMBEDDING_MODEL=BAAI/bge-m3
export FAHMAI_EMBEDDING_MODEL_PATH=/root/data/model/bge-m3
export FAHMAI_EMBEDDING_DEVICE=cpu
export FAHMAI_OFFLINE=true
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HOME=/root/data/hf-cache

# --- Generation model: ThaiLLM (Typhoon-S) on :8002 ---
# Both /agent/local and /agent/thaillm point at the ThaiLLM backend here, since
# this single-model setup serves only Typhoon on :8002.
export FAHMAI_LLM_MODE=openai_compatible
export FAHMAI_LLM_API_BASE=http://127.0.0.1:8002/v1
export FAHMAI_LLM_MODEL=typhoon-local
export FAHMAI_THAILLM_LLM_API_BASE=http://127.0.0.1:8002/v1
export FAHMAI_THAILLM_LLM_MODEL=typhoon-local
export FAHMAI_LLM_TIMEOUT=600

# --- Pipeline behaviour (matches the final submission run) ---
export FAHMAI_ENABLE_INPUT_GUARD=true
export FAHMAI_PIPELINE_MODE=execute
export FAHMAI_ENABLE_SQL_GENERATION=true
export FAHMAI_ENABLE_ANSWER_SYNTHESIS=true
export FAHMAI_ENABLE_SQL_TOOLS=true
export FAHMAI_SQL_TOOL_MODE=deterministic
export FAHMAI_TOP_K=3
export FAHMAI_CANDIDATE_K=30
export FAHMAI_SNIPPET_CHARS=360
export FAHMAI_LLM_SQL_MAX_TOKENS=2048
export FAHMAI_LLM_ANSWER_MAX_TOKENS=3072

# --- API bind ---
export FAHMAI_API_HOST=0.0.0.0
export FAHMAI_API_PORT=8888

exec "$ENV/bin/python" api_server.py
