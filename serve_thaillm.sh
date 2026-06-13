#!/usr/bin/env bash
# Serve Typhoon-S ThaiLLM 8B Instruct (Qwen3ForCausalLM, dense bf16) via vLLM as an
# OpenAI-compatible endpoint on 127.0.0.1:$THAILLM_PORT (default 8002), served as
# "typhoon-local". This is the generation backend the FahMai API server (:8888)
# calls for POST /agent/thaillm.
#
# Notes:
#  * --enforce-eager avoids the triton "Failed to find C compiler" startup error
#    (this host has no system C compiler on PATH) and loads faster / lighter.
#  * NO --reasoning-parser: this instruct model is not a <think>-style reasoning
#    model; the qwen3 reasoning parser wrongly diverts its output into
#    reasoning_content and leaves message.content null. Leaving it off keeps the
#    answer in message.content where the pipeline reads it.
set -euo pipefail

ENV=/root/data/miniforge3/envs/thaillm
MODEL=/root/data/model/typhoon-s-thaillm-8b-instruct-research-preview
PORT="${THAILLM_PORT:-8002}"
GPU_UTIL="${THAILLM_GPU_UTIL:-0.35}"

# Pin to the single B200 MIG slice (3g.90gb). Numeric index, NOT the MIG-UUID.
export CUDA_VISIBLE_DEVICES=0
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HOME=/root/data/hf-cache
export VLLM_LOGGING_LEVEL=INFO
export XDG_CACHE_HOME=/root/data/.cache
export CC="$ENV/bin/x86_64-conda-linux-gnu-cc"
export CXX="$ENV/bin/x86_64-conda-linux-gnu-c++"

exec "$ENV/bin/vllm" serve "$MODEL" \
  --served-model-name typhoon-local \
  --host 127.0.0.1 \
  --port "$PORT" \
  --max-model-len 32768 \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-num-seqs 4 \
  --attention-backend FLASH_ATTN \
  --enforce-eager \
  --trust-remote-code
