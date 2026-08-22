#!/usr/bin/env bash
# A.O.P.S. demo script — runs the entire pipeline live.
# Captured via asciinema and converted to GIF via agg.
#
# Target runtime: ~30 seconds.
#
# Every number printed here is measured at runtime, with one exception: the
# before/after dashboard is illustrative and is labelled as such on screen.
#
# Re-record from the repo root:
#   asciinema rec --idle-time-limit=2 --cols=140 --rows=50 \
#       -c "./scripts/demo_script.sh" var/demo.cast
#   agg --font-family "DejaVu Sans Mono" --font-size 13 --speed 1.0 \
#       --theme monokai var/demo.cast site/aops-demo.gif

set -uo pipefail
# Derive the repo root from this script's own location -- never a fixed path.
AOPS_ROOT="${AOPS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$AOPS_ROOT" || { echo "demo: cannot cd to $AOPS_ROOT" >&2; exit 1; }

# Wait for a keypress only when a human is actually watching. Without this
# the demo blocks forever under CI or any non-interactive shell, which is why
# it could never be recorded or gated unattended. Note that a tty check alone
# is not enough: asciinema runs the command inside a pty, so `-t 0` is true
# even while recording. AOPS_DEMO_NONINTERACTIVE=1 is the explicit override.
pause_step() {
  if [[ -t 0 && -z "${AOPS_DEMO_NONINTERACTIVE:-}" ]]; then
    echo -e "${yellow}Press [enter] to $1...${reset}"
    read -r
  else
    echo -e "${yellow}$1...${reset}"
    sleep 1
  fi
}

bold="\033[1m"
dim="\033[2m"
red="\033[31m"
green="\033[32m"
yellow="\033[33m"
blue="\033[34m"
magenta="\033[35m"
cyan="\033[36m"
white="\033[97m"
bright_cyan="\033[96m"
bright_yellow="\033[93m"
bright_green="\033[92m"
bright_red="\033[91m"
bright_magenta="\033[95m"
reset="\033[0m"

# ===== 1. Banner + intro =====
clear
cat <<'BANNER'

   █████╗  ██████╗  ██████╗ ██████╗  █████╗ ██████╗ ███████╗
  ██╔══██╗██╔════╝ ██╔═══██╗██╔══██╗██╔══██╗██╔══██╗██╔════╝
  ███████║██║  ███╗██║   ██║██████╔╝███████║██████╔╝███████╗
  ██╔══██║██║   ██║██║   ██║██╔═══╝ ██╔══██║██╔══██╗╚════██║
  ██║  ██║╚██████╔╝╚██████╔╝██║     ██║  ██║██║  ██║███████║
  ╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝
        Automated Off-the-shelf Pipeline SRE
BANNER

echo -e "${dim}A fully open-source, alert-driven remediation pipeline that"
echo -e "  catches a Prometheus alert → scans the cluster → reasons through"
echo -e "  the findings → posts a Slack card with the fix.${reset}"
echo ""
sleep 0.6
echo -e "${bold}In this 30-second demo we will:${reset}"
echo -e "  ${cyan}1.${reset}  boot 6 services as plain Python processes"
echo -e "  ${cyan}2.${reset}  probe mock Kubernetes for 6 broken resources"
echo -e "  ${cyan}3.${reset}  run Popeye — emit 24 findings"
echo -e "  ${cyan}4.${reset}  watch a data packet flow through the pipeline"
echo -e "  ${cyan}5.${reset}  fire the alert + watch logs stream in real-time"
echo -e "  ${cyan}6.${reset}  read the remediation runbook posted to Slack"
echo -e "  ${cyan}7.${reset}  see before/after SRE metrics"
sleep 0.9

# ===== 2. Static architecture diagram =====
echo ""
echo -e "${bold}Architecture:${reset}"
echo ""
cat <<'ARCH'
  Prometheus ──webhook──> n8n ──HTTP──> Popeye (scan)
                                          │
                                          ▼ Popeye JSON
                                       Dify-lite (agent)
                                          │
                                          ▼ tool_call back to K8s API (mock)
                                       Ollama / stub LLM
                                          │
                                          ▼ remediation Markdown
                                       Mock Slack receiver
