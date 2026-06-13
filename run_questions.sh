#!/usr/bin/env bash
# Run the FahMai orchestrator over questions.csv against the local Qwen (vLLM :8001)
# + bge-m3 + DuckDB. Usage:
#   ./run_questions.sh [LIMIT] [OUTPUT_SUFFIX]
# e.g.  ./run_questions.sh 2 smoke    -> first 2 questions  -> test_submission/orchestrator_results_smoke.jsonl
#       ./run_questions.sh            -> all questions      -> test_submission/orchestrator_results.jsonl
set -euo pipefail

ENV=/root/data/miniforge3/envs/fahmai
APP_DIR="${FAHMAI_APP_DIR:-/root/data/API-Ready}"
cd "$APP_DIR"

LIMIT="${1:-}"
SUFFIX="${2:-}"
OUT="test_submission/orchestrator_results${SUFFIX:+_$SUFFIX}.jsonl"
mkdir -p test_submission

# Embeddings on CPU (keep the MIG slice for vLLM + the Jenkins app3 job).
export CUDA_VISIBLE_DEVICES=0
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HOME=/root/data/hf-cache
export TOKENIZERS_PARALLELISM=false

LIMIT_ARG=()
if [ -n "$LIMIT" ]; then LIMIT_ARG=(--limit "$LIMIT"); fi

set -x
exec "$ENV/bin/python" data-parser/run_orchestrator_csv.py \
  --questions-csv questions.csv \
  --output "$OUT" \
  --full --progress \
  --enable-input-guard --safety-route-dir safety_route \
  --database "$APP_DIR/data-parser/output/fahmai.duckdb" \
  --model-path /root/data/model/bge-m3 \
  --device cpu \
  --mode execute \
  --llm-mode openai_compatible \
  --llm-api-base http://127.0.0.1:8001/v1 \
  --llm-model qwen-local \
  --llm-timeout 600 \
  --enable-sql-generation \
  --enable-answer-synthesis \
  --llm-sql-max-tokens 2048 \
  --llm-answer-max-tokens 3072 \
  "${LIMIT_ARG[@]}"
