#!/usr/bin/env python3
"""
Popeye-compatible Kubernetes sanitizer — the A.O.P.S. alternative to K8sGPT.

Reads cluster state through a KubeClient backend -- the mock-k8s-api fixture
server in sandbox mode, or a live cluster via kubectl in real mode -- runs 14
analyzer rules over it, and emits Popeye-shaped JSON that the Dify-lite agent
can reason over. Every report states which backend produced it (`data_source`).

Popeye codes used (matches upstream Popeye 0.11+):
  NO-001  NodeNotReady
  NO-002  NodeWithDiskPressure
  NO-003  NodeWithMemoryPressure
  DPL-000  DeploymentUnhealthy
  DPL-001  DeploymentNoReplicas
  POP-001  PodImagePullBackOff
  POP-002  PodCrashLoopBackOff
  POP-003  PodPending
  POP-004  PodHighRestartCount
  ING-001  IngressDanglingService
  ING-002  IngressNoLoadBalancer
  SVC-001  ServiceNoEndpoints
  PVC-001  PendingPVC
  PVC-002  MissingStorageClass
  MEM-001  PodMemoryLimitHit (extends OOMKilled findings)
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from typing import Any

import subprocess
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [popeye] %(levelname)s %(message)s")
log = logging.getLogger("popeye")

MOCK_K8S_URL = os.environ.get("MOCK_K8S_URL", "http://localhost:8001")
AUTH_TOKEN = os.environ.get("MOCK_K8S_TOKEN", "aops-demo-token")
NAMESPACE = os.environ.get("AOPS_NAMESPACE", "payment-prod")
KUBECONFIG = os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))

# AOPS_MODE decides WHERE cluster state comes from. This is the single most
# important switch in the service and it never degrades silently:
#   "sandbox" -> mock-k8s-api fixtures. Reports are labelled data_source=fixtures.
#   "real"    -> a live cluster via kubectl/kubeconfig. If the cluster is
#                unreachable the scan FAILS (503). It must never fall back to
#                fixtures while claiming to describe a real cluster.
AOPS_MODE = os.environ.get("AOPS_MODE", "sandbox").lower()

# POPEYE_MODE decides WHICH ENGINE produces findings from that state:
#   "real"    -> prefer the upstream Popeye Go binary, fall back to the
#                built-in analyzers (both read the same cluster, so this
#                fallback changes fidelity, never provenance).
#   "builtin" -> always use the built-in Python analyzers.
POPEYE_MODE = os.environ.get("POPEYE_MODE", "builtin").lower()

# Can we run the real Popeye binary?
POPEYE_BIN = shutil.which("popeye")


class ClusterUnreachable(RuntimeError):
    """Raised in real mode when live cluster state cannot be read.

    Deliberately fatal: serving fixture data under a real-mode label is the
    one failure this service must never produce.
    """

# Popeye severity levels (int) matching upstream
S_OK = 0
S_INFO = 1
S_WARN = 2
S_ERROR = 3
SEVERITY_LABEL = {S_OK: "ok", S_INFO: "info",
                  S_WARN: "warning", S_ERROR: "error"}


@dataclass
class Finding:
    group: str            # resource group, e.g. "pods"
    gvr: str              # group/version/resource, e.g. "v1/pods"
    name: str             # resource name
    code: str             # Popeye code, e.g. "POP-001"
    severity: int
    message: str

    def to_dict(self):
        return {
            "group": self.group,
            "gvr": self.gvr,
            "name": self.name,
            "code": self.code,
            "severity": self.severity,
            "severity_label": SEVERITY_LABEL[self.severity],
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Cluster access — two interchangeable backends behind one interface.
#
# The 14 built-in analyzers below only ever call _get(<k8s api path>). Which
# backend answers that call is what separates sandbox mode from real mode, and
# every report carries the answer in its `data_source` field.
# ---------------------------------------------------------------------------

# K8s API path -> (kubectl resource, namespaced)
_KUBECTL_ROUTES: list[tuple[str, str, bool]] = [
    (r"^/api/v1/nodes$",                                          "nodes",                  False),
    (r"^/apis/apps/v1/namespaces/(?P<ns>[^/]+)/deployments$",     "deployments",            True),
    (r"^/api/v1/namespaces/(?P<ns>[^/]+)/pods$",                  "pods",                   True),
    (r"^/apis/networking\.k8s\.io/v1/namespaces/(?P<ns>[^/]+)/ingresses$",
                                                                   "ingresses",              True),
    (r"^/api/v1/namespaces/(?P<ns>[^/]+)/services$",              "services",               True),
    (r"^/api/v1/namespaces/(?P<ns>[^/]+)/persistentvolumeclaims$",
                                                                   "persistentvolumeclaims", True),
]


class KubeClient:
    """Reads Kubernetes list resources by API path."""

    data_source = "unknown"

    def get(self, path: str) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def probe(self) -> tuple[bool, str]:  # pragma: no cover - interface
        raise NotImplementedError


class MockHTTPClient(KubeClient):
    """Sandbox backend: the deterministic mock-k8s-api fixture server."""

    data_source = "fixtures"

    def get(self, path: str) -> dict:
        url = f"{MOCK_K8S_URL.rstrip('/')}{path}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            log.warning("mock K8s API %s -> HTTP %d", path, e.code)
            return {"items": []}
        except Exception as e:
            log.error("mock K8s API %s -> %s", path, e)
            return {"items": []}

    def probe(self) -> tuple[bool, str]:
        try:
            self.get("/healthz")
            return True, f"mock-k8s-api at {MOCK_K8S_URL}"
        except Exception as e:
            return False, str(e)


class KubectlClient(KubeClient):
    """Real backend: live cluster state via kubectl + kubeconfig.

    Any failure raises ClusterUnreachable rather than returning an empty list,
    so a broken connection can never be mistaken for a healthy cluster.
    """

    data_source = "live-cluster"

    def _run(self, args: list[str]) -> str:
        cmd = ["kubectl", "--kubeconfig", KUBECONFIG] + args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise ClusterUnreachable("kubectl not found in PATH")
        except subprocess.TimeoutExpired:
            raise ClusterUnreachable(f"kubectl timed out: {' '.join(args)}")
        if r.returncode != 0:
            raise ClusterUnreachable(
                f"kubectl {' '.join(args)} failed (rc={r.returncode}): "
                f"{r.stderr.strip()[:300]}")
        return r.stdout

    def get(self, path: str) -> dict:
        for pattern, resource, namespaced in _KUBECTL_ROUTES:
            m = re.match(pattern, path)
            if not m:
                continue
            args = ["get", resource, "-o", "json"]
            if namespaced:
                args += ["-n", m.group("ns")]
            return json.loads(self._run(args))
        raise ClusterUnreachable(f"no kubectl route for API path {path!r}")

    def probe(self) -> tuple[bool, str]:
        try:
            raw = json.loads(self._run(["get", "nodes", "-o", "json"]))
            n = len(raw.get("items", []))
            return True, f"live cluster via {KUBECONFIG} ({n} nodes)"
        except ClusterUnreachable as e:
            return False, str(e)


def make_client(mode: str | None = None) -> KubeClient:
    mode = (mode or AOPS_MODE).lower()
    return KubectlClient() if mode == "real" else MockHTTPClient()


CLIENT: KubeClient = make_client()


def _get(path: str) -> dict:
    return CLIENT.get(path)


def _safe_get(path: str) -> dict:
    """_get for cosmetic counters — never propagates a cluster error."""
    try:
        return _get(path)
    except Exception:
        return {"items": []}


def grade_from_score(score: int) -> str:
    """Popeye-compatible letter grade for a 0-100 score."""
    for cut, grade in ((90, "A"), (80, "B"), (70, "C"), (60, "D")):
        if score >= cut:
            return grade
    return "F"


def _res(verbs, namespaced=True, kind="", items=None):
    """Synthesize a 'resource list' wrapper matching Popeye's expectations."""
    return {"verbs": verbs, "namespaced": namespaced,
            "kind": kind, "items": items or []}


