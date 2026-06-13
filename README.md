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
serve_qwen_vllm.sh             Starts the Qwen NVFP4 vLLM backend on :8001
serve_thaillm.sh               Starts the ThaiLLM/Typhoon vLLM backend on :8002
start_fahmai_api.sh            Starts the API with both backend routes configured
start_thaillm_api.sh           Starts the API pointed only at ThaiLLM/Typhoon
run_questions.sh               Batch runner for questions.csv
questions.csv                  Evaluation/input questions
sample_submission.csv          Submission template
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
./start_api.sh
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
./serve_qwen_vllm.sh
./serve_thaillm.sh
```

Then start the API:

```bash
FAHMAI_APP_DIR=/root/data/API-Ready ./start_fahmai_api.sh
```

Use `FAHMAI_LLM_API_BASE` / `FAHMAI_LLM_MODEL` for the Qwen route and `FAHMAI_THAILLM_LLM_API_BASE` / `FAHMAI_THAILLM_LLM_MODEL` for the ThaiLLM route. See `.env.example` and `README_DEPLOY.md` for the full deployment configuration.
