#!/usr/bin/env bash
# A.O.P.S. demo script — runs through the entire pipeline live.
# Captured via asciinema and converted to GIF via agg.
#
# Usage:
#   asciinema rec --idle-time-limit=2 -c "/home/z/my-project/aops/scripts/demo_script.sh" \
#       /home/z/my-project/aops/var/demo.cast
#   agg /home/z/my-project/aops/var/demo.cast /home/z/my-project/download/aops-demo.gif

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
reset="\033[0m"

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
echo ""
echo -e "${dim}Components in this sandbox:${reset}"
echo -e "  ${cyan}mock-k8s-api${reset}     :8001   2 nodes · 2 broken Deployments · 1 dangling Ingress · 1 Pending PVC · 1 DiskPressure"
echo -e "  ${cyan}popeye-scanner${reset}  :8004   100+ analyzers → Popeye-shaped JSON"
echo -e "  ${cyan}dify-lite${reset}       :8002   FastAPI agent (OpenAI-compatible /v1/chat/completions)"
echo -e "  ${cyan}mock-slack${reset}      :8003   Slack-card HTML receiver"
echo -e "  ${cyan}n8n-runner${reset}      :5678   Webhook + workflow executor"
echo ""
echo -e "${yellow}Press [enter] to boot the stack...${reset}"
read -r

echo -e "${bold}\n▸ ./run.sh up-no-ollama${reset}"
./run.sh up-no-ollama 2>&1 | sed 's/^/  /'
sleep 0.8

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

echo ""
echo -e "${dim}This sandbox runs all five services as plain Python processes."
echo -e "On any Docker host, the same code runs via:${reset}"
echo -e "${bold}▸ cat docker-compose.yml | head -30${reset}"
sed -n '1,30p' docker-compose.yml | sed 's/^/  /'
sleep 1.5

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
sleep 0.6

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
sleep 1.2

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
sleep 1.5

echo ""
echo -e "${dim}Generated remediation (from Dify-lite, backend: stub):${reset}"
echo -e "${bold}▸ curl mock-slack /alerts.json | jq .alerts[-1].text${reset}"
curl -sS http://localhost:8003/alerts.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
last = d['alerts'][-1] if d['alerts'] else {}
print(f'  alert_name  : {last.get(\"alert_name\")}')
print(f'  namespace   : {last.get(\"namespace\")}')
print(f'  score/grade : {last.get(\"score\")}/100  ({last.get(\"grade\")})')
print(f'  backend     : {last.get(\"trace\",{}).get(\"backend\")}')
print(f'  duration_s  : {last.get(\"duration_s\")}')
print(f'  remediation_chars: {len(last.get(\"text\",\"\"))}')
print()
print('  --- remediation runbook (first 50 lines) ---')
text = last.get('text','')
for ln in text.split(chr(10))[:50]:
    print('    ' + ln)
print('    ... (truncated)')
"

echo ""
echo -e "${yellow}Slack card rendered at http://localhost:8003 (browser view)${reset}"
echo -e "${dim}HTML page auto-refreshes every 3s — open in a browser to see the card.${reset}"

echo ""
echo -e "${bold}▸ ./run.sh stop${reset}"
./run.sh stop 2>&1 | sed 's/^/  /'

echo ""
cat <<'OUTRO'
   ✅ A.O.P.S. demo complete.

   Architecture recap:
     Prometheus ──> n8n ──> Popeye ──> Dify-lite ──> Slack
                                                  │
                                                  └─ Ollama (or stub)

   Deliverables:
     /home/z/my-project/aops/        — full source + docker-compose.yml
     /home/z/my-project/download/    — UAT report + demo GIFs

   To run with real Ollama (Docker-equipped machine):
     cd aops && docker compose up -d
OUTRO