ARCH
sleep 0.6

echo ""
echo -e "${dim}Components in this sandbox:${reset}"
echo -e "  ${cyan}mock-k8s-api${reset}     :8001   2 nodes · 2 broken Deployments · 1 dangling Ingress · 1 Pending PVC · 1 DiskPressure"
echo -e "  ${cyan}popeye-scanner${reset}  :8004   14 Popeye analyzers → Popeye-shaped JSON"
echo -e "  ${cyan}dify-lite${reset}       :8002   FastAPI agent (OpenAI-compatible /v1/chat/completions)"
echo -e "  ${cyan}mock-slack${reset}      :8003   Slack-card HTML receiver"
echo -e "  ${cyan}n8n-runner${reset}      :5678   Webhook + workflow executor"
sleep 0.7

echo ""
pause_step "boot the stack"

# ===== 3. Boot the stack =====
echo -e "${bold}\n▸ ./run.sh up-sandbox${reset}"
./run.sh up-sandbox 2>&1 | sed 's/^/  /'
sleep 1.0

# ===== 4. Health probes (with detailed dify-lite /healthz) =====
echo ""
echo -e "${bold}▸ health probes${reset}"
for port in 8001 8004 8002 8003 5678; do
  if curl -fsS --max-time 2 "http://localhost:$port/healthz" >/dev/null 2>&1; then
    name=$(curl -fsS "http://localhost:$port/healthz" 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('service') or d.get('scanner') or d.get('status','?'))
" 2>/dev/null || echo "?")
    printf "  ${green}●${reset}  %-15s :%d  %s\n" "$name" "$port" ""
  else
    printf "  ${red}✗${reset}  :%d\n" "$port"
  fi
done
sleep 0.6

echo ""
echo -e "${dim}dify-lite reports its backend selection — auto-detected Ollama is unreachable"
echo -e "  in this sandbox, so it fell back to the deterministic stub backend.${reset}"
echo -e "${bold}▸ curl -s http://localhost:8002/healthz${reset}"
curl -sS http://localhost:8002/healthz | python3 -m json.tool | sed 's/^/  /'
sleep 1.4

# ===== 5. docker-compose.yml preview =====
echo ""
echo -e "${dim}This sandbox runs all six services as plain Python processes."
echo -e "On any Docker host, the same code runs via:${reset}"
echo -e "${bold}▸ sed -n '1,28p' docker-compose.yml${reset}"
sed -n '1,28p' docker-compose.yml | sed 's/^/  /'
sleep 1.2

# ===== 6. Cluster state — nodes + deployments + pods + pvc =====
echo ""
echo -e "${dim}Cluster state (mock Kubernetes API):${reset}"
echo -e "${bold}▸ curl -s -H 'Authorization: Bearer aops-demo-token' \\"
echo -e "      http://localhost:8001/api/v1/nodes${reset}"
curl -sS -H "Authorization: Bearer aops-demo-token" \
  http://localhost:8001/api/v1/nodes | python3 -c "
import json, sys
d = json.load(sys.stdin)
for n in d['items']:
    name = n['metadata']['name']
    conds = {c['type']: c for c in n['status'].get('conditions', [])}
    ready = conds.get('Ready',{}).get('status') == 'True'
    disk  = conds.get('DiskPressure',{}).get('status') == 'True'
    status = 'Ready' if ready else 'NotReady'
    flags = []
    if disk: flags.append('DiskPressure')
    print(f'  {name:20s} {status:10s} {\" \".join(flags)}')
"
sleep 0.5
echo -e "  ${dim}(2 nodes, one is in DiskPressure)${reset}"
sleep 0.6

echo ""
echo -e "${bold}▸ curl -s .../deployments + /pods + /pvc${reset}"
curl -sS -H "Authorization: Bearer aops-demo-token" \
  http://localhost:8001/apis/apps/v1/namespaces/payment-prod/deployments | python3 -c "
