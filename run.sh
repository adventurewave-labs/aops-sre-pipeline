#!/usr/bin/env bash
# A.O.P.S. orchestrator for the sandbox (no Docker).
# Starts all 6 services as plain Python processes, lets you trigger an alert,
# and cleans up afterwards.
#
# Usage:
#   ./run.sh up        — start all services in background, wait for health
#   ./run.sh status     — show all processes + latest health probes
#   ./run.sh alert      — fire the PaymentAPIHighErrorRate alert
#   ./run.sh logs [svc] — tail logs (default: all services)
#   ./run.sh stop       — stop everything
#   ./run.sh restart    — stop + up
#   ./run.sh demo       — record an asciinema demo of the full flow

set -euo pipefail

AOPS_ROOT="${AOPS_ROOT:-$(cd "$(dirname "$0")" && pwd)}"
LOGDIR="$AOPS_ROOT/var/log"
PIDDIR="$AOPS_ROOT/var/pids"
mkdir -p "$LOGDIR" "$PIDDIR"

# Where Python lives — prefer venv if present
PY="${PYTHON:-python3}"

# Export shared config so all services see it
export MOCK_K8S_URL="${MOCK_K8S_URL:-http://localhost:8001}"
export POPEYE_URL="${POPEYE_URL:-http://localhost:8004}"
export DIFY_LITE_URL="${DIFY_LITE_URL:-http://localhost:8002}"
export MOCK_SLACK_URL="${MOCK_SLACK_URL:-http://localhost:8003}"
export OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:0.5b}"
export AOPS_LLM_BACKEND="${AOPS_LLM_BACKEND:-auto}"

# ---------------------------- helpers ----------------------------------------

start_svc() {
  local name="$1"; local script="$2"; local port="$3"
  local pidfile="$PIDDIR/$name.pid"
  local logfile="$LOGDIR/$name.log"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$pidfile"))"
    return
  fi
  echo "Starting $name on port $port..."
  nohup "$PY" "$script" >"$logfile" 2>&1 &
  echo $! > "$pidfile"
  sleep 0.3
}

wait_healthy() {
  local name="$1"; local port="$2"; local path="${3:-/healthz}"
  local tries=0
  while ! curl -fsS "http://localhost:$port$path" >/dev/null 2>&1; do
    tries=$((tries+1))
    if [[ $tries -ge 30 ]]; then
      echo "  $name did not become healthy after 30s; tailing log:"
      tail -20 "$LOGDIR/$name.log" || true
      return 1
    fi
    sleep 1
  done
  echo "  $name healthy"
}

stop_svc() {
  local name="$1"
  local pidfile="$PIDDIR/$name.pid"
  if [[ -f "$pidfile" ]]; then
    local pid; pid="$(cat "$pidfile")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "Stopping $name (pid $pid)..."
      kill "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.3
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  fi
}

# ---------------------------- commands --------------------------------------

cmd_up() {
  echo "=== A.O.P.S. stack — bringing up ==="

  start_svc mock-k8s-api   "$AOPS_ROOT/services/mock-k8s-api/app.py"        8001
  wait_healthy mock-k8s-api 8001 || return 1

  start_svc popeye-scanner "$AOPS_ROOT/services/popeye-scanner/app.py"      8004
  wait_healthy popeye-scanner 8004 || return 1

  start_svc mock-slack     "$AOPS_ROOT/services/mock-slack/app.py"         8003
  wait_healthy mock-slack 8003 || return 1

  start_svc dify-lite      "$AOPS_ROOT/services/dify-lite/app.py"         8002
  wait_healthy dify-lite 8002 || return 1

  start_svc n8n-runner     "$AOPS_ROOT/services/n8n-runner/app.py"        5678
  wait_healthy n8n-runner 5678 || return 1

  echo ""
  echo "All services up. Endpoints:"
  echo "  mock-k8s-api     http://localhost:8001  (mock Kubernetes API)"
  echo "  popeye-scanner   http://localhost:8004  (Popeye-shaped sanitizer JSON)"
  echo "  dify-lite        http://localhost:8002  (Dify-like chat completions)"
  echo "  mock-slack       http://localhost:8003  (Slack-card HTML page)"
  echo "  n8n-runner       http://localhost:5678  (Alertmanager webhook + workflow)"
  if [[ "$OLLAMA_STARTED" == "1" ]]; then
    echo "  ollama           http://localhost:11434 (LLM backend, model $OLLAMA_MODEL)"
  fi
  echo ""
  echo "Fire an alert:  $0 alert"
  echo "View Slack card: open http://localhost:8003 in a browser"
}

