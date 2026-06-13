#!/usr/bin/env bash
# Serve Qwen3.6-35B-A3B-NVFP4 (qwen3_5_moe) via vLLM as an OpenAI-compatible
# endpoint on 127.0.0.1:8001, served as "qwen-local".  Internal generation
# endpoint that the FahMai API server (:8888) calls for POST /agent/local.
#
# IMPORTANT: this host has no matching CUDA toolkit for flashinfer's runtime JIT
# (nvcc/CCCL version skew), so we use vLLM's PRECOMPILED backends only:
#   * MoE  -> marlin            (NVFP4 experts, no JIT)
#   * attn -> FLASH_ATTN        (precompiled vllm_flash_attn, no JIT)
#   * KV   -> default (bf16)    (fp8 KV would force a flashinfer fp8 GEMM JIT)
set -euo pipefail

ENV=/root/data/miniforge3/envs/fahmai
MODEL=/root/data/model/Qwen3.6-35B-A3B-NVFP4

# Pin to the single B200 MIG slice (3g.90gb). Numeric index, NOT the MIG-UUID
# (vLLM int()-parses CUDA_VISIBLE_DEVICES; a UUID crashes it).
export CUDA_VISIBLE_DEVICES=0
# Local weights only.
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HOME=/root/data/hf-cache
export VLLM_LOGGING_LEVEL=INFO
# Keep any caches under /root/data.
export XDG_CACHE_HOME=/root/data/.cache
# Force non-flashinfer FP4 MoE path (use marlin).
export VLLM_USE_FLASHINFER_MOE_FP4=0

# The model's FP8 GDN/linear-attention layers use flashinfer's bmm_fp8, which has
# NO non-flashinfer fallback and must JIT-compile a cutlass fp8 kernel for sm_100.
# Point CUDA_HOME at the bundled cu13 toolkit so nvcc is found.  That wheel ships
# nvcc 13.2 but CUDART headers 13.0, so flashinfer's CCCL version-equality check
# (#error) trips; disable it via NVCC_PREPEND_FLAGS. A 13.2 compiler + 13.0
# headers links cleanly against the cu130 torch runtime (one-time JIT, cached).
export CUDA_HOME="$ENV/lib/python3.12/site-packages/nvidia/cu13"
export PATH="$CUDA_HOME/bin:$PATH"
# Host C/C++ compiler. The fahmai env ships no gcc/cc, but Triton must JIT-compile
# the Qwen3-VL vision-tower kernels at startup (profile_run) and nvcc/flashinfer
# need a host compiler too. Borrow the conda gcc 15 toolchain from the thaillm env
# via stable cc/gcc/c++/g++ symlinks under .toolchain/bin (created once).
HOST_TOOLCHAIN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.toolchain/bin"
export CC="$HOST_TOOLCHAIN/cc"
export CXX="$HOST_TOOLCHAIN/c++"
# Put the fahmai env's bin on PATH too: the JIT/torch-compile step shells out to
# `ninja` (and other build tools) which live in $ENV/bin but are otherwise not on
# PATH because we invoke vllm by absolute path rather than activating the env.
export PATH="$HOST_TOOLCHAIN:$ENV/bin:$PATH"
# The cu13 wheel ships a runtime layout (libX.so.13, no libX.so / no lib64). We
# created libX.so dev symlinks + a lib64->lib symlink so flashinfer's JIT can link
# (-lcudart/-lcublas/...). Add the dir to link- and run-time search paths too.
# libcuda.so (the CUDA *driver* library, -lcuda) is NOT in the cu13 wheel; it ships
# with the NVIDIA driver under /usr/lib/x86_64-linux-gnu. flashinfer's one-time
# fp8_gemm_cutlass JIT links against it, so add the driver dir to the search paths.
DRIVER_LIB=/usr/lib/x86_64-linux-gnu
export LIBRARY_PATH="$CUDA_HOME/lib:$DRIVER_LIB${LIBRARY_PATH:+:$LIBRARY_PATH}"
export LD_LIBRARY_PATH="$CUDA_HOME/lib:$DRIVER_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export NVCC_PREPEND_FLAGS="-ccbin $HOST_TOOLCHAIN/g++ -DCCCL_DISABLE_CTK_COMPATIBILITY_CHECK"
# Recommended by the model card; relaxes flashinfer's Python-level version check.
export FLASHINFER_DISABLE_VERSION_CHECK=1
# This container has a ~48.9 GiB cgroup memory cap. CUTLASS sm_100 FP8 compiles are
# memory-hungry; ninja's default parallelism (~all cores) OOM-kills nvcc. Cap it.
export MAX_JOBS=2
# Keep flashinfer's JIT workspace/cache under /root/data (defaults to $HOME).
export FLASHINFER_WORKSPACE_BASE=/root/data/.cache/flashinfer

exec "$ENV/bin/vllm" serve "$MODEL" \
  --served-model-name qwen-local \
  --host 127.0.0.1 \
  --port 8001 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.45 \
  --max-num-seqs 4 \
  --moe-backend marlin \
  --attention-backend FLASH_ATTN \
  --reasoning-parser qwen3 \
  --trust-remote-code