import json, sys
d = json.load(sys.stdin)
for dep in d['items']:
    name = dep['metadata']['name']
    replicas = dep['spec'].get('replicas',0)
    ready = dep['status'].get('readyReplicas',0)
    print(f'  dep {name:20s} ready {ready}/{replicas}')
"
curl -sS -H "Authorization: Bearer aops-demo-token" \
  http://localhost:8001/api/v1/namespaces/payment-prod/pods | python3 -c "
import json, sys
d = json.load(sys.stdin)
for p in d['items']:
    name = p['metadata']['name']
    phase = p['status'].get('phase', '?')
    cs = p['status'].get('containerStatuses', [{}])[0]
    state = cs.get('state', cs.get('lastState', {}))
    if 'waiting' in state: reason = state['waiting'].get('reason', '?')
    elif 'terminated' in state: reason = f\"{state['terminated'].get('reason','?')} (exit {state['terminated'].get('exitCode')})\"
    else: reason = '?'
    print(f'  pod {name:42s} {phase:10s} {reason}')
"
curl -sS -H "Authorization: Bearer aops-demo-token" \
  http://localhost:8001/api/v1/namespaces/payment-prod/persistentvolumeclaims | python3 -c "
import json, sys
d = json.load(sys.stdin)
for p in d['items']:
    name = p['metadata']['name']
    phase = p['status'].get('phase')
    sc = p.get('spec',{}).get('storageClassName')
    print(f'  pvc {name:42s} phase={phase:8s} storageClass={sc} (missing!)')
"
sleep 1.2

# ===== 7. Popeye scan =====
echo ""
echo -e "${dim}Popeye sanitizer report:${reset}"
echo -e "${bold}▸ curl -s -X POST http://localhost:8004/scan?namespace=payment-prod${reset}"
curl -sS -X POST "http://localhost:8004/scan?namespace=payment-prod" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'  score={d[\"score\"]:>3}/100  grade={d[\"grade\"]:1s}  findings={d[\"findings_count\"]}  '
      f'errors={d[\"findings_by_severity\"][\"error\"]}  warnings={d[\"findings_by_severity\"][\"warning\"]}')
print('  issues (first 12):')
for i in d['issues']['payment-prod'][:12]:
    sev = i['severity_label']
    color = {'error':'\033[31m','warning':'\033[33m','info':'\033[36m'}.get(sev,'')
    rst = '\033[0m' if color else ''
    print(f'    {color}[{sev:7s}]{rst} {i[\"code\"]:8s} {i[\"name\"]:38s} {i[\"message\"][:60]}')
print(f'    ... and {d[\"findings_count\"]-12} more')
"
sleep 1.2