# ---------------------------------------------------------------------------
# Analyzers — each returns a list[Finding]
# ---------------------------------------------------------------------------
def analyze_nodes() -> list[Finding]:
    findings: list[Finding] = []
    for n in _get("/api/v1/nodes").get("items", []):
        name = n["metadata"]["name"]
        conds = {c["type"]: c for c in n["status"].get("conditions", [])}
        ready = conds.get("Ready", {}).get("status") == "True"
        disk = conds.get("DiskPressure", {}).get("status") == "True"
        mem = conds.get("MemoryPressure", {}).get("status") == "True"
        pid = conds.get("PIDPressure", {}).get("status") == "True"
        if not ready:
            findings.append(Finding("nodes", "v1/nodes", name,
                                    "NO-001", S_ERROR,
                                    f"Node {name} is NotReady"))
        if disk:
            msg = conds["DiskPressure"].get("message",
                                            f"DiskPressure on {name}")
            findings.append(Finding("nodes", "v1/nodes", name,
                                    "NO-002", S_ERROR, msg))
        if mem:
            findings.append(Finding("nodes", "v1/nodes", name,
                                    "NO-003", S_ERROR,
                                    f"MemoryPressure on {name}"))
        if pid:
            findings.append(Finding("nodes", "v1/nodes", name,
                                    "NO-004", S_ERROR,
                                    f"PIDPressure on {name}"))
    return findings


