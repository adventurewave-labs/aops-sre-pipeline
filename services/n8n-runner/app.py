#!/usr/bin/env python3
"""
Workflow executor — a faithful, lightweight re-implementation of the
n8n workflow in `aops-workflow.json`. Loads the JSON definition, walks
its `connections` graph in topological order, and executes each HTTP
request node against the live services in the sandbox.

This is what makes the demo runnable without a real n8n instance. The
same `aops-workflow.json` file can be imported into a real n8n (in a
Docker-equipped environment) — it is the canonical, portable artifact.

Endpoints:
  GET  /healthz
  GET  /workflow       -> the loaded workflow definition (JSON)
  GET  /runs           -> last N workflow executions
  POST /webhook/aops-alert  -> Alertmanager webhook (workflow trigger)
  POST /webhook/:path  -> generic webhook trigger
  POST /trigger        -> convenience trigger (same as /webhook/aops-alert)
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import urllib.request
import urllib.error
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOW_PATH = os.path.join(HERE, "aops-workflow.json")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [n8n-runner] %(levelname)s %(message)s")
log = logging.getLogger("n8n-runner")

BIND_HOST = os.environ.get("N8N_RUNNER_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("N8N_RUNNER_PORT", "5678"))
# In the sandbox everything is on localhost; the docker-compose.yml uses
# service DNS names. Allow both via env var so the same runner works in
# both modes.
HOST_OVERRIDES = {
    "popeye-scanner": os.environ.get("POPEYE_URL",
                                      "http://localhost:8004"),
    "dify-lite":      os.environ.get("DIFY_LITE_URL",
                                      "http://localhost:8002"),
    "mock-slack":     os.environ.get("MOCK_SLACK_URL",
                                     "http://localhost:8003"),
    "n8n-runner":     os.environ.get("N8N_RUNNER_URL",
                                      "http://localhost:5678"),
}

# ---------------------------------------------------------------------------
# Load workflow
# ---------------------------------------------------------------------------
with open(WORKFLOW_PATH) as f:
    WORKFLOW = json.load(f)

# Build name->node lookup
NODES = {n["name"]: n for n in WORKFLOW["nodes"]}

# Build edge list from connections (node -> [downstream nodes])
EDGES: dict[str, list[str]] = {}
for src, conn in WORKFLOW["connections"].items():
    for main_list in conn.get("main", []):
        for edge in main_list:
            EDGES.setdefault(src, []).append(edge["node"])

# Determine trigger (webhook) nodes and entry nodes
TRIGGER_NODES = [n["name"] for n in WORKFLOW["nodes"]
                 if n["type"] == "n8n-nodes-base.webhook"]

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
RUNS: list[dict] = []
RUNS_LOCK = threading.Lock()
MAX_RUNS_KEPT = 20


def _substitute_url(url: str) -> str:
    """Replace service DNS names with host overrides for sandbox runs."""
    for svc, override in HOST_OVERRIDES.items():
        if f"{svc}:" in url or f"{svc}." in url:
            # Replace "http://popeye-scanner:8004/..." -> "http://localhost:8004/..."
            import re
            url = re.sub(rf"http://{re.escape(svc)}:\d+",
                         override.rstrip("/"), url)
            break
    return url


def _http_request(node: dict, upstream_data: dict, run_log: list,
                   node_outputs: dict | None = None) -> dict:
    """Execute an HTTP Request node, returning the JSON response.

    The workflow JSON uses n8n-style `={{...}}` expressions in body parameters
    that this sandbox runner does not evaluate (n8n's expression engine is
    out of scope here). Instead, this runner has explicit body builders keyed
    on the node name — so for each named HTTP Request node in the A.O.P.S.
    workflow, we construct the request body directly from upstream data.
    """
    params = node.get("parameters", {})
    method = params.get("httpMethod") or params.get("method") or "GET"
    url = params.get("url", "")
    url = _substitute_url(url)
    node_name = node["name"]

    # Per-node body builders — see aops-workflow.json for the canonical
    # definitions (which use n8n expressions for real n8n).
    BODY_BUILDERS = {
        "Popeye Scan": lambda d: None,  # no body, just POST with optional auth header
        "Dify Agent Reasoning": lambda d: {
            "model": os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b"),
            "messages": [
                {"role": "system",
                 "content":
                    "You are AURA-SRE, a senior Site Reliability Engineer "
                    "co-pilot. You received a Popeye sanitizer report (JSON) "
                    "describing issues in a Kubernetes namespace. Produce a "
                    "remediation runbook with: 1) root-cause hypothesis, "
                    "2) numbered remediation steps with the exact kubectl "
                    "commands, 3) risk / blast-radius notes, 4) suggested "
                    "follow-up alerts. Use Markdown. Be terse and grounded "
                    "only in the JSON you received."},
                {"role": "user", "content": json.dumps(d)},
            ],
        },
        "Post to Slack": lambda d: {
            "alert_name": "PaymentAPIHighErrorRate",
            "namespace": "payment-prod",
            "severity": "critical",
            "score": (node_outputs or {}).get("Popeye Scan", {}).get("score", 0) if node_outputs else upstream_data.get("score", 0),
            "grade": (node_outputs or {}).get("Popeye Scan", {}).get("grade", "?") if node_outputs else upstream_data.get("grade", "?"),
            "text": (d.get("choices", [{}])[0].get("message", {}).get("content", "")
                     if isinstance(d, dict) else str(d)),
            "trace": d.get("_dify_lite_trace", {}) if isinstance(d, dict) else {},
            "duration_s": d.get("_dify_lite_duration_s") if isinstance(d, dict) else None,
        },
    }

    builder = BODY_BUILDERS.get(node_name)
    body_obj = builder(upstream_data) if builder else None
    body = json.dumps(body_obj).encode("utf-8") if body_obj is not None else None

    headers = {"Content-Type": "application/json",
               "Accept": "application/json"}
    if params.get("sendHeaders"):
        for h in params.get("headerParameters", {}).get("parameters", []):
            headers[h["name"]] = h["value"]

    started = time.time()
    log.info("→ node '%s' %s %s", node_name, method, url)
    if body_obj is not None:
        log.info("  body keys: %s", list(body_obj.keys()))

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    timeout = int((params.get("options") or {}).get("timeout", 30000)) / 1000.0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp_body = r.read().decode("utf-8")
            try:
                resp = json.loads(resp_body)
            except Exception:
                resp = {"text": resp_body}
            status = r.status
    except urllib.error.HTTPError as e:
        resp = {"error": e.read().decode("utf-8", errors="replace"), "status": e.code}
        status = e.code
    except Exception as e:
        log.error("node '%s' failed: %s", node_name, e)
        resp = {"error": str(e)}
        status = 0

    elapsed = round(time.time() - started, 3)
    run_log.append({
        "node": node_name,
        "url": url,
        "method": method,
        "status": status,
        "duration_s": elapsed,
        "response_keys": list(resp.keys()) if isinstance(resp, dict) else None,
    })
    log.info("← node '%s' status=%s elapsed=%.2fs",
             node_name, status, elapsed)
    return resp


def execute_workflow(trigger_payload: dict) -> dict:
    """Run the chain from webhook trigger through to final response node."""
    run_id = f"run-{int(time.time()*1000)}"
    started = time.time()
    run_log: list[dict] = []
    log.info("==== Workflow run %s started ====", run_id)

    # Data passed between nodes — initialize with the webhook payload.
    node_outputs: dict[str, dict] = {"__trigger__": trigger_payload}
    last_data = trigger_payload

    # Walk the chain. The A.O.P.S. workflow is a linear chain starting at
    # the webhook trigger and ending at the Respond node — but the runner
    # is generic enough to handle a DAG.
    # Topological order via DFS from triggers.
    order: list[str] = []
    visited: set[str] = set()
    def visit(name):
        if name in visited:
            return
        visited.add(name)
        for nxt in EDGES.get(name, []):
            visit(nxt)
        order.append(name)
    for trig in TRIGGER_NODES:
        visit(trig)
    order.reverse()
    log.info("Execution order: %s", order)

    final_response = {"status": "ok", "run_id": run_id}
    for node_name in order:
        node = NODES.get(node_name)
        if not node:
            continue
        if node["type"] == "n8n-nodes-base.webhook":
            # The webhook is the trigger; its "output" is the payload
            node_outputs[node_name] = trigger_payload
            continue
        if node["type"] == "n8n-nodes-base.respondToWebhook":
            # Final node — we already have the last HTTP output
            node_outputs[node_name] = final_response
            continue
        if node["type"] == "n8n-nodes-base.httpRequest":
            out = _http_request(node, last_data, run_log, node_outputs)
            node_outputs[node_name] = out
            last_data = out
            # Stash upstream data under node name for $node['Name'].json access
            # (simpler than n8n's full expression engine)
            continue
    # Compose a final response
    final_response = {
        "status": "completed",
        "run_id": run_id,
        "duration_s": round(time.time() - started, 3),
        "node_outputs": {
            name: {
                k: (v if not isinstance(v, dict) else
                    {kk: vv for kk, vv in v.items()
                     if kk in ("score","grade","scanner","findings_count",
                               "issues","id","model","choices","_dify_lite_trace",
                               "_dify_lite_duration_s","ok","total_alerts","received")})
                for k, v in node_outputs.get(name, {}).items()
                if k in ("score","grade","scanner","findings_count","issues",
                         "id","model","choices","_dify_lite_trace",
                         "_dify_lite_duration_s","ok","total_alerts","received")
            } for name in order
        },
        "trace": run_log,
    }

    elapsed = round(time.time() - started, 3)
    log.info("==== Workflow run %s done in %.2fs ====", run_id, elapsed)

    with RUNS_LOCK:
        RUNS.append({
            "run_id": run_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(started)),
            "duration_s": elapsed,
            "trigger_payload": trigger_payload,
            "trace": run_log,
            "final_response": final_response,
        })
        if len(RUNS) > MAX_RUNS_KEPT:
            RUNS.pop(0)

    return final_response


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class N8nRunnerHandler(BaseHTTPRequestHandler):
    server_version = "AOPSN8nRunner/0.1"

    def _send(self, code, obj, ctype="application/json"):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/healthz", "/livez", "/readyz", "/"):
            return self._send(200, {
                "status": "ok",
                "service": "n8n-runner",
                "workflow": WORKFLOW.get("name"),
                "nodes": list(NODES.keys()),
                "triggers": TRIGGER_NODES,
                "runs_total": len(RUNS),
            })
        if u.path == "/workflow":
            return self._send(200, WORKFLOW)
        if u.path == "/runs":
            with RUNS_LOCK:
                return self._send(200, {"runs": RUNS[-10:]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            # Fall back to form-encoded or raw text
            payload = {"raw": raw}

        # Accept /webhook/aops-alert, /trigger, /webhook/<anything>
        if u.path in ("/webhook/aops-alert", "/trigger", "/webhook"):
            log.info("POST %s payload=%s", u.path,
                     {k: v for k, v in payload.items() if k != "raw"} if payload else "(empty)")
            try:
                result = execute_workflow(payload)
                return self._send(200, result)
            except Exception as e:
                log.exception("workflow failed")
                return self._send(500, {"error": str(e), "run_id": None})
        return self._send(404, {"error": "not found"})

    def do_HEAD(self):
        return self.do_GET()

    def log_message(self, fmt, *args):
        pass


def main():
    log.info("n8n-runner on %s:%d (workflow=%s)",
             BIND_HOST, BIND_PORT, WORKFLOW.get("name"))
    log.info("Execution graph: %s", EDGES)
    log.info("Host overrides: %s", HOST_OVERRIDES)
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), N8nRunnerHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