cmd_start_ollama() {
  if [[ -x "${HOME}/.local/bin/ollama" ]]; then
    if ! curl -fsS "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
      echo "Starting Ollama server..."
      OLLAMA_HOST=0.0.0.0:11434 nohup "${HOME}/.local/bin/ollama" serve \
        >"$LOGDIR/ollama.log" 2>&1 &
      echo $! > "$PIDDIR/ollama.pid"
      # Wait up to 30s for /api/tags
      for _ in $(seq 1 30); do
        if curl -fsS "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
          echo "  Ollama ready"
          break
        fi
        sleep 1
      done
      # Try to pull the model
      if curl -fsS "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; then
        echo "Pulling $OLLAMA_MODEL (may take a minute)..."
        if "${HOME}/.local/bin/ollama" pull "$OLLAMA_MODEL"; then
          echo "  Model ready"
          OLLAMA_STARTED=1
        else
          echo "  Model pull failed — dify-lite will use the stub backend"
          OLLAMA_STARTED=0
        fi
      fi
    else
      echo "Ollama already running"
      OLLAMA_STARTED=1
    fi
  else
    echo "Ollama binary not installed at ~/.local/bin/ollama"
    echo "  dify-lite will use the deterministic stub backend"
    OLLAMA_STARTED=0
  fi
  export OLLAMA_STARTED
}

cmd_status() {
  echo "=== A.O.P.S. status ==="
  for svc in mock-k8s-api popeye-scanner dify-lite mock-slack n8n-runner ollama; do
    pidfile="$PIDDIR/$svc.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      pid="$(cat "$pidfile")"
      printf "  %-18s RUNNING pid=%s\n" "$svc" "$pid"
    else
      printf "  %-18s STOPPED\n" "$svc"
    fi
  done
  echo ""
  echo "Health probes:"
  for port in 8001 8004 8002 8003 5678; do
    if curl -fsS --max-time 2 "http://localhost:$port/healthz" >/dev/null 2>&1; then
      printf "  :%d  OK\n" "$port"
    else
      printf "  :%d  --\n" "$port"
    fi
  done
}

cmd_alert() {
  "$AOPS_ROOT/services/prometheus-mock/alert.sh"
}

cmd_logs() {
  local svc="${1:-}"
  if [[ -n "$svc" ]]; then
    tail -f "$LOGDIR/$svc.log"
  else
    tail -f "$LOGDIR"/*.log
  fi
}

cmd_stop() {
  echo "=== Stopping A.O.P.S. ==="
  for svc in n8n-runner dify-lite mock-slack popeye-scanner mock-k8s-api ollama; do
    stop_svc "$svc"
  done
}

cmd_demo() {
  exec "$AOPS_ROOT/scripts/record_demo.sh"
}

# ---------------------------- main ------------------------------------------

OLLAMA_STARTED=0
case "${1:-up}" in
  up)
    cmd_start_ollama || true
    cmd_up
    ;;
  up-no-ollama)
    cmd_up
    ;;
  status)    cmd_status ;;
  alert)     cmd_alert ;;
  logs)      shift; cmd_logs "$@" ;;
  stop)      cmd_stop ;;
  restart)   cmd_stop; sleep 1; exec "$0" up ;;
  demo)      cmd_demo ;;
  *)
    echo "Usage: $0 {up|up-no-ollama|status|alert|logs [svc]|stop|restart|demo}"
    exit 1 ;;
esac