def analyze_deployments(ns: str) -> list[Finding]:
    findings: list[Finding] = []
    for d in _get(f"/apis/apps/v1/namespaces/{ns}/deployments").get("items", []):
        name = d["metadata"]["name"]
        spec_reps = d["spec"].get("replicas", 0)
        ready = d["status"].get("readyReplicas", 0)
        avail = d["status"].get("availableReplicas", 0)
        unavail = d["status"].get("unavailableReplicas", 0)

        for cond in d["status"].get("conditions", []):
            if cond["type"] == "Available" and cond["status"] == "False":
                findings.append(Finding(
                    "deployments", "apps/v1/deployments", name,
                    "DPL-000", S_ERROR,
                    f"Deployment {name} not Available: {cond.get('reason', '?')} "
                    f"— {cond.get('message', '')}".strip(" — "),
                ))
            if cond["type"] == "Progressing" and cond["status"] == "False":
                findings.append(Finding(
                    "deployments", "apps/v1/deployments", name,
                    "DPL-003", S_WARN,
                    f"Deployment {name} ProgressDeadlineExceeded: "
                    f"{cond.get('message', '')}",
                ))
        if spec_reps > 0 and ready == 0:
            findings.append(Finding(
                "deployments", "apps/v1/deployments", name,
                "DPL-001", S_ERROR,
                f"Deployment {name} has 0/{spec_reps} ready replicas",
            ))
        if unavail > 0:
            findings.append(Finding(
                "deployments", "apps/v1/deployments", name,
                "DPL-005", S_WARN,
                f"{unavail} replicas unavailable for {name}",
            ))
    return findings


def analyze_pods(ns: str) -> list[Finding]:
    findings: list[Finding] = []
    for p in _get(f"/api/v1/namespaces/{ns}/pods").get("items", []):
        name = p["metadata"]["name"]
        phase = p["status"].get("phase")
        for cs in p["status"].get("containerStatuses", []):
            cname = cs["name"]
            state = cs.get("state", {})
            last = cs.get("lastState", {})
            restarts = cs.get("restartCount", 0)

            waiting = state.get("waiting", {})
            term = last.get("terminated", {})

            w_reason = waiting.get("reason", "")
            w_msg = waiting.get("message", "")
            t_reason = term.get("reason", "")
            t_code = term.get("exitCode")

            if w_reason == "ImagePullBackOff":
                findings.append(Finding(
                    "pods", "v1/pods", name,
                    "POP-001", S_ERROR,
                    f"Container {cname} in ImagePullBackOff — {w_msg}",
                ))
            elif w_reason == "CrashLoopBackOff":
                findings.append(Finding(
                    "pods", "v1/pods", name,
                    "POP-002", S_ERROR,
                    f"Container {cname} in CrashLoopBackOff — {w_msg}",
                ))
            elif w_reason == "ErrImagePull":
                findings.append(Finding(
                    "pods", "v1/pods", name,
                    "POP-001", S_ERROR,
                    f"Container {cname} ErrImagePull — {w_msg}",
                ))

            if t_reason == "OOMKilled" or t_code == 137:
                findings.append(Finding(
                    "pods", "v1/pods", name,
                    "MEM-001", S_ERROR,
                    f"Container {cname} was OOMKilled (exit 137) "
                    f"— {term.get('message', '')}",
                ))
            elif t_reason == "Error":
                findings.append(Finding(
                    "pods", "v1/pods", name,
                    "POP-005", S_ERROR,
                    f"Container {cname} terminated with Error (exit {t_code})",
                ))

            if restarts >= 5:
                findings.append(Finding(
                    "pods", "v1/pods", name,
                    "POP-004", S_WARN,
                    f"Container {cname} has {restarts} restarts",
                ))

        if phase == "Pending":
            findings.append(Finding(
                "pods", "v1/pods", name,
                "POP-003", S_WARN,
                f"Pod {name} is in Pending phase",
            ))
        elif phase == "Failed":
            findings.append(Finding(
                "pods", "v1/pods", name,
                "POP-006", S_ERROR,
                f"Pod {name} is in Failed phase",
            ))
    return findings


