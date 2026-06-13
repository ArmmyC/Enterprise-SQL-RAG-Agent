# FahMai SQL/RAG Agent API

FahMai is a guarded agent API that answers business questions with a shared SQL + RAG pipeline over DuckDB. It supports two OpenAI-compatible generation backends:

- `POST /agent/local` uses Qwen NVFP4 served by vLLM, intended for B200.
- `POST /agent/thaillm` uses ThaiLLM/Typhoon 8B served by vLLM.

Both endpoints share the same DuckDB database, BGE-M3 embedding/RAG layer, deterministic SQL tools, and input safety route.

## Repository Layout

```text
api_server.py                  FastAPI wrapper for the agent endpoints
data-parser/                   SQL/RAG orchestrator, router, tools, entity resolver
safety_route/                  Prompt-injection detector and safe-response layer
scripts/api/                   API launchers for local and B200 deployment
scripts/models/                vLLM launchers for Qwen and ThaiLLM/Typhoon
scripts/batch/                 Batch runner for data/questions.csv
scripts/smoke/                 Curl smoke test helper
data/questions.csv             Evaluation/input questions
data/sample_submission.csv     Submission template
docs/deployment.md             Batch and deployment notes
```

Large runtime files are intentionally ignored by Git. Put the DuckDB file at:

```text
data-parser/output/fahmai.duckdb
```

## Local API

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the API after configuring the environment:

```bash
cp .env.example .env
./scripts/api/start_api.sh
```

Smoke test:

```bash
curl -sS http://127.0.0.1:8888/agent/local \
  -H 'Content-Type: application/json' \
  -d '{"question":"MSRP ของสินค้ารหัส NT-LT-001 เป็นเท่าไหร่ครับ"}'
```

Response contract:

```json
{"id":"...","answer":"...","total_output_token_count":123}
```

## B200 Deployment

On the B200 host, run the model servers first:

```bash
./scripts/models/serve_qwen_vllm.sh
./scripts/models/serve_thaillm.sh
```

Then start the API:

```bash
FAHMAI_APP_DIR=/root/data/API-Ready ./scripts/api/start_fahmai_api.sh
```

Use `FAHMAI_LLM_API_BASE` / `FAHMAI_LLM_MODEL` for the Qwen route and `FAHMAI_THAILLM_LLM_API_BASE` / `FAHMAI_THAILLM_LLM_MODEL` for the ThaiLLM route. See `.env.example` and `docs/deployment.md` for the full deployment configuration.
