#!/usr/bin/env bash
# Start the FahMai guarded SQL/RAG agent API on :8888 (the proxy-facing endpoints:
#   http://swarm-manager.modelharbor.com:<PORT>/agent/local    ->  127.0.0.1:8888/agent/local
#   http://swarm-manager.modelharbor.com:<PORT>/agent/thaillm  ->  127.0.0.1:8888/agent/thaillm )
# It routes /agent/local -> the internal vLLM Qwen endpoint on :8001 and
# /agent/thaillm -> the internal vLLM Typhoon-S ThaiLLM endpoint on :8002, and
# uses local bge-m3 + DuckDB. The API server itself is CPU-only (embeddings on CPU),
# so either conda env works; we use fahmai.
set -euo pipefail

ENV=/root/data/miniforge3/envs/fahmai
APP_DIR="${FAHMAI_APP_DIR:-/root/data/API-Ready}"
cd "$APP_DIR"

# MIG slice (numeric index 0; the slice is the only CUDA-visible device).
export CUDA_VISIBLE_DEVICES=0

# --- Data + models ---
export FAHMAI_DATABASE="${FAHMAI_DATABASE:-$APP_DIR/data-parser/output/fahmai.duckdb}"
export FAHMAI_EMBEDDING_MODEL=BAAI/bge-m3
export FAHMAI_EMBEDDING_MODEL_PATH=/root/data/model/bge-m3
# bge-m3 on CPU: keeps the shared MIG slice free for vLLM + the intermittent
# Jenkins app3 job; query embedding is tiny so CPU latency is negligible.
export FAHMAI_EMBEDDING_DEVICE=cpu
export FAHMAI_OFFLINE=true
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HOME=/root/data/hf-cache

# --- Generation models (internal vLLM OpenAI-compatible endpoints) ---
# Default backend for POST /agent/local -> Qwen on :8001.
export FAHMAI_LLM_MODE=openai_compatible
export FAHMAI_LLM_API_BASE=http://127.0.0.1:8001/v1
export FAHMAI_LLM_MODEL=qwen-local
export FAHMAI_LLM_TIMEOUT=600
# ThaiLLM backend for POST /agent/thaillm -> Typhoon-S on :8002. If unset, the
# /agent/thaillm route falls back to the default (Qwen) backend above.
export FAHMAI_THAILLM_LLM_API_BASE=http://127.0.0.1:8002/v1
export FAHMAI_THAILLM_LLM_MODEL=typhoon-local

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
# Larger token budgets so the reasoning model's post-<think> answer is not truncated.
export FAHMAI_LLM_SQL_MAX_TOKENS=2048
export FAHMAI_LLM_ANSWER_MAX_TOKENS=3072

# --- API bind ---
export FAHMAI_API_HOST=0.0.0.0
export FAHMAI_API_PORT=8888

exec "$ENV/bin/python" api_server.py