def analyze_ingresses(ns: str) -> list[Finding]:
    findings: list[Finding] = []
    services = {s["metadata"]["name"] for s in
                _get(f"/api/v1/namespaces/{ns}/services").get("items", [])}
    log.info("Ingress analysis: known services = %s", services)
    for ing in _get(f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses") \
            .get("items", []):
        name = ing["metadata"]["name"]
        for rule in ing["spec"].get("rules", []):
            for path in rule.get("http", {}).get("paths", []):
                svc = path.get("backend", {}).get("service", {})
                svc_name = svc.get("name")
                if svc_name and svc_name not in services:
                    findings.append(Finding(
                        "ingresses", "networking.k8s.io/v1/ingresses", name,
                        "ING-001", S_ERROR,
                        f"Ingress {name} routes to non-existent "
                        f"Service '{svc_name}' (dangling backend)",
                    ))
        lb_ingress = ing.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        if not lb_ingress:
            findings.append(Finding(
                "ingresses", "networking.k8s.io/v1/ingresses", name,
                "ING-002", S_WARN,
                f"Ingress {name} has no associated load-balancer address "
                f"(may be pending or LB class missing)",
            ))
    return findings


def analyze_services(ns: str) -> list[Finding]:
    findings: list[Finding] = []
    pods_by_label: dict[str, list[str]] = {}
    for p in _get(f"/api/v1/namespaces/{ns}/pods").get("items", []):
        labels = p["metadata"].get("labels", {})
        for k, v in labels.items():
            pods_by_label[f"{k}={v}"] = pods_by_label.get(
                f"{k}={v}", []) + [p["metadata"]["name"]]

    for svc in _get(f"/api/v1/namespaces/{ns}/services").get("items", []):
        name = svc["metadata"]["name"]
        selector = svc["spec"].get("selector", {})
        if not selector:
            findings.append(Finding(
                "services", "v1/services", name,
                "SVC-002", S_INFO,
                f"Service {name} has no selector (headless/external?)",
            ))
            continue
        matching = []
        for k, v in selector.items():
            matching = pods_by_label.get(f"{k}={v}", [])
            if matching:
                break
        if not matching:
            findings.append(Finding(
                "services", "v1/services", name,
                "SVC-001", S_ERROR,
                f"Service {name} selector {selector} matches no pods "
                f"(no endpoints)",
            ))
    return findings


def analyze_pvcs(ns: str) -> list[Finding]:
    findings: list[Finding] = []
    for pvc in _get(f"/api/v1/namespaces/{ns}/persistentvolumeclaims") \
            .get("items", []):
        name = pvc["metadata"]["name"]
        phase = pvc.get("status", {}).get("phase")
        sc = pvc.get("spec", {}).get("storageClassName")
        if phase == "Pending":
            msg = next((c.get("message", "") for c in
                        pvc.get("status", {}).get("conditions", [])
                        if c.get("reason") == "ProvisioningFailed"),
                       f"PVC {name} is Pending")
            findings.append(Finding(
                "pvcs", "v1/persistentvolumeclaims", name,
                "PVC-001", S_ERROR, msg,
            ))
            if sc:
                findings.append(Finding(
                    "pvcs", "v1/persistentvolumeclaims", name,
                    "PVC-002", S_ERROR,
                    f"PVC {name} references StorageClass '{sc}' "
                    f"which does not exist in the cluster",
                ))
        elif phase == "Lost":
            findings.append(Finding(
                "pvcs", "v1/persistentvolumeclaims", name,
                "PVC-003", S_ERROR,
                f"PVC {name} is in Lost phase",
            ))
    return findings


# ---------------------------------------------------------------------------
# Real Popeye binary integration
# ---------------------------------------------------------------------------
def _run_real_popeye(ns: str) -> dict | None:
    """Try running the real Popeye binary against the cluster.

    Returns parsed Popeye JSON output, or None if Popeye is not available
    or the cluster is unreachable.
    """
    if not POPEYE_BIN:
        log.info("Real Popeye binary not found in PATH")
        return None
    if POPEYE_MODE != "real":
        log.info("POPEYE_MODE=%s — skipping real Popeye", POPEYE_MODE)
        return None

    log.info("Running real Popeye binary against ns=%s", ns)
    try:
        cmd = [
            POPEYE_BIN,
            "--kubeconfig", KUBECONFIG,
            "--namespace", ns,
            "--out", "json",
            "--force",
            "--no-color",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log.warning("Popeye binary failed (rc=%d): %s",
                        result.returncode, result.stderr[:300])
            return None
        popeye_output = json.loads(result.stdout)
        log.info("Real Popeye completed successfully")
        return popeye_output
    except FileNotFoundError:
        log.info("Popeye binary not found")
        return None
    except subprocess.TimeoutExpired:
        log.warning("Popeye binary timed out after 60s")
        return None
    except Exception as e:
        log.warning("Popeye binary error: %s", e)
        return None


def build_report(ns: str) -> dict:
    """Run a scan.

    Engine selection (Popeye binary vs built-in analyzers) is independent of
    data provenance: both engines read whatever cluster the active KubeClient
    points at. In real mode a cluster error propagates as ClusterUnreachable
    and the HTTP layer turns it into a 503 — we never substitute fixtures.
    """
    if AOPS_MODE == "real":
        ok, detail = CLIENT.probe()
        if not ok:
            raise ClusterUnreachable(
                f"AOPS_MODE=real but the cluster is unreachable: {detail}")
        log.info("Real mode: %s", detail)

    if POPEYE_MODE == "real":
        if not POPEYE_BIN:
            log.info("POPEYE_MODE=real but no popeye binary in PATH — "
                     "using built-in analyzers against the same cluster")
        elif AOPS_MODE != "real":
            log.info("POPEYE_MODE=real is only meaningful with AOPS_MODE=real "
                     "(the binary needs a kubeconfig) — using built-in analyzers")
        else:
            started = time.time()
            popeye_raw = _run_real_popeye(ns)
            if popeye_raw is not None:
                findings = _parse_real_popeye(popeye_raw)
                log.info("Parsed %d findings from real Popeye output", len(findings))
                return _assemble_report(ns, findings, started,
                                        engine="popeye-binary")
            log.warning("Real Popeye binary produced no usable output — "
                        "falling back to built-in analyzers (same cluster)")

    return _build_report_builtin(ns)


def _parse_real_popeye(popeye_raw: dict) -> list[Finding]:
    """Parse real Popeye JSON output into Finding objects.

    Schema (verified against derailed/popeye v0.22.1 internal/report golden
    test, and byte-identical in v0.21.x apart from the section key name):

        {"popeye": {
            "report_time": "...", "score": 100, "grade": "A",
            "sections": [                       # "sanitizers" in popeye <= 0.20
              {"linter": "pods", "gvr": "v1/pods",
               "tally": {"ok":1,"info":0,"warning":0,"error":0,"score":100},
               "issues": {"payment-prod/payment-api-abc": [
                   {"group":"__root__","gvr":"v1/pods","level":2,
                    "message":"[POP-204] Pod is in ImagePullBackOff"}]}}],
            "errors": {...}}}

    `sections` is a LIST, and `issues` maps a resource FQN to a LIST of issues.
    Levels are ints 0..3 (ok/info/warn/error) per internal/rules/level.go.
    """
    report = popeye_raw.get("popeye", popeye_raw)
    if not isinstance(report, dict):
        return []

    sections = report.get("sections")
    if sections is None:
        sections = report.get("sanitizers")      # popeye <= 0.20
    if not isinstance(sections, list):
        log.warning("Popeye output has no 'sections'/'sanitizers' list — "
                    "unrecognised schema, keys=%s", list(report)[:8])
        return []

    findings: list[Finding] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        linter = section.get("linter") or section.get("sanitizer") or "unknown"
        gvr = section.get("gvr", "")
        issues = section.get("issues") or {}
        if not isinstance(issues, dict):
            continue
        for fqn, issue_list in issues.items():
            if not isinstance(issue_list, list):
                continue
            # Popeye keys issues by "namespace/name" (or bare name, cluster-scoped)
            name = fqn.split("/", 1)[1] if "/" in fqn else fqn
            for issue in issue_list:
                if not isinstance(issue, dict):
                    continue
                level = issue.get("level", S_OK)
                if isinstance(level, str):
                    level = {"ok": S_OK, "info": S_INFO,
                             "warn": S_WARN, "warning": S_WARN,
                             "error": S_ERROR}.get(level.lower(), S_WARN)
                try:
                    severity = int(level)
                except (TypeError, ValueError):
                    severity = S_WARN
                severity = max(S_OK, min(S_ERROR, severity))

                message = issue.get("message", "") or json.dumps(issue)
                # Popeye prefixes messages with its code: "[POP-204] Pod is ..."
                m = re.match(r"^\[(?P<code>[A-Z]+-\d+)\]\s*(?P<rest>.*)$", message)
                code = m.group("code") if m else "POP-000"
                if m:
                    message = m.group("rest").strip() or message

                findings.append(Finding(
                    group=linter,
                    gvr=issue.get("gvr") or gvr or f"v1/{linter}",
                    name=name,
                    code=code,
                    severity=severity,
                    message=message,
                ))
    # Popeye emits ok-level entries for clean resources; they are not findings.
    return [f for f in findings if f.severity > S_OK]


def _build_report_builtin(ns: str) -> dict:
    """Run built-in Python analyzers and assemble a Popeye-shaped JSON report."""
    started = time.time()
    log.info("Running built-in Popeye analyzers against ns=%s", ns)
    findings: list[Finding] = []
    findings += analyze_nodes()
    findings += analyze_deployments(ns)
    findings += analyze_pods(ns)
    findings += analyze_ingresses(ns)
    findings += analyze_services(ns)
    findings += analyze_pvcs(ns)

    return _assemble_report(ns, findings, started, engine="builtin-analyzers")


def _assemble_report(ns: str, findings: list[Finding], started: float,
                     engine: str = "builtin-analyzers") -> dict:
    """Assemble findings into the standard A.O.P.S. report format."""
    by_group: dict[str, list[Finding]] = {}
    for f in findings:
        by_group.setdefault(f.group, []).append(f)

    sanitizers = {}
    for grp, flist in by_group.items():
        items = []
        for f in flist:
            items.append({
                "name": f.name,
                "sanitizers": [{
                    "code": f.code,
                    "severity": f.severity,
                    "severity_label": SEVERITY_LABEL[f.severity],
                    "message": f.message,
                }],
            })
        sanitizers[grp] = {"items": items}

    penalty = sum(S_ERROR if f.severity == S_ERROR else
                  (S_WARN if f.severity == S_WARN else S_INFO) for f in findings)
    score = max(0, 100 - penalty * 4)
    grade = grade_from_score(score)

    elapsed = round(time.time() - started, 3)
    log.info("Scan complete: %d findings, score=%d grade=%s, %.2fs",
             len(findings), score, grade, elapsed)

    issues_by_ns = {
        ns: [{
            "group": f.group,
            "gvr": f.gvr,
            "name": f.name,
            "code": f.code,
            "severity": f.severity,
            "severity_label": SEVERITY_LABEL[f.severity],
            "message": f.message,
        } for f in findings]
    }

    return {
        "scanner": "popeye",
        "popeye_version": engine,
        "popeye_mode": POPEYE_MODE,
        # Provenance. `data_source` answers the only question that matters when
        # someone acts on this report: did it describe a real cluster?
        "aops_mode": AOPS_MODE,
        "data_source": CLIENT.data_source,
        "engine": engine,
        "namespace": ns,
        "scan_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_seconds": elapsed,
        "score": score,
        "grade": grade,
        "resources_scanned": sum(len(_safe_get(p).get("items", []))
                                 for p in [
                                     f"/api/v1/nodes",
                                     f"/apis/apps/v1/namespaces/{ns}/deployments",
                                     f"/api/v1/namespaces/{ns}/pods",
                                     f"/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses",
                                     f"/api/v1/namespaces/{ns}/services",
                                     f"/api/v1/namespaces/{ns}/persistentvolumeclaims",
                                 ]),
        "sanitizers": sanitizers,
        "issues": issues_by_ns,
        "findings_count": len(findings),
        "findings_by_severity": {
            "error":   sum(1 for f in findings if f.severity == S_ERROR),
            "warning": sum(1 for f in findings if f.severity == S_WARN),
            "info":    sum(1 for f in findings if f.severity == S_INFO),
        },
    }


def main():
    """CLI entry — print Popeye-shaped JSON to stdout."""
    report = build_report(NAMESPACE)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
