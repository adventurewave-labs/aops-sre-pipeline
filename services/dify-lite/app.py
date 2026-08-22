#!/usr/bin/env python3
"""
Dify-lite — a lightweight agentic brain that mimics Dify.ai's chat-completions
+ tool-calling API surface for the A.O.P.S. demo.

Pipeline:
  n8n POSTs Popeye JSON + a system prompt to /v1/chat/completions
  -> Dify-lite runs a 2-round agentic loop:
        Round 1: ask LLM/stub "do you need any extra context from K8s?"
                 -> may emit a tool_call (HTTP request against mock K8s)
        Round 2: with the extra context, ask LLM/stub for the final remediation
  -> Returns OpenAI-compatible chat.completion JSON with the remediation text.

Backend selection (env AOPS_LLM_BACKEND):
  - ollama  : POST to $OLLAMA_URL/v1/chat/completions (default model qwen2.5:0.5b)
  - stub    : deterministic rule-based SRE remediator (always works, instant,
              no LLM dependency — useful for fast/UAT runs)
  - auto    : try ollama, fall back to stub if unreachable (default)
"""

from __future__ import annotations

import os
import sys
import json
import re
import time
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [dify-lite] %(levelname)s %(message)s")
log = logging.getLogger("dify-lite")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LLM_BACKEND = os.environ.get("AOPS_LLM_BACKEND", "ollama").lower()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b")
MOCK_K8S_URL = os.environ.get("MOCK_K8S_URL", "http://localhost:8001")
K8S_TOKEN = os.environ.get("MOCK_K8S_TOKEN", "aops-demo-token")
MAX_AGENT_ROUNDS = int(os.environ.get("AOPS_MAX_AGENT_ROUNDS", "2"))

SYSTEM_PROMPT = """You are AURA-SRE, a senior Site Reliability Engineer co-pilot.
You receive a Popeye sanitizer report (JSON) describing issues in a Kubernetes
namespace. Your job:

1. Quickly classify findings by severity.
2. If you need extra context from the Kubernetes API, emit a tool_call JSON:
   {"tool":"k8s_get","path":"/api/v1/namespaces/<ns>/pods"}
3. Once you have enough context, produce a remediation runbook containing:
   - One-line root-cause hypothesis.
   - Numbered remediation steps (with the exact kubectl commands).
   - Risk / blast-radius notes.
   - Suggested follow-up alerts to add.

Be terse, technical, and grounded ONLY in the JSON you received. Do not
hallucinate resource names. Use Markdown sections delimited by '## '.

Output ONLY your final remediation runbook in your last assistant message.
"""