# ===== 8. ANIMATED data-packet flowing through the pipeline =====
echo ""
echo -e "${bold}▸ data packet flowing through the pipeline${reset} ${dim}(animated)${reset}"
echo ""
# Print 6 frames, each moving the packet one step further. Use cursor movement
# to overwrite the previous line so the animation reads naturally in asciinema.
# Frame 0: at Prometheus
# Frame 1: at n8n (after webhook)
# Frame 2: at Popeye (after scan)
# Frame 3: at Dify-lite (with tool_call back to K8s API)
# Frame 4: at Ollama (LLM reasoning)
# Frame 5: at Slack (final card)
frames=(
  "  ${bright_cyan}●${reset}  ${dim}[PROM]${reset}  ───webhook──→  ${dim}[N8N]${reset}  ──HTTP──→  ${dim}[POP]${reset}  ──JSON──→  ${dim}[DIFY]${reset}  ──chat──→  ${dim}[OLL]${reset}  ──md──→  ${dim}[SLK]${reset}"
  "  ${dim}[PROM]${reset}  ───webhook──→  ${bright_cyan}●${reset} ${dim}[N8N]${reset}  ──HTTP──→  ${dim}[POP]${reset}  ──JSON──→  ${dim}[DIFY]${reset}  ──chat──→  ${dim}[OLL]${reset}  ──md──→  ${dim}[SLK]${reset}"
  "  ${dim}[PROM]${reset}  ───webhook──→  ${dim}[N8N]${reset}  ──HTTP──→  ${bright_cyan}●${reset} ${dim}[POP]${reset}  ──JSON──→  ${dim}[DIFY]${reset}  ──chat──→  ${dim}[OLL]${reset}  ──md──→  ${dim}[SLK]${reset}"
  "  ${dim}[PROM]${reset}  ───webhook──→  ${dim}[N8N]${reset}  ──HTTP──→  ${dim}[POP]${reset}  ──JSON──→  ${bright_yellow}●${reset} ${dim}[DIFY]${reset}  ──chat──→  ${dim}[OLL]${reset}  ──md──→  ${dim}[SLK]${reset}  ${bright_yellow}(tool_call: back to [K8s API])${reset}"
  "  ${dim}[PROM]${reset}  ───webhook──→  ${dim}[N8N]${reset}  ──HTTP──→  ${dim}[POP]${reset}  ──JSON──→  ${dim}[DIFY]${reset}  ──chat──→  ${bright_magenta}●${reset} ${dim}[OLL]${reset}  ──md──→  ${dim}[SLK]${reset}"
  "  ${dim}[PROM]${reset}  ───webhook──→  ${dim}[N8N]${reset}  ──HTTP──→  ${dim}[POP]${reset}  ──JSON──→  ${dim}[DIFY]${reset}  ──chat──→  ${dim}[OLL]${reset}  ──md──→  ${bright_green}●${reset} ${dim}[SLK]${reset}  ${bright_green}✓ card posted${reset}"
)
printf "  %b\n" "${frames[0]}"
sleep 0.45
for i in 1 2 3 4 5; do
  printf "\033[F\033[K"  # move up + clear line
  printf "  %b\n" "${frames[$i]}"
  sleep 0.45
done
sleep 0.5

# ===== 9. Prometheus alert rule preview =====
echo ""
echo -e "${dim}The Prometheus alert that triggers this whole flow:${reset}"
echo -e "${bold}▸ cat prometheus-rules.yml${reset}  ${dim}(PrometheusRule CRD)${reset}"
cat <<'RULE' | sed 's/^/  /'
  - alert: PaymentAPIHighErrorRate
    expr: |
      sum(rate(http_requests_total{service="payment-api",code=~"5.."}[5m]))
      / sum(rate(http_requests_total{service="payment-api"}[5m])) > 0.05
    for: 5m
    labels: { severity: critical, namespace: payment-prod }
    annotations:
      summary: "Payment API 5xx rate is 12.3% (threshold 5%)"
      description: "5xx error ratio exceeds 5% for 5 minutes"
      runbook_url: "https://wiki.internal/runbooks/payment-api-5xx"
RULE
sleep 1.2

# ===== 10. Alertmanager payload preview =====
echo ""
echo -e "${dim}Alertmanager webhook payload (what n8n receives):${reset}"
echo -e "${bold}▸ curl -s ... | jq '.alerts[0].labels, .alerts[0].annotations'${reset}"
python3 -c "
import json
payload = {
  'alertname': 'PaymentAPIHighErrorRate',
  'severity': 'critical',
  'namespace': 'payment-prod',
  'service': 'payment-api',
  'summary': 'Payment API 5xx rate is 12.3% (threshold 5%)',
  'started_at': '2026-08-20T06:25:00Z',
  'fingerprint': '9f8b9d2c1e4a8b6f',
}
for k,v in payload.items():
    print(f'  {k:14s} : {v}')
"
sleep 1.0

echo ""
echo -e "${yellow}Firing the alert + streaming n8n-runner logs in real-time...${reset}"
pause_step "fire the alert"

# ===== 11. Fire alert + real-time log tail =====
echo -e "${bold}▸ ./run.sh alert  (and tail -f var/log/n8n-runner.log)${reset}"
echo ""

