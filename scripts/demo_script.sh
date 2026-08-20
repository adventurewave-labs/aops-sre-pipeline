#!/usr/bin/env bash
# A.O.P.S. demo script — runs through the entire pipeline live.
# Captured via asciinema and converted to GIF via agg.
#
# Target runtime: ~25 seconds (so the GIF is long enough to show every step).
#
# Usage:
#   asciinema rec --idle-time-limit=2 --cols=140 --rows=44 \
#       -c "/home/z/my-project/aops/scripts/demo_script.sh" \
#       /home/z/my-project/aops/var/demo.cast
#   agg --font-family "DejaVu Sans Mono" --font-size 13 --speed 0.85 \
#       --theme "monokai" /home/z/my-project/aops/var/demo.cast \
#       /home/z/my-project/aops/site/aops-demo.gif

set -uo pipefail
AOPS_ROOT="${AOPS_ROOT:-/home/z/my-project/aops}"
cd "$AOPS_ROOT"

bold="\033[1m"
dim="\033[2m"
red="\033[31m"
green="\033[32m"
yellow="\033[33m"
blue="\033[34m"
magenta="\033[35m"
cyan="\033[36m"
white="\033[97m"
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
echo -e "${bold}In this 25-second demo we will:${reset}"
echo -e "  ${cyan}1.${reset}  boot 5 services as plain Python processes"
echo -e "  ${cyan}2.${reset}  probe mock Kubernetes for 6 broken resources"
echo -e "  ${cyan}3.${reset}  run Popeye — emit 24 findings"
echo -e "  ${cyan}4.${reset}  fire the PaymentAPIHighErrorRate alert into n8n"
echo -e "  ${cyan}5.${reset}  watch the workflow chain fire end-to-end"
echo -e "  ${cyan}6.${reset}  read the remediation runbook posted to Slack"
sleep 0.8

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
echo -e "${yellow}Press [enter] to boot the stack...${reset}"
read -r

# ===== 2. Boot the stack =====
echo -e "${bold}\n▸ ./run.sh up-no-ollama${reset}"
./run.sh up-no-ollama 2>&1 | sed 's/^/  /'
sleep 1.0

# ===== 3. Health probes (with one detailed response) =====
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
sleep 0.7

echo ""
echo -e "${dim}dify-lite reports its backend selection — auto-detected Ollama is unreachable"
echo -e "  in this sandbox, so it fell back to the deterministic stub backend.${reset}"
echo -e "${bold}▸ curl -s http://localhost:8002/healthz${reset}"
curl -sS http://localhost:8002/healthz | python3 -m json.tool | sed 's/^/  /'
sleep 1.0

# ===== 4. docker-compose.yml preview =====
echo ""
echo -e "${dim}This sandbox runs all five services as plain Python processes."
echo -e "On any Docker host, the same code runs via:${reset}"
echo -e "${bold}▸ sed -n '1,30p' docker-compose.yml${reset}"
sed -n '1,30p' docker-compose.yml | sed 's/^/  /'
sleep 1.4

# ===== 5. Cluster state — nodes + deployments =====
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
sleep 0.6
echo -e "  ${dim}(2 nodes, one is in DiskPressure)${reset}"
sleep 0.7

echo ""
echo -e "${bold}▸ curl -s .../deployments${reset}"
curl -sS -H "Authorization: Bearer aops-demo-token" \
  http://localhost:8001/apis/apps/v1/namespaces/payment-prod/deployments | python3 -c "
import json, sys
d = json.load(sys.stdin)
for dep in d['items']:
    name = dep['metadata']['name']
    replicas = dep['spec'].get('replicas',0)
    ready = dep['status'].get('readyReplicas',0)
    print(f'  {name:20s} ready {ready}/{replicas}')
"
sleep 0.6
echo -e "  ${dim}(2 broken Deployments)${reset}"
sleep 0.8

# ===== 6. Pod + Ingress + PVC round-up =====
echo ""
echo -e "${bold}▸ pods / ingress / pvc summary${reset}"
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
echo -e "  ${dim}Ingress routes to 'payment-frontend' which is not in the Service list → dangling${reset}"
sleep 0.6
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
sleep 1.0

# ===== 7. Popeye sanitizer =====
echo ""
echo -e "${dim}Popeye sanitizer report:${reset}"
echo -e "${bold}▸ curl -s -X POST http://localhost:8004/scan?namespace=payment-prod${reset}"
curl -sS -X POST "http://localhost:8004/scan?namespace=payment-prod" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'  score={d[\"score\"]:>3}/100  grade={d[\"grade\"]:1s}  findings={d[\"findings_count\"]}  '
      f'errors={d[\"findings_by_severity\"][\"error\"]}  warnings={d[\"findings_by_severity\"][\"warning\"]}')
print('  issues:')
for i in d['issues']['payment-prod']:
    sev = i['severity_label']
    color = {'error':'\033[31m','warning':'\033[33m','info':'\033[36m'}.get(sev,'')
    rst = '\033[0m' if color else ''
    print(f'    {color}[{sev:7s}]{rst} {i[\"code\"]:8s} {i[\"name\"]:38s} {i[\"message\"][:70]}')
"
sleep 1.4

