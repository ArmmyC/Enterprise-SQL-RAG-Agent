#!/usr/bin/env bash
# Quick test of the FahMai agent endpoint through the public proxy.
# Usage:
#   ./scripts/smoke/test_agent_curl.sh                # sends data/questions.csv #1 (L3-Q-EASY-001)
#   ./scripts/smoke/test_agent_curl.sh "your question" # sends a custom question
#
# Endpoint contract:
#   POST .../agent/thaillm   {"question": "..."}
#     -> {"id": "...", "answer": "...", "total_output_token_count": N}

URL="${FAHMAI_URL:-http://swarm-manager.modelharbor.com:47378/agent/thaillm}"

# Default = data/questions.csv question #1 (L3-Q-EASY-001)
DEFAULT_Q="MSRP ของสินค้ารหัส NT-LT-001 (NovaTech laptop) เป็นเท่าไหร่ครับ"
QUESTION="${1:-$DEFAULT_Q}"

echo ">> POST $URL"
echo ">> question: $QUESTION"
echo

# Build the JSON body safely (handles Thai/UTF-8, quotes, etc.) and POST it.
QUESTION="$QUESTION" python3 - <<'PY' > /tmp/_fahmai_body.json
import json, os
print(json.dumps({"question": os.environ["QUESTION"]}, ensure_ascii=False))
PY

curl -sS -m 600 -w '\n\n[http %{http_code}  %{time_total}s]\n' \
  -X POST "$URL" \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/_fahmai_body.json

rm -f /tmp/_fahmai_body.json
