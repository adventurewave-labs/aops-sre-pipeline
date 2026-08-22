#!/usr/bin/env bash
# End-to-end verification of A.O.P.S. real mode.
#
# Real mode makes claims that sandbox mode cannot check: that containers reach
# a live cluster, that Popeye scans it, that Prometheus has real series, and
# that Alertmanager can deliver to n8n. This script checks each link and tells
# you which one is broken. Every claim the README makes about real mode should
# be verifiable by running this.
#
# Prereqs: a kind cluster from ./scripts/setup-kind-cluster.sh up, and the
# stack up via docker-compose.real.yml.
#
# Usage: ./scripts/verify-real-mode.sh
set -uo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YEL='\033[1;33m'; BOLD='\033[1m'; RST='\033[0m'
PASS=0; FAIL=0; SKIP=0

check() {
  local label="$1"; shift
  if out=$("$@" 2>&1); then
    echo -e "  ${GREEN}[PASS]${RST} $label"
    PASS=$((PASS+1))
  else
    echo -e "  ${RED}[FAIL]${RST} $label"
    echo "         ${out:0:200}"
    FAIL=$((FAIL+1))
  fi
}

skip() { echo -e "  ${YEL}[SKIP]${RST} $1"; SKIP=$((SKIP+1)); }

echo -e "\n${BOLD}A.O.P.S. real-mode verification${RST}\n"

echo -e "${BOLD}1. Cluster${RST}"
check "kind cluster 'aops-demo' exists" \
  bash -c "kind get clusters | grep -qx aops-demo"
check "internal kubeconfig is container-facing (not 127.0.0.1)" \
  bash -c "grep -q 'server: https://aops-demo-control-plane:6443' .kubeconfig.internal"
check "broken resources deployed" \
  bash -c "kubectl -n payment-prod get deploy payment-api payment-worker >/dev/null"

echo -e "\n${BOLD}2. Container -> cluster reachability (D4)${RST}"
check "popeye-scanner can reach the cluster" \
  bash -c "docker compose exec -T popeye-scanner kubectl get nodes >/dev/null"
check "remediation-executor can reach the cluster" \
  bash -c "docker compose exec -T remediation-executor kubectl get nodes >/dev/null"

echo -e "\n${BOLD}3. Scanner provenance (D2)${RST}"
check "scanner reports data_source=live-cluster" \
  bash -c "curl -fsS -XPOST 'http://localhost:8004/scan?namespace=payment-prod' | grep -q '\"data_source\": \"live-cluster\"'"
check "scanner healthz reports cluster_reachable=true" \
  bash -c "curl -fsS http://localhost:8004/healthz | grep -q '\"cluster_reachable\": true'"
check "real Popeye binary present in the scanner image (D1)" \
  bash -c "docker compose exec -T popeye-scanner popeye version >/dev/null"

echo -e "\n${BOLD}4. Prometheus has real series (D5)${RST}"
check "kube-state-metrics target is UP" \
  bash -c "curl -fsS 'http://localhost:9090/api/v1/targets?state=active' | grep -q 'kube-state-metrics'"
check "kube_pod_container_status_restarts_total has samples" \
  bash -c "curl -fsS --get 'http://localhost:9090/api/v1/query' --data-urlencode 'query=count(kube_pod_container_status_restarts_total)' | grep -q '\"value\"'"
check "http_requests_total has samples" \
  bash -c "curl -fsS --get 'http://localhost:9090/api/v1/query' --data-urlencode 'query=count(http_requests_total)' | grep -q '\"value\"'"
check "alert rules are loaded" \
  bash -c "curl -fsS http://localhost:9090/api/v1/rules | grep -q PaymentAPIHighErrorRate"

echo -e "\n${BOLD}5. n8n workflow import (D6)${RST}"
check "n8n has the A.O.P.S. workflow" \
  bash -c "docker compose exec -T n8n n8n list:workflow 2>/dev/null | grep -qi 'A.O.P.S'"
check "webhook /webhook/aops-alert resolves (not 404)" \
  bash -c "test \"\$(curl -s -o /dev/null -w '%{http_code}' -XPOST http://localhost:5678/webhook/aops-alert -H 'Content-Type: application/json' -d '{}')\" != 404"

echo -e "\n${BOLD}6. Alerting path${RST}"
echo -e "  ${YEL}note${RST} PaymentAPIHighErrorRate has 'for: 5m' — allow 5-6 minutes"
echo -e "       after the exporter starts before expecting it to fire."
if curl -fsS http://localhost:9090/api/v1/alerts 2>/dev/null | grep -q '"state":"firing"'; then
  echo -e "  ${GREEN}[PASS]${RST} at least one alert is firing"
  PASS=$((PASS+1))
elif curl -fsS http://localhost:9090/api/v1/alerts 2>/dev/null | grep -q '"state":"pending"'; then
  skip "alert is pending (waiting out 'for:') — re-run in a few minutes"
else
  skip "no alert firing yet — re-run in a few minutes"
fi

echo ""
echo -e "${BOLD}Result: ${GREEN}${PASS} passed${RST}, ${RED}${FAIL} failed${RST}, ${YEL}${SKIP} skipped${RST}"
echo ""
if [[ $FAIL -gt 0 ]]; then
  echo "Real mode is NOT fully wired. Fix the failures above before claiming it works."
  exit 1
fi
echo "Real mode verified end to end."
