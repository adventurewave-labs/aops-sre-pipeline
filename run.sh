#!/usr/bin/env bash
# A.O.P.S. orchestrator — supports REAL and SANDBOX modes.
#
# Usage:
#   ./run.sh up              — start services (respects AOPS_MODE)
#   ./run.sh up-sandbox      — force sandbox mode (mock K8s API)
#   ./run.sh up-real         — force real mode (needs kind cluster)
#   ./run.sh status          — show services + health probes
#   ./run.sh alert           — fire the PaymentAPIHighErrorRate alert
#   ./run.sh remediate       — run real remediation + before/after
#   ./run.sh logs [svc]      — tail logs (default: all services)
#   ./run.sh stop            — stop everything
#   ./run.sh restart         — stop + up
#
# Environment (or .env file):
#   AOPS_MODE        real|sandbox (default: real)
#   OLLAMA_MODEL     qwen2.5:0.5b|llama3.1:8b
#   DRY_RUN          0|1

set -euo pipefail

AOPS_ROOT="${AOPS_ROOT:-$(cd "$(dirname "$0")" && pwd)}"
LOGDIR="$AOPS_ROOT/var/log"
PIDDIR="$AOPS_ROOT/var/pids"
mkdir -p "$LOGDIR" "$PIDDIR"

# Load .env if it exists
if [[ -f "$AOPS_ROOT/.env" ]]; then
  set -a
  source "$AOPS_ROOT/.env"
  set +a
fi

PY="${PYTHON:-python3}"
AOPS_MODE="${AOPS_MODE:-sandbox}"
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:0.5b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

export MOCK_K8S_URL="${MOCK_K8S_URL:-http://localhost:8001}"
export POPEYE_URL="${POPEYE_URL:-http://localhost:8004}"
export DIFY_LITE_URL="${DIFY_LITE_URL:-http://localhost:8002}"
export MOCK_SLACK_URL="${MOCK_SLACK_URL:-http://localhost:8003}"
export OLLAMA_URL="$OLLAMA_URL"
export OLLAMA_MODEL="$OLLAMA_MODEL"
export AOPS_LLM_BACKEND="${AOPS_LLM_BACKEND:-ollama}"
export AOPS_NAMESPACE="payment-prod"

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

cmd_up_sandbox() {
  echo "=== A.O.P.S. stack — SANDBOX mode ==="
  echo ""

  start_svc mock-k8s-api   "$AOPS_ROOT/services/mock-k8s-api/app.py"        8001
  wait_healthy mock-k8s-api 8001 || return 1

  export POPEYE_MODE=builtin
  start_svc popeye-scanner "$AOPS_ROOT/services/popeye-scanner/app.py"      8004
  wait_healthy popeye-scanner 8004 || return 1

  start_svc mock-slack     "$AOPS_ROOT/services/mock-slack/app.py"         8003
  wait_healthy mock-slack 8003 || return 1

  export AOPS_LLM_BACKEND=ollama
  start_svc dify-lite      "$AOPS_ROOT/services/dify-lite/app.py"         8002
  wait_healthy dify-lite 8002 || return 1

  start_svc n8n-runner     "$AOPS_ROOT/services/n8n-runner/app.py"        5678
  wait_healthy n8n-runner 5678 || return 1

  _print_endpoints "sandbox"
}

cmd_up_real() {
  echo "=== A.O.P.S. stack — REAL mode ==="
  echo ""

  # Check for real K8s access
  if ! kubectl get ns payment-prod >/dev/null 2>&1; then
    echo "ERROR: Cannot access namespace 'payment-prod' on the current cluster."
    echo "  Run: ./scripts/setup-kind-cluster.sh up"
    echo "  Or:  export KUBECONFIG=/path/to/kubeconfig"
    return 1
  fi
  echo "  Connected to real K8s cluster"
  echo ""

  export POPEYE_MODE=real
  start_svc popeye-scanner "$AOPS_ROOT/services/popeye-scanner/app.py"      8004
  wait_healthy popeye-scanner 8004 || return 1

  start_svc mock-slack     "$AOPS_ROOT/services/mock-slack/app.py"         8003
  wait_healthy mock-slack 8003 || return 1

  export AOPS_LLM_BACKEND=ollama
  start_svc dify-lite      "$AOPS_ROOT/services/dify-lite/app.py"         8002
  wait_healthy dify-lite 8002 || return 1

  start_svc n8n-runner     "$AOPS_ROOT/services/n8n-runner/app.py"        5678
  wait_healthy n8n-runner 5678 || return 1

  start_svc remediation-executor "$AOPS_ROOT/services/remediation-executor/app.py" 8005
  wait_healthy remediation-executor 8005 || return 1

  _print_endpoints "real"
}

