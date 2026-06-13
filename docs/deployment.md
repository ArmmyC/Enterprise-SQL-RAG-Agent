# FahMai Agent Deployment Bundle

This folder contains the code used for the final guarded SQL/RAG submission pipeline.

## Contents

- `data-parser/` - query orchestrator, router, deterministic SQL tools, RAG helpers, entity resolver, answer normalizer, and router config.
- `safety_route/` - prompt-injection detector and clean-intent extractor used by `run_orchestrator_csv.py --enable-input-guard`.
- `data-parser/output/` - local DuckDB runtime artifacts. This directory is kept
  with `.gitkeep`, but `*.duckdb` files are ignored because they are large.
- `api_server.py` - FastAPI wrapper exposing `/agent/local` and `/agent/thaillm`.
- `scripts/models/serve_qwen_vllm.sh` / `scripts/models/serve_thaillm.sh` - vLLM launchers for the two model backends.
- `_archive/` - local-only snapshot/zip backup from cleanup; ignored by Git.

## Main Entry Point

Run a batch:

```bash
python data-parser/run_orchestrator_csv.py \
  --questions-csv data/questions.csv \
  --output test_submission/orchestrator_results.jsonl \
  --full \
  --progress \
  --enable-input-guard \
  --safety-route-dir safety_route \
  --database data-parser/output/fahmai.duckdb \
  --model-path /path/to/models--BAAI--bge-m3/snapshots/<snapshot> \
  --mode execute \
  --llm-mode openai_compatible \
  --llm-api-base http://x1000c2s2b0n0:8000/v1 \
  --llm-model qwen-local \
  --llm-timeout 180 \
  --enable-answer-synthesis \
  --enable-sql-generation
```

Normalize a JSONL run into a submission CSV:

```bash
python data-parser/normalize_submission_answers.py \
  --sample-csv data/sample_submission.csv \
  --jsonl test_submission/orchestrator_results.jsonl \
  --output-csv test_submission/submission.csv \
  --fill-range 1-100 \
  --answer-format answer_only
```

## Runtime Assumptions

- Python environment has DuckDB and the model/RAG dependencies installed.
- BGE-M3 embedding model is available locally; on Lanta we used:
  `/project/zz992000-zdevb/zz992005/ub127/Hackathon4/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181`
- OpenAI-compatible chat endpoints are reachable at the configured `--llm-api-base`.
  On B200 two vLLM servers run on the MIG slice: Qwen (`qwen-local`) at
  `http://127.0.0.1:8001/v1` and Typhoon-S (`typhoon-local`) at `http://127.0.0.1:8002/v1`.
- Preferred database path for local/API runs:
  `data-parser/output/fahmai.duckdb`

## Safety Guard

The last 10 questions are prompt-injection/refusal cases. Use:

```bash
--enable-input-guard --safety-route-dir safety_route
```

The guard emits `clean_question`, `attack_types`, and routing decisions. It is a detector/sanitizer layer; final answers should still rely on trusted SQL/RAG evidence or canonical refusal wording.
