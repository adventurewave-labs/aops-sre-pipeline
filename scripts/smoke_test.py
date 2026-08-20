#!/usr/bin/env python3
"""Component-level smoke test for the A.O.P.S. stack.

Hits each service in isolation, prints a compact summary, exits non-zero if
anything fails. Used both interactively (sanity) and by the UAT runner.
"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = {
    "k8s":      "http://localhost:8001",
    "popeye":   "http://localhost:8004",
    "dify":     "http://localhost:8002",
    "slack":    "http://localhost:8003",
    "n8n":      "http://localhost:5678",
}


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


def post(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8") if isinstance(payload, (dict, list)) else payload.encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.startswith(("{", "[")) else body)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def line(s, c="="):
    print(s)


# 1. mock-k8s-api
print("\n=== 1. mock-k8s-api ===")
st, nodes = get(BASE["k8s"] + "/api/v1/nodes",
                 headers={"Authorization": "Bearer aops-demo-token"})
print(f"  HTTP {st}")
print(f"  nodes: {[n['metadata']['name'] for n in nodes['items']]}")
disk_pressure = [n for n in nodes['items']
                  if any(c.get('type') == 'DiskPressure' and c.get('status') == 'True'
                          for c in n['status'].get('conditions', []))]
print(f"  DiskPressure nodes: {[n['metadata']['name'] for n in disk_pressure]}")

st, deps = get(BASE["k8s"] + "/apis/apps/v1/namespaces/payment-prod/deployments",
                headers={"Authorization": "Bearer aops-demo-token"})
print(f"  deployments: {[(d['metadata']['name'], d['status'].get('readyReplicas',0)) for d in deps['items']]}")

st, ings = get(BASE["k8s"] + "/apis/networking.k8s.io/v1/namespaces/payment-prod/ingresses",
                headers={"Authorization": "Bearer aops-demo-token"})
print(f"  ingresses: {[i['metadata']['name'] for i in ings['items']]}")
print(f"  ingress backend(s): {[i['spec']['rules'][0]['http']['paths'][0]['backend']['service']['name'] for i in ings['items']]}")

st, svcs = get(BASE["k8s"] + "/api/v1/namespaces/payment-prod/services",
                headers={"Authorization": "Bearer aops-demo-token"})
print(f"  services: {[s['metadata']['name'] for s in svcs['items']]} (frontend intentionally absent)")

st, pvcs = get(BASE["k8s"] + "/api/v1/namespaces/payment-prod/persistentvolumeclaims",
                headers={"Authorization": "Bearer aops-demo-token"})
print(f"  PVCs: {[(p['metadata']['name'], p['status'].get('phase')) for p in pvcs['items']]}")

# 2. popeye scanner
print("\n=== 2. popeye-scanner ===")
st, scan = post(BASE["popeye"] + "/scan?namespace=payment-prod", {})
print(f"  HTTP {st}")
print(f"  score={scan['score']}  grade={scan['grade']}  findings={scan['findings_count']}")
print(f"  by_severity={scan['findings_by_severity']}")
print(f"  resources_scanned={scan['resources_scanned']}")
print(f"  first 8 issues:")
for i in scan["issues"]["payment-prod"][:8]:
    print(f"    [{i['severity_label']:7s}] {i['code']:8s} {i['name']:38s} {i['message'][:80]}")

# 3. dify-lite
print("\n=== 3. dify-lite ===")
st, health = get(BASE["dify"] + "/healthz")
print(f"  HTTP {st}")
print(f"  backend_active: {health['backend_active']}")
print(f"  ollama_reachable: {health['ollama_reachable']}")
print(f"  model: {health['ollama_model']}")

print("\n  POSTing Popeye JSON to /v1/chat/completions ...")
t0 = time.time()
payload = {
    "model": health["ollama_model"],
    "messages": [
        {"role": "system", "content": "You are AURA-SRE."},
        {"role": "user", "content": json.dumps(scan)},
    ],
}
st, completion = post(BASE["dify"] + "/v1/chat/completions", payload)
elapsed = time.time() - t0
print(f"  HTTP {st}  elapsed={elapsed:.2f}s  agent_duration={completion.get('_dify_lite_duration_s')}s")
trace = completion.get("_dify_lite_trace", {})
print(f"  backend_used: {trace.get('backend')}")
print(f"  rounds: {len(trace.get('rounds', []))}  tool_calls: {len(trace.get('tool_calls', []))}")
text = completion["choices"][0]["message"]["content"]
print(f"  remediation_chars: {len(text)}")
print(f"  remediation preview (first 600 chars):")
print("    " + text[:600].replace("\n", "\n    "))

# 4. mock-slack
print("\n=== 4. mock-slack ===")
st, slack_health = get(BASE["slack"] + "/healthz")
print(f"  HTTP {st}  alerts_received: {slack_health['alerts_received']}")

# 5. n8n-runner
print("\n=== 5. n8n-runner ===")
st, n8n_health = get(BASE["n8n"] + "/healthz")
print(f"  HTTP {st}")
print(f"  nodes: {n8n_health['nodes']}")
print(f"  triggers: {n8n_health['triggers']}")
print(f"  runs_total: {n8n_health['runs_total']}")

print("\n=== COMPONENT SMOKE TEST PASSED ===")