echo ""
echo -e "${dim}Popeye JSON shape (top-level keys):${reset}"
echo -e "${bold}▸ curl -s ... | python3 -c 'import json,sys; print(list(json.load(sys.stdin).keys()))'${reset}"
curl -sS -X POST "http://localhost:8004/scan?namespace=payment-prod" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for k in d.keys():
    v = d[k]
    if isinstance(v, dict):
        summary = f'dict({len(v)} keys)'
    elif isinstance(v, list):
        summary = f'list({len(v)})'
    else:
        summary = repr(v)
    print(f'  {k:20s} {summary}')
"
sleep 1.0

# ===== 8. n8n workflow nodes =====
echo ""
echo -e "${dim}The n8n workflow loaded by n8n-runner:${reset}"
echo -e "${bold}▸ curl -s http://localhost:5678/workflow | python3 -m json.tool | head -20${reset}"
curl -sS http://localhost:5678/workflow | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'  name: {d[\"name\"]}')
print(f'  nodes ({len(d[\"nodes\"])} total):')
for i, n in enumerate(d['nodes'], 1):
    typ = n['type'].replace('n8n-nodes-base.', '')
    print(f'    {i}. {n[\"name\"]:25s} ({typ})')
"
sleep 1.0

# ===== 9. Fire the alert =====
echo ""
echo -e "${yellow}Now firing the PaymentAPIHighErrorRate alert...${reset}"
echo -e "${yellow}Press [enter] to fire the alert into n8n-runner...${reset}"
read -r

echo -e "${bold}▸ ./run.sh alert${reset}"
./run.sh alert 2>&1 | python3 -c "
import json, sys, time
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception as e:
    print('  parse error:', e)
    print('  raw output (first 500 chars):')
    print('  ' + raw[:500])
    sys.exit(0)

print(f'  run_id: {d.get(\"run_id\")}  duration: {d.get(\"duration_s\")}s  status: {d.get(\"status\")}')
print()
print('  Node execution trace:')
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
sleep 1.6

# ===== 10. Final response payload =====
echo ""
echo -e "${dim}Full workflow response payload (top-level keys):${reset}"
echo -e "${bold}▸ curl -s http://localhost:5678/runs | tail -1${reset}"
curl -sS http://localhost:5678/runs | python3 -c "
import json, sys
d = json.load(sys.stdin)
run = d['runs'][-1] if d.get('runs') else {}
print(f'  run_id        : {run.get(\"run_id\")}')
print(f'  started_at    : {run.get(\"started_at\")}')
print(f'  duration_s    : {run.get(\"duration_s\")}')
print(f'  trigger keys  : {list(run.get(\"trigger_payload\",{}).keys())}')
print(f'  trace events  : {len(run.get(\"trace\",[]))}')
print(f'  final_status : {run.get(\"final_response\",{}).get(\"status\")}')
"
sleep 1.2

# ===== 11. Slack card metadata =====
echo ""
echo -e "${dim}Generated remediation (from Dify-lite, backend: stub):${reset}"
echo -e "${bold}▸ curl -s http://localhost:8003/alerts.json | python3 -m json.tool${reset}"
curl -sS http://localhost:8003/alerts.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
last = d['alerts'][-1] if d['alerts'] else {}
print(f'  alert_name       : {last.get(\"alert_name\")}')
print(f'  namespace        : {last.get(\"namespace\")}')
print(f'  severity         : {last.get(\"severity\")}')
print(f'  score/grade      : {last.get(\"score\")}/100  ({last.get(\"grade\")})')
print(f'  backend          : {last.get(\"trace\",{}).get(\"backend\")}')
print(f'  agent_duration_s : {last.get(\"duration_s\")}')
print(f'  remediation_chars : {len(last.get(\"text\",\"\"))}')
"
sleep 1.0

# ===== 12. Full remediation preview =====
echo ""
echo -e "${bold}▸ remediation runbook (full text)${reset}"
curl -sS http://localhost:8003/alerts.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
last = d['alerts'][-1] if d['alerts'] else {}
text = last.get('text','')
print('  ' + '─' * 100)
for ln in text.split(chr(10)):
    print('  ' + ln)
print('  ' + '─' * 100)
"
sleep 2.0

# ===== 13. Slack card visual + cleanup =====
echo ""
echo -e "${yellow}Slack card rendered at http://localhost:8003 (browser view)${reset}"
echo -e "${dim}HTML page auto-refreshes every 3s — open in a browser to see the card.${reset}"
sleep 1.0

echo ""
echo -e "${bold}▸ ./run.sh stop${reset}"
./run.sh stop 2>&1 | sed 's/^/  /'
sleep 0.8

# ===== 14. Recap outro =====
echo ""
cat <<'RECAP'
   ───────────────────────────────────────────────────────────────────────
   RECAP: what just happened

     1. Alertmanager webhook  →  n8n-runner  (POST /webhook/aops-alert)
     2. n8n → Popeye          : POST /scan → 24 findings, score 0/100 (F)
     3. n8n → Dify-lite       : POST /v1/chat/completions → 4.2 KB runbook
     4. Dify-lite → Ollama/stub : 2-round agent loop, tool_call to K8s API
     5. n8n → Slack           : POST /webhook/slack → card rendered

   End-to-end latency: ~23 ms (stub backend)
                       1.5–4 s with real Ollama + qwen2.5:0.5b

RECAP

cat <<'OUTRO'
   ✅ A.O.P.S. demo complete.

   Live landing page: https://aops-sre-pipeline.vercel.app
   Source:            https://github.com/adventurewave-labs/aops-sre-pipeline

   To run with real Ollama (Docker-equipped machine):
     cd aops && docker compose up -d
OUTRO