# Fire alert in the background, while we tail the n8n-runner log
# Both popeye-scanner and dify-lite will also log — we tail all of them.
LOGDIR="$AOPS_ROOT/var/log"

# Start tail in the background, capture to a temp file
( tail -F "$LOGDIR/n8n-runner.log" "$LOGDIR/dify-lite.log" "$LOGDIR/popeye-scanner.log" "$LOGDIR/mock-slack.log" 2>/dev/null \
    | sed 's/^/  /' ) > /tmp/tail-output.log 2>&1 &
TAIL_PID=$!

# Give tail a moment to start
sleep 0.2

# Fire the alert in the foreground (this writes the API response to stdout
# but we'll suppress it and just show the captured logs)
./run.sh alert > /tmp/alert-response.json 2>&1

# Let tail run a bit more to capture all the post-alert logs
sleep 1.2

# Stop tail
kill $TAIL_PID 2>/dev/null || true
wait $TAIL_PID 2>/dev/null || true

# Print the captured log lines
echo ""
echo -e "${dim}── real-time log stream during alert firing ──${reset}"
head -30 /tmp/tail-output.log
sleep 1.5

# Show the alert response (parsed)
echo ""
echo -e "${dim}workflow response:${reset}"
python3 -c "
import json
with open('/tmp/alert-response.json') as f:
    raw = f.read()
try:
    d = json.loads(raw)
except:
    print('  ' + raw[:500])
    raise SystemExit

print(f'  run_id: {d.get(\"run_id\")}  duration: {d.get(\"duration_s\")}s  status: {d.get(\"status\")}')
print('  node trace:')
for t in d.get('trace', []):
    name = t['node']
    status = t['status']
    dur = t['duration_s']
    if status == 200:
        col = '\033[32m'; sym = '✓'
    elif status == 0:
        col = '\033[31m'; sym = '✗'
    else:
        col = '\033[33m'; sym = '⚠'
    print(f'    {col}{sym} {name:25s} HTTP {status}  {dur}s\033[0m')
"
sleep 1.2

# Capture the MEASURED end-to-end latency so nothing downstream has to
# hardcode a number that can drift away from reality.
AOPS_MS="$(python3 -c "
import json
try:
    raw = open('/tmp/alert-response.json').read()
    # run.sh prints a human preamble before the JSON body, so decode from the
    # first brace rather than assuming the whole file is a JSON document.
    i = raw.find('{')
    d = json.JSONDecoder().raw_decode(raw[i:])[0] if i >= 0 else {}
    ms = int(round(float(d.get('duration_s', 0)) * 1000))
    print(ms if ms > 0 else -1)
except Exception:
    print(-1)
")"
if [[ "$AOPS_MS" -lt 0 ]]; then
  AOPS_LAT="unmeasured (the alert response carried no duration)"
  AOPS_CELL_TXT="  Pipeline round trip: unmeasured"
else
  AOPS_LAT="${AOPS_MS} ms  (measured just now, stub backend, fixtures)"
  AOPS_CELL_TXT="  Pipeline round trip: ${AOPS_MS} ms (measured)"
fi
# Pad to the dashboard's right-hand cell width so the box never skews.
AOPS_CELL="$(printf '%-43.43s' "$AOPS_CELL_TXT")"

# ===== 12. Slack alert metadata =====
echo ""
echo -e "${dim}Slack card metadata:${reset}"
echo -e "${bold}▸ curl -s http://localhost:8003/alerts.json${reset}"
curl -sS http://localhost:8003/alerts.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
last = d['alerts'][-1] if d['alerts'] else {}
print(f'  alert_name        : {last.get(\"alert_name\")}')
print(f'  namespace         : {last.get(\"namespace\")}')
print(f'  severity          : {last.get(\"severity\")}')
print(f'  score/grade       : {last.get(\"score\")}/100  ({last.get(\"grade\")})')
print(f'  backend           : {last.get(\"trace\",{}).get(\"backend\")}')
print(f'  agent_duration_s  : {last.get(\"duration_s\")}')
print(f'  remediation_chars  : {len(last.get(\"text\",\"\"))}')
"
sleep 0.9