# ---------------------------------------------------------------------------
# K8s API client (for tool calls during the agent loop)
# ---------------------------------------------------------------------------
def k8s_get(path: str) -> dict:
    url = f"{MOCK_K8S_URL.rstrip('/')}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {K8S_TOKEN}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.warning("tool k8s_get %s failed: %s", path, e)
        return {"error": str(e), "path": path}


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------
def _ollama_chat(messages: list[dict], stream: bool = False) -> str:
    """Call Ollama's OpenAI-compatible /v1/chat/completions endpoint."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    url = f"{OLLAMA_URL.rstrip('/')}/v1/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = json.loads(r.read().decode("utf-8"))
        elapsed = time.time() - t0
        text = body["choices"][0]["message"]["content"]
        log.info("ollama responded in %.2fs (%d chars)", elapsed, len(text))
        return text
    except urllib.error.URLError as e:
        log.warning("ollama unreachable: %s", e)
        raise
    except Exception as e:
        log.warning("ollama error: %s", e)
        raise


def _parse_tool_call(text: str) -> dict | None:
    """Detect a tool_call JSON blob inside the LLM response."""
    import re
    m = re.search(r"\{\s*\"tool\"\s*:\s*\"k8s_get\"\s*,\s*\"path\"\s*:\s*\"([^\"]+)\"\s*\}",
                  text)
    if m:
        return {"tool": "k8s_get", "path": m.group(1)}
    return None


# ---------------------------------------------------------------------------
# Structured remediation plan (D7)
#
# The Markdown runbook is for humans. The *plan* is what the remediation
# executor actually runs: a declarative list of steps, each naming a verb and
# a target, derived from the Popeye findings. The executor re-validates every
# step against its own allowlist, so nothing here is trusted on faith --
# neither this deterministic generator nor an LLM-authored plan.
# ---------------------------------------------------------------------------

# Popeye code -> plan step. Each entry is the ONLY thing that can put a step
# in a plan, which keeps the plan surface auditable and finite.
def _plan_steps_for(code: str, findings: list[dict], ns: str) -> list[dict]:
    names = sorted({f.get("name", "") for f in findings})
    if code == "POP-001":            # ImagePullBackOff
        return [{
            "id": "fix-payment-api-image",
            "reason": f"POP-001 ImagePullBackOff on {', '.join(names) or 'payment-api'}",
            "verb": "set-image",
            "resource": "deployment/payment-api",
            "namespace": ns,
            "args": {"container": "payment-api", "image": "nginx:latest"},
        }]
    if code in ("POP-002", "MEM-001"):   # CrashLoopBackOff / OOMKilled
        return [{
            "id": "fix-payment-worker-memory",
            "reason": f"{code} OOMKilled loop on {', '.join(names) or 'payment-worker'}",
            "verb": "set-resources",
            "resource": "deployment/payment-worker",
            "namespace": ns,
            "args": {"container": "payment-worker",
                     "limits_memory": "256Mi", "requests_memory": "192Mi"},
        }]
    if code in ("PVC-001", "PVC-002"):   # Pending PVC / missing StorageClass
        return [{
            "id": "fix-missing-storageclass",
            "reason": f"{code} PVC pending — StorageClass fast-ssd absent",
            "verb": "create-storageclass",
            "resource": "storageclass/fast-ssd",
            "namespace": None,
            "args": {"provisioner": "kubernetes.io/no-provisioner",
                     "volume_binding_mode": "WaitForFirstConsumer"},
        }]
    if code == "ING-001":            # dangling ingress
        return [{
            "id": "fix-dangling-ingress",
            "reason": "ING-001 Ingress backend Service does not exist",
            "verb": "set-ingress-backend",
            "resource": "ingress/payment-ingress",
            "namespace": ns,
            "args": {"service": "payment-api"},
        }]
    if code == "NO-002":             # node disk pressure — inspect only
        return [{
            "id": "inspect-disk-pressure",
            "reason": f"NO-002 DiskPressure on {', '.join(names) or 'node'}",
            "verb": "inspect-nodes",
            "resource": "nodes",
            "namespace": None,
            "args": {},
        }]
    return []


def build_plan(popeye_report: dict, backend: str = "stub") -> dict:
    """Derive a structured, executable plan from Popeye findings."""
    ns = popeye_report.get("namespace", "default")
    issues = popeye_report.get("issues", {}).get(ns, [])
    by_code: dict[str, list[dict]] = {}
    for i in issues:
        by_code.setdefault(i.get("code", "POP-000"), []).append(i)

    steps: list[dict] = []
    seen: set[str] = set()
    for code in sorted(by_code):
        for step in _plan_steps_for(code, by_code[code], ns):
            if step["id"] in seen:
                continue
            seen.add(step["id"])
            steps.append(step)

    return {
        "plan_version": 1,
        "namespace": ns,
        "generated_by": f"dify-lite/{backend}",
        "source": {
            "data_source": popeye_report.get("data_source", "unknown"),
            "engine": popeye_report.get("engine", "unknown"),
            "score": popeye_report.get("score"),
            "grade": popeye_report.get("grade"),
            "codes": sorted(by_code),
        },
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Deterministic rule-based SRE remediator (fallback / stub backend)
# ---------------------------------------------------------------------------
def stub_remediate(popeye_report: dict) -> str:
    """Build a remediation runbook from Popeye findings using heuristic rules.

    This is what the LLM would have produced — coded up deterministically so
    the demo always works even when Ollama is unreachable.
    """
    ns = popeye_report.get("namespace", "default")
    issues = popeye_report.get("issues", {}).get(ns, [])
    by_code: dict[str, list[dict]] = {}
    for i in issues:
        by_code.setdefault(i["code"], []).append(i)

    parts: list[str] = []
    parts.append(f"# A.O.P.S. Remediation Runbook — namespace `{ns}`\n")
    parts.append(f"_Generated by Dify-lite stub backend · "
                 f"Popeye score {popeye_report.get('score','?')}/100 "
                 f"(grade {popeye_report.get('grade','?')})_\n")

    # Root cause hypothesis
    parts.append("## Root Cause Hypothesis\n")
    root_causes: list[str] = []
    if "POP-001" in by_code:
        names = sorted({i["name"] for i in by_code["POP-001"]})
        root_causes.append(
            f"- **ImagePullBackOff** on pods {names} — the deployment "
            f"references an image tag that the registry cannot resolve."
        )
    if "POP-002" in by_code or "MEM-001" in by_code:
        root_causes.append(
            "- **OOMKilled loop** on payment-worker pods — the container's "
            "memory limit (128Mi) is too tight; peak usage hits the ceiling "
            "and Kubernetes kills it (exit 137), triggering CrashLoopBackOff."
        )
    if "NO-002" in by_code:
        root_causes.append(
            "- **DiskPressure** on worker-prod-02 — node disk usage 92% "
            "exceeds kubelet's 85% eviction threshold, which is also blocking "
            "pod placement and degrading I/O."
        )
    if "PVC-001" in by_code or "PVC-002" in by_code:
        root_causes.append(
            "- **Pending PVC** — the StorageClass `fast-ssd` referenced by "
            "payment-data-pvc was removed, so the CSI driver cannot provision."
        )
    if "ING-001" in by_code:
        root_causes.append(
            "- **Dangling Ingress** — payment-ingress routes to a Service "
            "`payment-frontend` that does not exist in this namespace."
        )
    if not root_causes:
        root_causes.append("- No high-severity root causes detected.")
    parts.extend(root_causes)
    parts.append("")

    # Remediation steps
    parts.append("## Remediation Steps\n")
    step = 1
    if "POP-001" in by_code:
        parts.append(
            f"{step}. **Fix the image tag on `payment-api`** — the tag "
            f"`broken-v9` does not exist in `registry.internal/payments/`. "
            f"Roll back to the last known-good tag and apply:\n"
            f"   ```bash\n"
            f"   kubectl -n {ns} set image deployment/payment-api "
            f"payment-api=registry.internal/payments/payment-api:v8\n"
            f"   kubectl -n {ns} rollout status deployment/payment-api "
            f"--timeout=120s\n"
            f"   ```\n"
        )
        step += 1
    if "POP-002" in by_code or "MEM-001" in by_code:
        parts.append(
            f"{step}. **Raise the memory limit on `payment-worker`** — "
            f"increase `memory.limit` from 128Mi to at least 256Mi (peak "
            f"observed 132Mi, headroom to 256Mi). Edit the Deployment:\n"
            f"   ```bash\n"
            f"   kubectl -n {ns} patch deployment payment-worker "
            f"--type=json -p='[{{\"op\":\"replace\","
            f"\"path\":\"/spec/template/spec/containers/0/resources/limits/memory\","
            f"\"value\":\"256Mi\"}}]'\n"
            f"   kubectl -n {ns} rollout status deployment/payment-worker "
            f"--timeout=120s\n"
            f"   ```\n"
        )
        step += 1
    if "NO-002" in by_code:
        parts.append(
            f"{step}. **Drain worker-prod-02 and clear its disk** — "
            f"the node is in DiskPressure (usage 92%). Identify the largest "
            f"consumers and prune, then uncordon:\n"
            f"   ```bash\n"
            f"   kubectl cordon worker-prod-02\n"
            f"   kubectl drain worker-prod-02 --ignore-daemonsets "
            f"--delete-emptydir-data --timeout=120s\n"
            f"   ssh worker-prod-02 'sudo crictl rmi --prune'\n"
            f"   ssh worker-prod-02 'sudo journalctl --vacuum-time=2d'\n"
            f"   kubectl uncordon worker-prod-02\n"
            f"   ```\n"
        )
        step += 1
    if "PVC-001" in by_code or "PVC-002" in by_code:
        parts.append(
            f"{step}. **Recreate the missing StorageClass `fast-ssd`** — "
            f"the PVC is Pending because no StorageClass matches. Reapply "
            f"the StorageClass, then PVC will bind automatically:\n"
            f"   ```bash\n"
            f"   cat <<EOF | kubectl apply -f -\n"
            f"   apiVersion: storage.k8s.io/v1\n"
            f"   kind: StorageClass\n"
            f"   metadata:\n"
            f"     name: fast-ssd\n"
            f"   provisioner: pd.csi.storage.gke.io\n"
            f"   volumeBindingMode: WaitForFirstConsumer\n"
            f"   allowVolumeExpansion: true\n"
            f"   parameters:\n"
            f"     type: pd-ssd\n"
            f"   EOF\n"
            f"   kubectl -n {ns} get pvc payment-data-pvc -w\n"
            f"   ```\n"
        )
        step += 1
    if "ING-001" in by_code:
        parts.append(
            f"{step}. **Fix the dangling Ingress backend** — "
            f"`payment-ingress` routes to Service `payment-frontend` which "
            f"does not exist. Either deploy the missing frontend Service or "
            f"correct the Ingress backend to point to `payment-api`:\n"
            f"   ```bash\n"
            f"   kubectl -n {ns} get svc\n"
            f"   kubectl -n {ns} edit ingress payment-ingress\n"
            f"   # In the editor, change backend.service.name from "
            f"'payment-frontend' to 'payment-api'\n"
            f"   ```\n"
        )
        step += 1
    parts.append("")

    # Risk / blast radius
    parts.append("## Risk & Blast Radius\n")
    risks = []
    if "POP-001" in by_code:
        risks.append(
            f"- **High**: `payment-api` currently serves zero traffic — "
            f"the SLO breach that triggered this alert will continue until "
            f"the image tag is fixed and the rollout completes."
        )
    if "POP-002" in by_code:
        risks.append(
            "- **Medium**: `payment-worker` is in CrashLoopBackOff; "
            "no payment processing is happening. Queue depth will grow."
        )
    if "NO-002" in by_code:
        risks.append(
            "- **Medium**: draining worker-prod-02 will evict "
            "the 2 payment-worker pods and 1 payment-api pod; ensure "
            "the other node has capacity or add a new node first."
        )
    if "PVC-001" in by_code:
        risks.append(
            "- **Low**: pending PVC is not yet mounted by any Pod — "
            "recreating the StorageClass is non-disruptive."
        )
    parts.extend(risks or ["- Low risk — no live customer traffic affected."])
    parts.append("")

    # Suggested alerts
    parts.append("## Suggested Additional Alerts\n")
    parts.append(
        "- `PaymentWorkerOOMKilled`: alert when "
        "`increase(kube_pod_container_status_last_terminated_reason"
        "{reason=\"OOMKilled\"}[5m]) > 0`.\n"
        "- `NodeDiskPressureHigh`: alert when "
        "`node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.15` "
        "for 5m.\n"
        "- `PVCStuckPending`: alert when "
        "`kube_persistentvolumeclaim_status_phase{phase=\"Pending\"} == 1` "
        "for 10m.\n"
        "- `DanglingIngress`: a Popeye scan cron job emitting an alert when "
        "`ING-001` findings are non-zero.\n"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------
def _extract_report(user_message: str) -> dict:
    """Pull the Popeye JSON out of a chat message (raw or embedded)."""
    try:
        return json.loads(user_message)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", user_message)
        try:
            return json.loads(m.group(0)) if m else {}
        except Exception:
            return {}


def _run_agent_loop(user_message: str, use_ollama: bool) -> tuple[str, dict]:
    """Two-round agent loop. Returns (final_text, trace_metadata)."""
    trace = {
        "backend": "stub" if not use_ollama else f"ollama:{OLLAMA_MODEL}",
        "rounds": [],
        "tool_calls": [],
    }

    if not use_ollama:
        # Stub backend — skip the LLM, go straight to deterministic remediator.
        trace["rounds"].append({
            "round": 1,
            "action": "stub_remediate",
            "note": "deterministic stub backend; no LLM call made",
        })
        # The user_message is expected to contain the Popeye JSON
        return stub_remediate(_extract_report(user_message)), trace

    # Ollama backend — actual 2-round agent loop
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    for rnd in range(1, MAX_AGENT_ROUNDS + 1):
        log.info("agent round %d — asking %s", rnd, OLLAMA_MODEL)
        try:
            reply = _ollama_chat(messages)
        except Exception as e:
            log.warning("ollama failed in round %d, falling back to stub: %s",
                        rnd, e)
            trace["fallback"] = True
            try:
                report = json.loads(user_message)
            except Exception:
                import re
                m = re.search(r"\{[\s\S]*\}", user_message)
                report = json.loads(m.group(0)) if m else {}
            return stub_remediate(report), trace

        trace["rounds"].append({
            "round": rnd,
            "assistant_reply_chars": len(reply),
        })

        tool_call = _parse_tool_call(reply)
        if tool_call:
            trace["tool_calls"].append(tool_call)
            log.info("round %d: tool_call %s", rnd, tool_call["path"])
            tool_result = k8s_get(tool_call["path"])
            messages.append({"role": "assistant", "content": reply})
            messages.append({
                "role": "user",
                "content": f"Tool result for {tool_call['path']}:\n"
                           f"{json.dumps(tool_result, indent=2)}\n\n"
                           f"Now produce the final remediation runbook.",
            })
            continue

        # No tool call -> this is the final answer
        log.info("round %d: final answer (%d chars)", rnd, len(reply))
        return reply, trace

    # If we exhausted rounds, take the last reply as final
    log.warning("exhausted %d rounds, returning last reply", MAX_AGENT_ROUNDS)
    return reply, trace


# ---------------------------------------------------------------------------
# FastAPI server (using stdlib http.server to avoid extra deps)
# ---------------------------------------------------------------------------
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _ollama_reachable() -> bool:
    try:
        url = f"{OLLAMA_URL.rstrip('/')}/api/tags"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def pick_backend() -> str:
    if LLM_BACKEND == "stub":
        return "stub"
    if LLM_BACKEND == "ollama":
        return "ollama" if _ollama_reachable() else "stub"
    # auto
    return "ollama" if _ollama_reachable() else "stub"


class DifyHandler(BaseHTTPRequestHandler):
    server_version = "DifyLite/0.1"

    def _send(self, code: int, obj: dict, ctype: str = "application/json"):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path in ("/healthz", "/livez", "/readyz", "/"):
            backend = pick_backend()
            return self._send(200, {
                "status": "ok",
                "service": "dify-lite",
                "backend_preferred": LLM_BACKEND,
                "backend_active": backend,
                "ollama_url": OLLAMA_URL,
                "ollama_model": OLLAMA_MODEL,
                "ollama_reachable": _ollama_reachable() if LLM_BACKEND != "stub" else False,
                "max_agent_rounds": MAX_AGENT_ROUNDS,
            })
        if u.path == "/v1/models":
            return self._send(200, {
                "object": "list",
                "data": [{
                    "id": OLLAMA_MODEL,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "dify-lite",
                }],
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        if u.path != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            req = json.loads(raw)
        except Exception as e:
            return self._send(400, {"error": f"bad JSON: {e}"})

        messages = req.get("messages", [])
        if not messages:
            return self._send(400, {"error": "messages[] required"})

        # Take the LAST user message as the agent input (typical n8n pattern
        # is to stuff the Popeye JSON into the user content). Be defensive
        # about message shape: n8n may send each message as a dict OR a string.
        def _msg_text(m):
            if isinstance(m, dict):
                return m.get("content", "")
            if isinstance(m, str):
                return m
            return str(m)
        def _msg_role(m):
            return m.get("role", "user") if isinstance(m, dict) else "user"

        user_msg = next((_msg_text(m) for m in reversed(messages)
                         if _msg_role(m) == "user"), "")

        backend = pick_backend()
        log.info("POST /v1/chat/completions backend=%s user_msg_chars=%d",
                 backend, len(user_msg))

        try:
            t0 = time.time()
            final_text, trace = _run_agent_loop(user_msg,
                                                 use_ollama=(backend == "ollama"))
            plan = build_plan(_extract_report(user_msg), backend)
            elapsed = time.time() - t0
        except Exception as e:
            log.exception("agent loop failed")
            return self._send(500, {"error": str(e)})

        # OpenAI-compatible chat.completion response
        return self._send(200, {
            "id": f"chatcmpl-aops-{int(time.time()*1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": OLLAMA_MODEL if backend == "ollama" else "dify-lite-stub",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": final_text,
                },
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": len(user_msg) // 4,
                "completion_tokens": len(final_text) // 4,
                "total_tokens": (len(user_msg) + len(final_text)) // 4,
            },
            "_dify_lite_trace": trace,
            "_dify_lite_duration_s": round(elapsed, 3),
            # The executable half of the answer. The Markdown above narrates;
            # this is what the remediation executor runs (after re-validating
            # every step against its own allowlist).
            "_dify_lite_plan": plan,
        })

    def do_HEAD(self):
        return self.do_GET()

    def log_message(self, fmt, *args):
        pass


def main():
    bind_host = os.environ.get("DIFY_LITE_HOST", "0.0.0.0")
    bind_port = int(os.environ.get("DIFY_LITE_PORT", "8002"))
    log.info("Dify-lite on %s:%d (preferred=%s, ollama_url=%s, model=%s)",
             bind_host, bind_port, LLM_BACKEND, OLLAMA_URL, OLLAMA_MODEL)
    srv = ThreadingHTTPServer((bind_host, bind_port), DifyHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