_print_endpoints() {
  local mode="$1"
  echo ""
  echo "All services up ($mode mode). Endpoints:"
  if [[ "$mode" == "sandbox" ]]; then
    echo "  mock-k8s-api         http://localhost:8001  (mock Kubernetes API)"
  fi
  echo "  popeye-scanner       http://localhost:8004  (Popeye: $POPEYE_MODE)"
  echo "  dify-lite            http://localhost:8002  (LLM backend: $AOPS_LLM_BACKEND)"
  echo "  slack-receiver       http://localhost:8003  (Slack card viewer)"
  echo "  n8n-runner           http://localhost:5678  (webhook + workflow)"
  if [[ "$mode" == "real" ]]; then
    echo "  remediation-executor http://localhost:8005  (kubectl fix executor)"
  fi
  echo ""
  echo "Fire an alert:       $0 alert"
  echo "Run remediation:      $0 remediate"
  echo "View Slack card:      open http://localhost:8003"
}

cmd_status() {
  echo "=== A.O.P.S. status (mode=$AOPS_MODE) ==="
  for svc in mock-k8s-api popeye-scanner dify-lite mock-slack n8n-runner ollama remediation-executor; do
    pidfile="$PIDDIR/$svc.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      pid="$(cat "$pidfile")"
      printf "  %-25s RUNNING pid=%s\n" "$svc" "$pid"
    else
      printf "  %-25s STOPPED\n" "$svc"
    fi
  done
  echo ""
  echo "Health probes:"
  for port in 8001 8004 8002 8003 5678 8005; do
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

cmd_remediate() {
  echo "=== Running real remediation ==="
  echo ""
  # Before scan
  echo "BEFORE scan..."
  curl -sS -X POST "http://localhost:8004/scan?namespace=payment-prod" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'  score={d[\"score\"]}/100  grade={d[\"grade\"]}  findings={d[\"findings_count\"]}')
print(f'  errors={d[\"findings_by_severity\"][\"error\"]}  warnings={d[\"findings_by_severity\"][\"warning\"]}')
"
  echo ""
  echo "Applying fixes..."
  curl -sS -X POST "http://localhost:8005/remediate" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'  Status: {d[\"status\"]}')
print(f'  Duration: {d[\"duration_s\"]}s')
print(f'  DRY_RUN: {d[\"dry_run\"]}')
print()
b = d['before']
a = d['after']
print(f'  BEFORE: score={b[\"score\"]}/100 grade={b[\"grade\"]} findings={b[\"findings_count\"]} (E:{b[\"errors\"]} W:{b[\"warnings\"]})')
print(f'  AFTER:  score={a[\"score\"]}/100 grade={a[\"grade\"]} findings={a[\"findings_count\"]} (E:{a[\"errors\"]} W:{a[\"warnings\"]})')
print(f'  DELTA:  score +{d[\"improvement\"][\"score_delta\"]}  findings -{d[\"improvement\"][\"findings_delta\"]}')
print()
print('  Steps:')
for s in d['steps']:
    sym = 'OK' if s['ok'] else 'FAILED'
    print(f'    [{sym}] {s[\"step\"]}')
"
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
  for svc in remediation-executor n8n-runner dify-lite mock-slack popeye-scanner mock-k8s-api ollama; do
    stop_svc "$svc"
  done
}

# ---------------------------- main ------------------------------------------

case "${1:-up}" in
  up)          cmd_up_${AOPS_MODE:-sandbox} ;;
 up-sandbox)  AOPS_MODE=sandbox cmd_up_sandbox ;;
 up-real)     AOPS_MODE=real cmd_up_real ;;
 status)      cmd_status ;;
 alert)       cmd_alert ;;
 remediate)   cmd_remediate ;;
 logs)        shift; cmd_logs "$@" ;;
 stop)        cmd_stop ;;
 restart)     cmd_stop; sleep 1; exec "$0" up ;;
 *)
    echo "Usage: $0 {up|up-sandbox|up-real|status|alert|remediate|logs [svc]|stop|restart}"
    echo ""
    echo "  up            Start services (respects AOPS_MODE env var)"
    echo "  up-sandbox    Force sandbox mode (mock K8s API, no cluster)"
    echo "  up-real       Force real mode (needs kind cluster + broken resources)"
    echo "  status        Show services + health probes"
    echo "  alert         Fire the PaymentAPIHighErrorRate alert"
    echo "  remediate     Run real remediation with before/after comparison"
    echo "  logs [svc]    Tail logs (default: all services)"
    echo "  stop          Stop everything"
    echo "  restart       Stop + up"
    exit 1
    ;;
esac