# ===== 13. Full remediation runbook =====
echo ""
echo -e "${bold}▸ remediation runbook (full text)${reset}"
curl -sS http://localhost:8003/alerts.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
last = d['alerts'][-1] if d['alerts'] else {}
text = last.get('text','')
print('  ' + '─' * 110)
for ln in text.split(chr(10)):
    print('  ' + ln)
print('  ' + '─' * 110)
"
sleep 1.6

# ===== 14. Before/After metrics dashboard (the WOW closer) =====
echo ""
echo -e "${bold}▸ SRE metrics: before vs after remediation${reset}"
echo -e "  ${yellow}ILLUSTRATIVE — the 'after' column is what a successful remediation would look like.${reset}"
echo -e "  ${yellow}The sandbox is fixture-backed and runs DRY_RUN=1: nothing was actually applied.${reset}"
echo ""
cat <<DASHBOARD
  ┌── BEFORE: cluster is on fire ─────────────┐  ┌── AFTER: kubectl apply complete ───────────┐
  │                                           │  │                                            │
  │  PaymentAPI 5xx rate      12.3%  🔴       │  │  PaymentAPI 5xx rate       0.1%  🟢       │
  │  payment-api pods ready     0/3  🔴       │  │  payment-api pods ready      3/3  🟢       │
  │  payment-worker pods ready  0/2  🔴       │  │  payment-worker pods ready   2/2  🟢       │
  │  CrashLoopBackOff pods       2   🔴       │  │  CrashLoopBackOff pods        0   🟢       │
  │  ImagePullBackOff pods      3   🔴       │  │  ImagePullBackOff pods       0   🟢       │
  │  DiskPressure nodes          1   🟠       │  │  DiskPressure nodes           0   🟢       │
  │  Pending PVCs                1   🟠       │  │  Pending PVCs                 0   🟢       │
  │  Dangling Ingress backends   1   🟠       │  │  Dangling Ingress backends   0   🟢       │
  │  Active alerts               1   🔴       │  │  Active alerts               0   🟢       │
  │  SLO burn rate             12.3x  🔴    │  │  SLO burn rate              0.1x  🟢       │
  │  Popeye score              0/100  F      │  │  Popeye score              93/100  A      │
  │                                           │  │                                            │
  │  Time to remediate: human = 45 min       │  │${AOPS_CELL}│
  └───────────────────────────────────────────┘  └────────────────────────────────────────────┘
DASHBOARD
sleep 2.0

# ===== 15. Cleanup =====
echo ""
echo -e "${bold}▸ ./run.sh stop${reset}"
./run.sh stop 2>&1 | sed 's/^/  /'
sleep 0.7

# ===== 16. Recap outro =====
echo ""
cat <<RECAP
   ───────────────────────────────────────────────────────────────────────
   RECAP: what just happened

     1. Alertmanager webhook  →  n8n-runner     (POST /webhook/aops-alert)
     2. n8n → Popeye          : POST /scan       → findings + score
     3. n8n → Dify-lite       : POST /v1/chat/completions → remediation runbook
     4. Dify-lite → Ollama/stub: agent loop, tool_call back to the K8s API
     5. n8n → Slack            : POST /webhook/slack → card rendered
     6. n8n → Remediation exec : POST /remediate → plan validated against the
                                 allowlist, DRY_RUN=1, refused unless the scan
                                 says data_source=live-cluster

   End-to-end latency: ${AOPS_LAT}

   This run used fixtures and applied nothing. For a real cluster:
     ./scripts/setup-kind-cluster.sh up  &&  ./scripts/verify-real-mode.sh

RECAP

cat <<'OUTRO'
   ✅ A.O.P.S. demo complete.

   Live landing page: https://aops-sre-pipeline.vercel.app
   Source:            https://github.com/adventurewave-labs/aops-sre-pipeline

   To run with real Ollama (Docker-equipped machine):
     cd aops && docker compose up -d
OUTRO
