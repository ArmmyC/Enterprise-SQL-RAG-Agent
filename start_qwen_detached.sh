#!/usr/bin/env bash
# Start the Qwen vLLM endpoint (:8001) as a fully detached daemon that survives
# the launching shell / SSH / Claude session disconnecting.
#
# Detachment: setsid (new session, no controlling tty) + nohup (SIGHUP-immune) +
# stdio redirected to a log. The vllm process reparents to init (PPID=1).
#
# Idempotent: refuses to start a second instance if the recorded PID is alive.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HERE/logs/qwen_vllm.log"
PIDFILE="$HERE/logs/qwen_vllm_8001.pid"
mkdir -p "$HERE/logs"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "qwen already running (PID $(cat "$PIDFILE")); not starting another."
  exit 0
fi

# Rotate the previous log so a crash trace is not mistaken for the new run.
if [[ -s "$LOG" ]]; then
  mv -f "$LOG" "$LOG.prev"
fi
: > "$LOG"

setsid nohup bash "$HERE/serve_qwen_vllm.sh" >"$LOG" 2>&1 < /dev/null &
disown 2>/dev/null || true

# Give vLLM a moment to fork its engine, then record the real vllm PID.
sleep 6
QPID="$(pgrep -f 'envs/fahmai/bin/vllm serve' | head -1 || true)"
if [[ -n "$QPID" ]]; then
  echo "$QPID" > "$PIDFILE"
  echo "started qwen vllm PID=$QPID  (log: $LOG)"
else
  echo "WARNING: vllm process not found after launch; check $LOG"
  exit 1
fi
