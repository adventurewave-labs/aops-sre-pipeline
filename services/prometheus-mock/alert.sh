#!/usr/bin/env bash
# Mock Alertmanager webhook — fires the PaymentAPIHighErrorRate alert payload
# at the n8n-runner webhook endpoint. This is what Prometheus would have sent
# if its `PaymentAPIHighErrorRate` alert (5xx > 5% for 5m) had fired.
#
# Usage: ./alert.sh [URL]
# Default URL: http://localhost:5678/webhook/aops-alert

set -euo pipefail

URL="${1:-${AOPS_N8N_URL:-http://localhost:5678/webhook/aops-alert}}"

PAYLOAD=$(cat <<'JSON'
{
  "receiver": "aops-n8n",
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "PaymentAPIHighErrorRate",
        "severity": "critical",
        "namespace": "payment-prod",
        "service": "payment-api",
        "slo": "99.9",
        "cluster": "prod-us-east-1"
      },
      "annotations": {
        "summary": "Payment API 5xx rate is 12.3% (threshold 5%)",
        "description": "PaymentAPIHighErrorRate has been firing for 5m07s. "
                       "prometheus_http_requests_total{code=~\"5..\"} / total "
                       "for the payment-api scrape target is 0.123.",
        "runbook_url": "https://wiki.internal/runbooks/payment-api-5xx",
        "dashboard": "https://grafana.internal/d/payment-api"
      },
      "startsAt": "2026-08-20T06:25:00Z",
      "endsAt": "0001-01-01T00:00:00Z",
      "fingerprint": "9f8b9d2c1e4a8b6f",
      "silencedBy": [],
      "inhibitedBy": []
    }
  ],
  "groupLabels": {
    "alertname": "PaymentAPIHighErrorRate",
    "namespace": "payment-prod"
  },
  "commonLabels": {
    "alertname": "PaymentAPIHighErrorRate",
    "severity": "critical"
  },
  "commonAnnotations": {
    "summary": "Payment API 5xx rate is 12.3% (threshold 5%)"
  },
  "externalURL": "https://alertmanager.internal",
  "version": "4",
  "groupKey": "{}:{}:{alertname=\"PaymentAPIHighErrorRate\"}",
  "truncatedAlerts": 0
}
JSON
)

echo "Firing PaymentAPIHighErrorRate alert at $URL..."
curl -sS -X POST "$URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
    print(json.dumps({
        'status': r.get('status'),
        'run_id': r.get('run_id'),
        'duration_s': r.get('duration_s'),
        'nodes_executed': list((r.get('node_outputs') or {}).keys()),
        'trace': r.get('trace'),
    }, indent=2))
except Exception as e:
    print('response (not JSON):', sys.stdin.read() if False else '<binary>', file=sys.stderr)
    raise
"
