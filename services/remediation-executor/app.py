#!/usr/bin/env python3
"""
A.O.P.S. Remediation Executor — actually applies kubectl fixes and re-scans.

This service:
  1. Receives (or fetches from dify-lite) a structured remediation plan
  2. Validates EVERY step against a finite allowlist, then applies the
     surviving steps as kubectl commands against a real Kubernetes cluster
  3. Waits for rollouts to complete
  4. Re-runs Popeye to get a real after-score
  5. Returns before/after comparison

Endpoints:
  POST /remediate  -> execute remediation plan
  GET  /status     -> last remediation status + before/after
  GET  /healthz

Environment:
  KUBECONFIG             path to kubeconfig (default: ~/.kube/config)
  POPEYE_URL             URL of popeye-scanner service
  DIFY_LITE_URL          URL of dify-lite (plan source when none is posted)
  AOPS_NAMESPACE         namespace to operate in (default: payment-prod)
  AOPS_SETTLE_SECONDS    pause between apply and re-scan (default: 5)
  DRY_RUN                set to "1" to preview without applying (default: "1")
"""

from __future__ import annotations

import os
import sys
import json
import time
import re
import subprocess
import logging
import urllib.request
import urllib.error
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [remediation] %(levelname)s %(message)s")
log = logging.getLogger("remediation")

KUBECONFIG = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
POPEYE_URL = os.environ.get("POPEYE_URL", "http://localhost:8004")
DIFY_URL = os.environ.get("DIFY_LITE_URL", "http://localhost:8002")
NAMESPACE = os.environ.get("AOPS_NAMESPACE", "payment-prod")
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
SETTLE_SECONDS = float(os.environ.get("AOPS_SETTLE_SECONDS", "5"))

BIND_HOST = os.environ.get("REMEDIATION_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("REMEDIATION_PORT", "8005"))

# Thread-safe state
STATE_LOCK = threading.Lock()
LAST_REMEDIATION: dict | None = None


# ---------------------------------------------------------------------------
# Kubectl helper
# ---------------------------------------------------------------------------
def _kubectl(args: list[str], input_data: str | None = None) -> dict:
    """Run a kubectl command, return {ok, stdout, stderr, returncode}."""
    cmd = ["kubectl", "--kubeconfig", KUBECONFIG] + args
    if DRY_RUN:
        log.info("[DRY RUN] would run: %s", " ".join(cmd))
        return {"ok": True, "stdout": f"[DRY RUN] {' '.join(cmd)}",
                "stderr": "", "returncode": 0}
    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            input=input_data,
        )
        ok = result.returncode == 0
        if not ok:
            log.warning("kubectl failed (rc=%d): %s", result.returncode, result.stderr[:200])
        return {"ok": ok, "stdout": result.stdout, "stderr": result.stderr,
                "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        log.error("kubectl timed out: %s", " ".join(cmd))
        return {"ok": False, "stdout": "", "stderr": "timeout after 120s",
                "returncode": -1}
    except FileNotFoundError:
        log.error("kubectl not found — is it installed and in PATH?")
        return {"ok": False, "stdout": "", "stderr": "kubectl not found in PATH",
                "returncode": -1}


# ---------------------------------------------------------------------------
# Plan execution with an allowlist (D7)
#
# The executor runs a declarative plan produced by dify-lite from Popeye
# findings. It does NOT trust that plan: every step is re-validated here
# against a finite allowlist of (verb -> handler, resource pattern) before any
# kubectl runs. An unknown verb, a resource outside the pattern, or a step
# targeting another namespace is rejected and recorded, never executed.
#
# This is the boundary that makes "an LLM decides what to run" safe: the model
# can only ever select from verbs that already exist here.
# ---------------------------------------------------------------------------

RESOURCE_RE = re.compile(r"^(?:(?P<kind>[a-z]+)/)?(?P<name>[a-z0-9][a-z0-9.-]{0,252})$")


def _check_resource(resource: str, allowed_kinds: tuple[str, ...]) -> str:
    m = RESOURCE_RE.match(resource or "")
    if not m:
        raise ValueError(f"malformed resource {resource!r}")
    kind = m.group("kind") or ""
    if kind not in allowed_kinds:
        raise ValueError(f"resource kind {kind or '<none>'!r} not allowed here "
                         f"(expected one of {allowed_kinds})")
    return m.group("name")


def do_set_image(step: dict, ns: str) -> dict:
    name = _check_resource(step["resource"], ("deployment",))
    args = step.get("args", {})
    container = args.get("container") or name
    image = args.get("image")
    if not image:
        raise ValueError("set-image requires args.image")
    r = _kubectl(["set", "image", f"deployment/{name}",
                  f"{container}={image}", "-n", ns])
    if not r["ok"]:
        return {"ok": False, "detail": r["stderr"]}
    r2 = _kubectl(["rollout", "status", f"deployment/{name}",
                   "-n", ns, "--timeout=120s"])
    return {"ok": r2["ok"], "detail": r2["stdout"] or r2["stderr"]}


def do_set_resources(step: dict, ns: str) -> dict:
    name = _check_resource(step["resource"], ("deployment",))
    args = step.get("args", {})
    patch = json.dumps([
        {"op": "replace",
         "path": "/spec/template/spec/containers/0/resources/limits/memory",
         "value": args.get("limits_memory", "256Mi")},
        {"op": "replace",
         "path": "/spec/template/spec/containers/0/resources/requests/memory",
         "value": args.get("requests_memory", "192Mi")},
    ])
    r = _kubectl(["patch", f"deployment/{name}", "--type=json", "-p", patch,
                  "-n", ns])
    if not r["ok"]:
        return {"ok": False, "detail": r["stderr"]}
    r2 = _kubectl(["rollout", "status", f"deployment/{name}",
                   "-n", ns, "--timeout=120s"])
    return {"ok": r2["ok"], "detail": r2["stdout"] or r2["stderr"]}


def do_create_storageclass(step: dict, ns: str) -> dict:
    name = _check_resource(step["resource"], ("storageclass",))
    args = step.get("args", {})
    sc_yaml = f"""apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {name}
provisioner: {args.get("provisioner", "kubernetes.io/no-provisioner")}
volumeBindingMode: {args.get("volume_binding_mode", "WaitForFirstConsumer")}
allowVolumeExpansion: true
"""
    r = _kubectl(["apply", "-f", "-"], input_data=sc_yaml)
    return {"ok": r["ok"], "detail": r["stdout"] or r["stderr"]}


def do_set_ingress_backend(step: dict, ns: str) -> dict:
    name = _check_resource(step["resource"], ("ingress",))
    service = step.get("args", {}).get("service")
    if not service:
        raise ValueError("set-ingress-backend requires args.service")
    patch = json.dumps([
        {"op": "replace",
         "path": "/spec/rules/0/http/paths/0/backend/service/name",
         "value": service},
    ])
    r = _kubectl(["patch", f"ingress/{name}", "--type=json", "-p", patch,
                  "-n", ns])
    return {"ok": r["ok"], "detail": r["stdout"] or r["stderr"]}


def do_inspect_nodes(step: dict, ns: str) -> dict:
    """Read-only. Node disk pressure needs host access A.O.P.S. does not have,
    so this reports state rather than pretending to fix it."""
    r = _kubectl(["get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"])
    if not r["ok"]:
        return {"ok": False, "detail": r["stderr"]}
    details = []
    for node in r["stdout"].split():
        r2 = _kubectl(["describe", "node", node])
        details.append(f"{node}: "
                       + ("DiskPressure — manual cleanup required"
                          if "DiskPressure" in r2["stdout"] else "OK"))
    return {"ok": True, "detail": "; ".join(details) or "no nodes returned"}


# verb -> (handler, mutating?)
ALLOWED_VERBS: dict[str, tuple] = {
    "set-image":           (do_set_image, True),
    "set-resources":       (do_set_resources, True),
    "create-storageclass": (do_create_storageclass, True),
    "set-ingress-backend": (do_set_ingress_backend, True),
    "inspect-nodes":       (do_inspect_nodes, False),
}


def validate_step(step: dict, ns: str) -> tuple[bool, str]:
    """Reject anything the executor is not explicitly built to do."""
    if not isinstance(step, dict):
        return False, "step is not an object"
    verb = step.get("verb")
    if verb not in ALLOWED_VERBS:
        return False, (f"verb {verb!r} is not in the allowlist "
                       f"({sorted(ALLOWED_VERBS)})")
    step_ns = step.get("namespace")
    if step_ns not in (None, ns):
        return False, (f"step targets namespace {step_ns!r}; this executor is "
                       f"scoped to {ns!r}")
    if not step.get("resource"):
        return False, "step has no resource"
    return True, "ok"


def execute_plan(plan: dict, ns: str) -> list[dict]:
    """Validate then run each step. Rejected steps are recorded, not run."""
    results = []
    for i, step in enumerate(plan.get("steps", [])):
        step_id = (step or {}).get("id", f"step-{i}")
        ok, why = validate_step(step, ns)
        if not ok:
            log.warning("REJECTED %s: %s", step_id, why)
            results.append({"step": step_id, "verb": (step or {}).get("verb"),
                            "status": "rejected", "ok": False, "detail": why})
            continue
        handler, mutating = ALLOWED_VERBS[step["verb"]]
        log.info("Running %s (%s%s)", step_id, step["verb"],
                 "" if mutating else ", read-only")
        try:
            out = handler(step, ns)
        except Exception as e:
            log.warning("  %s raised: %s", step_id, e)
            out = {"ok": False, "detail": str(e)}
        results.append({
            "step": step_id,
            "verb": step["verb"],
            "resource": step.get("resource"),
            "reason": step.get("reason"),
            "mutating": mutating,
            "status": "applied" if out["ok"] else "failed",
            **out,
        })
        log.info("  %s -> %s", step_id, "OK" if out["ok"] else "FAILED")
    return results


# ---------------------------------------------------------------------------
# Popeye scanner integration (real HTTP call)
# ---------------------------------------------------------------------------
def run_popeye_scan() -> dict:
    """Call the Popeye scanner service to get a real report."""
    url = f"{POPEYE_URL.rstrip('/')}/scan?namespace={NAMESPACE}"
    try:
        req = urllib.request.Request(url, method="POST",
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.error("Popeye scan failed: %s", e)
        return {"error": str(e), "score": 0, "grade": "F", "findings_count": 0}


# ---------------------------------------------------------------------------
# Main remediation flow
# ---------------------------------------------------------------------------
def fetch_plan_from_agent(report: dict) -> dict | None:
    """Ask dify-lite for a plan derived from this Popeye report."""
    body = json.dumps({
        "model": "aops",
        "messages": [{"role": "user", "content": json.dumps(report)}],
    }).encode()
    req = urllib.request.Request(
        f"{DIFY_URL.rstrip('/')}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode()).get("_dify_lite_plan")
    except Exception as e:
        log.warning("could not fetch plan from dify-lite: %s", e)
        return None


def execute_remediation(plan: dict | None = None) -> dict:
    """Run a remediation plan, capturing before/after Popeye scores.

    `plan` is the plan to execute. If none is supplied, one is requested from
    dify-lite based on the current scan. Either way every step is validated
    against ALLOWED_VERBS before it runs.
    """
    started = time.time()
    log.info("=== Starting remediation (dry_run=%s) ===", DRY_RUN)

    before = run_popeye_scan()
    log.info("BEFORE: score=%s grade=%s findings=%s data_source=%s",
             before.get("score"), before.get("grade"),
             before.get("findings_count"), before.get("data_source"))

    if before.get("error"):
        return {"status": "aborted",
                "reason": f"cannot scan before remediating: {before['error']}"}

    # A report built from fixtures describes no real cluster; refuse to apply
    # mutations on the strength of it unless explicitly running dry.
    provenance = before.get("data_source", "unknown")
    if provenance != "live-cluster" and not DRY_RUN:
        return {
            "status": "refused",
            "reason": (f"scan data_source={provenance!r}, not 'live-cluster' — "
                       f"refusing to apply real kubectl changes based on a "
                       f"report that does not describe a real cluster."),
            "hint": "Run the scanner with AOPS_MODE=real, or set DRY_RUN=1.",
            "before": before.get("score"),
        }

    if plan is None:
        plan = fetch_plan_from_agent(before)
    if not plan or not plan.get("steps"):
        return {"status": "no-op",
                "reason": "no remediation plan available for these findings",
                "data_source": provenance}

    ns = plan.get("namespace") or NAMESPACE
    log.info("Executing plan from %s: %d step(s) in ns=%s",
             plan.get("generated_by", "?"), len(plan["steps"]), ns)
    results = execute_plan(plan, ns)

    if SETTLE_SECONDS > 0:
        time.sleep(SETTLE_SECONDS)

    after = run_popeye_scan()
    log.info("AFTER: score=%s grade=%s findings=%s",
             after.get("score"), after.get("grade"), after.get("findings_count"))

    elapsed = round(time.time() - started, 3)
    report = {
        "status": "completed",
        "dry_run": DRY_RUN,
        "namespace": ns,
        "duration_s": elapsed,
        "data_source": provenance,
        "plan": {
            "generated_by": plan.get("generated_by"),
            "step_count": len(plan["steps"]),
            "codes": plan.get("source", {}).get("codes", []),
        },
        "before": {
            "score": before.get("score", 0),
            "grade": before.get("grade", "?"),
            "findings_count": before.get("findings_count", 0),
            "errors": before.get("findings_by_severity", {}).get("error", 0),
            "warnings": before.get("findings_by_severity", {}).get("warning", 0),
        },
        "after": {
            "score": after.get("score", 0),
            "grade": after.get("grade", "?"),
            "findings_count": after.get("findings_count", 0),
            "errors": after.get("findings_by_severity", {}).get("error", 0),
            "warnings": after.get("findings_by_severity", {}).get("warning", 0),
        },
        "improvement": {
            "score_delta": (after.get("score", 0) - before.get("score", 0)),
            "findings_delta": (before.get("findings_count", 0)
                               - after.get("findings_count", 0)),
        },
        "steps": results,
        "steps_applied": sum(1 for r in results if r["status"] == "applied"),
        "steps_rejected": sum(1 for r in results if r["status"] == "rejected"),
        "steps_failed": sum(1 for r in results if r["status"] == "failed"),
    }

    with STATE_LOCK:
        global LAST_REMEDIATION
        LAST_REMEDIATION = report

    log.info("=== Remediation complete in %.2fs (%d applied, %d rejected, %d failed) ===",
             elapsed, report["steps_applied"], report["steps_rejected"],
             report["steps_failed"])
    return report


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class RemediationHandler(BaseHTTPRequestHandler):
    server_version = "AOPSRemediation/0.1"

    def _send(self, code: int, obj: dict, ctype: str = "application/json"):
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
                "service": "remediation-executor",
                "dry_run": DRY_RUN,
                "namespace": NAMESPACE,
                "kubeconfig": KUBECONFIG,
                "has_last_remediation": LAST_REMEDIATION is not None,
                "allowed_verbs": sorted(ALLOWED_VERBS),
                "settle_seconds": SETTLE_SECONDS,
            })
        if u.path == "/status":
            with STATE_LOCK:
                data = LAST_REMEDIATION
            if data is None:
                return self._send(200, {"status": "no remediation run yet"})
            return self._send(200, data)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/remediate":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            plan = None
            if raw:
                try:
                    body = json.loads(raw)
                except Exception:
                    body = None
                # Accept either a bare plan or an envelope carrying one.
                if isinstance(body, dict):
                    plan = body.get("plan") if "plan" in body else body
                    # An empty body/envelope means "no plan supplied" — fall
                    # through to asking dify-lite — not "a plan with no steps".
                    if not isinstance(plan, dict) or not plan:
                        plan = None
            try:
                result = execute_remediation(plan)
                return self._send(200, result)
            except Exception as e:
                log.exception("remediation failed")
                return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def do_HEAD(self):
        return self.do_GET()

    def log_message(self, fmt, *args):
        pass


def main():
    log.info("Remediation executor on %s:%d (dry_run=%s, ns=%s, kubeconfig=%s)",
             BIND_HOST, BIND_PORT, DRY_RUN, NAMESPACE, KUBECONFIG)
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), RemediationHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
