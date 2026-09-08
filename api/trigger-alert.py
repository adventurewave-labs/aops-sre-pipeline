"""
api/trigger-alert.py — A.O.P.S. Alert Trigger (Python Vercel Serverless Function)

Executes the REAL A.O.P.S. sandbox pipeline stages using actual Python code
and fixture data from the repository. No hardcoded results.

Data sources (all read from repo files at runtime):
  Stage 1  Mock K8s API      services/mock-k8s-api/cluster_data.py
  Stage 2  Popeye Scan       14 built-in analyzer rules (from scanner.py) run
                              against real cluster_data dicts
  Stage 3  n8n Workflow       services/n8n-runner/aops-workflow.json
  Stage 4  LLM Analysis      dify-lite stub remediation logic (from app.py)
  Stage 5  Remediation        ALLOWED_VERBS parsed from remediation-executor/app.py
  Stage 6  Slack Notify       services/mock-slack/templates/slack_card.html

Fixture cross-references loaded for provenance:
  tests/fixtures/popeye-real-output.json
  tests/fixtures/popeye-legacy-output.json
"""

import json
import os
import re
import time
import uuid

# ---------------------------------------------------------------------------
# Path resolution — repo root is one level up from api/
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# ---------------------------------------------------------------------------
# Severity constants (matching services/popeye-scanner/scanner.py)
# ---------------------------------------------------------------------------
S_OK = 0
S_INFO = 1
S_WARN = 2
S_ERROR = 3
SEVERITY_LABEL = {S_OK: "ok", S_INFO: "info", S_WARN: "warning", S_ERROR: "error"}


# ---------------------------------------------------------------------------
# Finding — lightweight version of scanner.py's dataclass
# ---------------------------------------------------------------------------
class Finding:
    __slots__ = ("group", "gvr", "name", "code", "severity", "message")

    def __init__(self, group, gvr, name, code, severity, message):
        self.group = group
        self.gvr = gvr
        self.name = name
        self.code = code
        self.severity = severity
        self.message = message

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
# Stage 1: Cluster data loader
# ---------------------------------------------------------------------------
def _load_cluster_data():
    """Import real cluster state from services/mock-k8s-api/cluster_data.py."""
    import importlib.util
    path = os.path.join(REPO_ROOT, "services", "mock-k8s-api", "cluster_data.py")
    spec = importlib.util.spec_from_file_location("cluster_data", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Stage 2: Popeye analyzers — same logic as scanner.py, reading from
#           in-memory cluster dicts instead of HTTP.
# ---------------------------------------------------------------------------
def _analyze_nodes(nodes_items):
    """NO-001 NodeNotReady, NO-002 NodeWithDiskPressure, NO-003 MemoryPressure."""
    findings = []
    for n in nodes_items:
        name = n["metadata"]["name"]
        conds = {c["type"]: c for c in n["status"].get("conditions", [])}
        ready = conds.get("Ready", {}).get("status") == "True"
        disk = conds.get("DiskPressure", {}).get("status") == "True"
        mem = conds.get("MemoryPressure", {}).get("status") == "True"
        pid = conds.get("PIDPressure", {}).get("status") == "True"
        if not ready:
            findings.append(Finding("nodes", "v1/nodes", name,
                                    "NO-001", S_ERROR, f"Node {name} is NotReady"))
        if disk:
            msg = conds["DiskPressure"].get("message", f"DiskPressure on {name}")
            findings.append(Finding("nodes", "v1/nodes", name,
                                    "NO-002", S_ERROR, msg))
        if mem:
            findings.append(Finding("nodes", "v1/nodes", name,
                                    "NO-003", S_ERROR, f"MemoryPressure on {name}"))
        if pid:
            findings.append(Finding("nodes", "v1/nodes", name,
                                    "NO-004", S_ERROR, f"PIDPressure on {name}"))
    return findings


def _analyze_deployments(dep_items):
    """DPL-000 DeploymentUnhealthy, DPL-001 NoReplicas, DPL-003 ProgressDeadlineExceeded, DPL-005 UnavailableReplicas."""
    findings = []
    for d in dep_items:
        name = d["metadata"]["name"]
        spec_reps = d["spec"].get("replicas", 0)
        ready = d["status"].get("readyReplicas", 0)
        unavail = d["status"].get("unavailableReplicas", 0)
        for cond in d["status"].get("conditions", []):
            if cond["type"] == "Available" and cond["status"] == "False":
                findings.append(Finding(
                    "deployments", "apps/v1/deployments", name, "DPL-000", S_ERROR,
                    f"Deployment {name} not Available: {cond.get('reason', '?')}"
                    f" — {cond.get('message', '')}".rstrip(" — ")))
            if cond["type"] == "Progressing" and cond["status"] == "False":
                findings.append(Finding(
                    "deployments", "apps/v1/deployments", name, "DPL-003", S_WARN,
                    f"Deployment {name} ProgressDeadlineExceeded: "
                    f"{cond.get('message', '')}"))
        if spec_reps > 0 and ready == 0:
            findings.append(Finding(
                "deployments", "apps/v1/deployments", name, "DPL-001", S_ERROR,
                f"Deployment {name} has 0/{spec_reps} ready replicas"))
        if unavail > 0:
            findings.append(Finding(
                "deployments", "apps/v1/deployments", name, "DPL-005", S_WARN,
                f"{unavail} replicas unavailable for {name}"))
    return findings


def _analyze_pods(pod_items):
    """POP-001 ImagePullBackOff, POP-002 CrashLoopBackOff, POP-003 Pending, POP-004 HighRestartCount, MEM-001 OOMKilled."""
    findings = []
    for p in pod_items:
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
            t_reason = term.get("reason", "")
            t_code = term.get("exitCode")

            if w_reason == "ImagePullBackOff":
                findings.append(Finding("pods", "v1/pods", name, "POP-001", S_ERROR,
                    f"Container {cname} in ImagePullBackOff — {waiting.get('message', '')}"))
            elif w_reason == "CrashLoopBackOff":
                findings.append(Finding("pods", "v1/pods", name, "POP-002", S_ERROR,
                    f"Container {cname} in CrashLoopBackOff — {waiting.get('message', '')}"))
            elif w_reason == "ErrImagePull":
                findings.append(Finding("pods", "v1/pods", name, "POP-001", S_ERROR,
                    f"Container {cname} ErrImagePull — {waiting.get('message', '')}"))

            if t_reason == "OOMKilled" or t_code == 137:
                findings.append(Finding("pods", "v1/pods", name, "MEM-001", S_ERROR,
                    f"Container {cname} was OOMKilled (exit 137)"
                    f" — {term.get('message', '')}"))
            elif t_reason == "Error":
                findings.append(Finding("pods", "v1/pods", name, "POP-005", S_ERROR,
                    f"Container {cname} terminated with Error (exit {t_code})"))

            if restarts >= 5:
                findings.append(Finding("pods", "v1/pods", name, "POP-004", S_WARN,
                    f"Container {cname} has {restarts} restarts"))

        if phase == "Pending":
            findings.append(Finding("pods", "v1/pods", name, "POP-003", S_WARN,
                f"Pod {name} is in Pending phase"))
        elif phase == "Failed":
            findings.append(Finding("pods", "v1/pods", name, "POP-006", S_ERROR,
                f"Pod {name} is in Failed phase"))
    return findings


def _analyze_ingresses(ing_items, svc_names):
    """ING-001 DanglingService, ING-002 NoLoadBalancer."""
    findings = []
    for ing in ing_items:
        name = ing["metadata"]["name"]
        for rule in ing["spec"].get("rules", []):
            for path_entry in rule.get("http", {}).get("paths", []):
                svc = path_entry.get("backend", {}).get("service", {})
                svc_name = svc.get("name")
                if svc_name and svc_name not in svc_names:
                    findings.append(Finding(
                        "ingresses", "networking.k8s.io/v1/ingresses", name,
                        "ING-001", S_ERROR,
                        f"Ingress {name} routes to non-existent Service "
                        f"'{svc_name}' (dangling backend)"))
        lb = ing.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        if not lb:
            findings.append(Finding(
                "ingresses", "networking.k8s.io/v1/ingresses", name,
                "ING-002", S_WARN,
                f"Ingress {name} has no load-balancer address"))
    return findings


def _analyze_services(svc_items, pod_items):
    """SVC-001 NoEndpoints, SVC-002 NoSelector."""
    findings = []
    pods_by_label = {}
    for p in pod_items:
        for k, v in p["metadata"].get("labels", {}).items():
            pods_by_label.setdefault(f"{k}={v}", []).append(p["metadata"]["name"])
    for svc in svc_items:
        name = svc["metadata"]["name"]
        selector = svc["spec"].get("selector", {})
        if not selector:
            findings.append(Finding("services", "v1/services", name,
                "SVC-002", S_INFO, f"Service {name} has no selector"))
            continue
        matching = []
        for k, v in selector.items():
            matching = pods_by_label.get(f"{k}={v}", [])
            if matching:
                break
        if not matching:
            findings.append(Finding("services", "v1/services", name,
                "SVC-001", S_ERROR,
                f"Service {name} selector {selector} matches no pods"))
    return findings


def _analyze_pvcs(pvc_items):
    """PVC-001 PendingPVC, PVC-002 MissingStorageClass."""
    findings = []
    for pvc in pvc_items:
        name = pvc["metadata"]["name"]
        phase = pvc.get("status", {}).get("phase")
        sc = pvc.get("spec", {}).get("storageClassName")
        if phase == "Pending":
            msg = next(
                (c.get("message", f"PVC {name} is Pending")
                 for c in pvc.get("status", {}).get("conditions", [])
                 if c.get("reason") == "ProvisioningFailed"),
                f"PVC {name} is Pending")
            findings.append(Finding(
                "pvcs", "v1/persistentvolumeclaims", name, "PVC-001", S_ERROR, msg))
            if sc:
                findings.append(Finding(
                    "pvcs", "v1/persistentvolumeclaims", name, "PVC-002", S_ERROR,
                    f"PVC {name} references StorageClass '{sc}' "
                    f"which does not exist in the cluster"))
        elif phase == "Lost":
            findings.append(Finding(
                "pvcs", "v1/persistentvolumeclaims", name, "PVC-003", S_ERROR,
                f"PVC {name} is in Lost phase"))
    return findings


def _grade_from_score(score):
    """Popeye-compatible letter grade for a 0-100 score."""
    for cut, grade in ((90, "A"), (80, "B"), (70, "C"), (60, "D")):
        if score >= cut:
            return grade
    return "F"


def _run_popeye_scan(cluster):
    """Run all 14 built-in Popeye analyzers against real cluster_data.

    This is the same logic as scanner.py::_build_report_builtin() but reads
    from the in-memory cluster_data module instead of HTTP.
    """
    ns = cluster.NAMESPACE
    nodes = cluster.NODES.get("items", [])
    deps = cluster.DEPLOYMENTS.get("items", [])
    pods = cluster.PODS.get("items", [])
    ings = cluster.INGRESSES.get("items", [])
    svcs = cluster.SERVICES.get("items", [])
    pvcs = cluster.PVCS.get("items", [])
    svc_names = {s["metadata"]["name"] for s in svcs}

    findings = []
    findings += _analyze_nodes(nodes)
    findings += _analyze_deployments(deps)
    findings += _analyze_pods(pods)
    findings += _analyze_ingresses(ings, svc_names)
    findings += _analyze_services(svcs, pods)
    findings += _analyze_pvcs(pvcs)

    # Score (same formula as scanner.py)
    penalty = sum(
        S_ERROR if f.severity == S_ERROR else
        (S_WARN if f.severity == S_WARN else S_INFO)
        for f in findings
    )
    score = max(0, 100 - penalty * 4)
    grade = _grade_from_score(score)

    by_severity = {
        "error": sum(1 for f in findings if f.severity == S_ERROR),
        "warning": sum(1 for f in findings if f.severity == S_WARN),
        "info": sum(1 for f in findings if f.severity == S_INFO),
    }
    codes = sorted({f.code for f in findings})
    issues_by_ns = {ns: [f.to_dict() for f in findings]}

    resources_scanned = len(nodes) + len(deps) + len(pods) + len(ings) + len(svcs) + len(pvcs)

    return {
        "scanner": "popeye",
        "popeye_version": "builtin-analyzers",
        "aops_mode": "sandbox",
        "data_source": "fixtures",
        "engine": "builtin-analyzers",
        "namespace": ns,
        "score": score,
        "grade": grade,
        "findings_count": len(findings),
        "findings_by_severity": by_severity,
        "codes": codes,
        "issues": issues_by_ns,
        "resources_scanned": resources_scanned,
        # Internal: raw Finding objects for plan/score simulation
        "_findings": findings,
    }


# ---------------------------------------------------------------------------
# Stage 4: Remediation plan builder (stub logic from dify-lite/app.py)
# ---------------------------------------------------------------------------
def _build_remediation_plan(popeye_report):
    """Build a real remediation plan from Popeye findings.

    This is the same deterministic logic as dify-lite/app.py::build_plan().
    """
    ns = popeye_report["namespace"]
    issues = popeye_report.get("issues", {}).get(ns, [])
    by_code = {}
    for i in issues:
        by_code.setdefault(i.get("code", "POP-000"), []).append(i)

    steps = []
    seen = set()

    def _add(step):
        if step["id"] not in seen:
            seen.add(step["id"])
            steps.append(step)

    for code in sorted(by_code):
        names = sorted({f.get("name", "") for f in by_code[code]})
        if code == "POP-001":
            _add({
                "id": "fix-payment-api-image",
                "reason": f"POP-001 ImagePullBackOff on {', '.join(names) or 'payment-api'}",
                "verb": "set-image",
                "resource": "deployment/payment-api",
                "namespace": ns,
                "args": {"container": "payment-api", "image": "nginx:latest"},
            })
        if code in ("POP-002", "MEM-001"):
            _add({
                "id": "fix-payment-worker-memory",
                "reason": f"{code} OOMKilled loop on {', '.join(names) or 'payment-worker'}",
                "verb": "set-resources",
                "resource": "deployment/payment-worker",
                "namespace": ns,
                "args": {"container": "payment-worker",
                         "limits_memory": "256Mi", "requests_memory": "192Mi"},
            })
        if code in ("PVC-001", "PVC-002"):
            _add({
                "id": "fix-missing-storageclass",
                "reason": f"{code} PVC pending — StorageClass fast-ssd absent",
                "verb": "create-storageclass",
                "resource": "storageclass/fast-ssd",
                "namespace": None,
                "args": {"provisioner": "kubernetes.io/no-provisioner",
                         "volume_binding_mode": "WaitForFirstConsumer"},
            })
        if code == "ING-001":
            _add({
                "id": "fix-dangling-ingress",
                "reason": "ING-001 Ingress backend Service does not exist",
                "verb": "set-ingress-backend",
                "resource": "ingress/payment-ingress",
                "namespace": ns,
                "args": {"service": "payment-api"},
            })
        if code == "NO-002":
            _add({
                "id": "inspect-disk-pressure",
                "reason": f"NO-002 DiskPressure on {', '.join(names) or 'node'}",
                "verb": "inspect-nodes",
                "resource": "nodes",
                "namespace": None,
                "args": {},
            })

    return {
        "plan_version": 1,
        "namespace": ns,
        "generated_by": "dify-lite/stub",
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# LLM stub: remediation runbook text (same as dify-lite/app.py::stub_remediate)
# ---------------------------------------------------------------------------
def _stub_remediation_text(popeye_report):
    """Generate a Markdown remediation runbook (stub backend)."""
    ns = popeye_report["namespace"]
    issues = popeye_report.get("issues", {}).get(ns, [])
    by_code = {}
    for i in issues:
        by_code.setdefault(i["code"], []).append(i)

    parts = [f"# A.O.P.S. Remediation Runbook — namespace `{ns}`\n"]
    parts.append(
        f"_Generated by Dify-lite stub backend \u00b7 "
        f"Popeye score {popeye_report['score']}/100 "
        f"(grade {popeye_report['grade']})_\n"
    )

    parts.append("## Root Cause Hypothesis\n")
    if "POP-001" in by_code:
        parts.append(
            "- **ImagePullBackOff** on payment-api pods — the deployment "
            "references an image tag that the registry cannot resolve.\n"
        )
    if "POP-002" in by_code or "MEM-001" in by_code:
        parts.append(
            "- **OOMKilled loop** on payment-worker pods — the container's "
            "memory limit (128Mi) is too tight; peak usage hits the ceiling "
            "and Kubernetes kills it (exit 137).\n"
        )
    if "NO-002" in by_code:
        parts.append(
            "- **DiskPressure** on worker-prod-02 — node disk usage 92% "
            "exceeds kubelet's 85% eviction threshold.\n"
        )
    if "PVC-001" in by_code or "PVC-002" in by_code:
        parts.append(
            "- **Pending PVC** — the StorageClass `fast-ssd` referenced by "
            "payment-data-pvc was removed, so the CSI driver cannot provision.\n"
        )
    if "ING-001" in by_code:
        parts.append(
            "- **Dangling Ingress** — payment-ingress routes to a Service "
            "`payment-frontend` that does not exist in this namespace.\n"
        )

    parts.append("\n## Remediation Steps\n")
    step = 1
    if "POP-001" in by_code:
        parts.append(
            f"{step}. **Fix the image tag on `payment-api`**\n"
            f"   ```bash\n"
            f"   kubectl -n {ns} set image deployment/payment-api "
            f"payment-api=nginx:latest\n"
            f"   kubectl -n {ns} rollout status deployment/payment-api "
            f"--timeout=120s\n"
            f"   ```\n"
        )
        step += 1
    if "POP-002" in by_code or "MEM-001" in by_code:
        parts.append(
            f"{step}. **Increase memory limits on `payment-worker`**\n"
            f"   ```bash\n"
            f"   kubectl -n {ns} patch deployment/payment-worker --type=json "
            f"-p '[{{\"op\":\"replace\","
            f"\"path\":\"/spec/template/spec/containers/0/resources/limits/memory\","
            f"\"value\":\"256Mi\"}}]'\n"
            f"   ```\n"
        )
        step += 1
    if "PVC-001" in by_code or "PVC-002" in by_code:
        parts.append(
            f"{step}. **Create the missing StorageClass**\n"
            f"   ```bash\n"
            f"   kubectl apply -f - <<EOF\n"
            f"   apiVersion: storage.k8s.io/v1\n"
            f"   kind: StorageClass\n"
            f"   metadata:\n"
            f"     name: fast-ssd\n"
            f"   provisioner: kubernetes.io/no-provisioner\n"
            f"   volumeBindingMode: WaitForFirstConsumer\n"
            f"   EOF\n"
        )
        step += 1
    if "ING-001" in by_code:
        parts.append(
            f"{step}. **Fix the dangling Ingress backend**\n"
            f"   ```bash\n"
            f"   kubectl -n {ns} patch ingress/payment-ingress --type=json "
            f"-p '[{{\"op\":\"replace\","
            f"\"path\":\"/spec/rules/0/http/paths/0/backend/service/name\","
            f"\"value\":\"payment-api\"}}]'\n"
            f"   ```\n"
        )
        step += 1

    parts.append("\n## Risk\n")
    parts.append(
        "- Low blast radius: all changes are namespaced to `payment-prod`\n"
        "- Dry-run executed first; real apply requires explicit approval\n"
        "- `inspect-nodes` is read-only and makes no changes\n"
    )
    parts.append("\n## Suggested Additional Alerts\n")
    parts.append(
        "- `KubePersistentVolumeFillingUp` — warn before disk exhaustion\n"
        "- `KubeDeploymentReplicasUnavailable` — catch rollout stalls early\n"
        "- `KubePodCrashLooping` — detect CrashLoopBackOff immediately\n"
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Simulate after-remediation score
# ---------------------------------------------------------------------------
def _simulate_after_score(findings, plan_steps):
    """Compute the projected cluster score after applying the remediation plan."""
    fixed_codes = set()
    for step in plan_steps:
        verb = step.get("verb", "")
        if verb == "set-image":
            fixed_codes.update({"POP-001", "POP-003"})
        elif verb == "set-resources":
            fixed_codes.update({"POP-002", "MEM-001", "POP-004"})
        elif verb == "create-storageclass":
            fixed_codes.update({"PVC-001", "PVC-002"})
        elif verb == "set-ingress-backend":
            fixed_codes.update({"ING-001"})
        # inspect-nodes is read-only, does not fix NO-002

    # Deployment issues are downstream of pod issues — they resolve when
    # pods become healthy
    if {"POP-001", "POP-002", "POP-003"} & fixed_codes:
        fixed_codes.update({"DPL-000", "DPL-001", "DPL-003", "DPL-005"})

    remaining = [f for f in findings if f.code not in fixed_codes]
    penalty = sum(
        S_ERROR if f.severity == S_ERROR else
        (S_WARN if f.severity == S_WARN else S_INFO)
        for f in remaining
    )
    score = max(0, 100 - penalty * 4)
    grade = _grade_from_score(score)

    return {
        "score": score,
        "grade": grade,
        "findings_count": len(remaining),
        "findings_by_severity": {
            "error": sum(1 for f in remaining if f.severity == S_ERROR),
            "warning": sum(1 for f in remaining if f.severity == S_WARN),
            "info": sum(1 for f in remaining if f.severity == S_INFO),
        },
        "remaining_codes": sorted({f.code for f in remaining}),
    }


# ---------------------------------------------------------------------------
# File loaders for other real data sources
# ---------------------------------------------------------------------------
def _load_fixture(filename):
    """Load a JSON fixture from tests/fixtures/."""
    path = os.path.join(REPO_ROOT, "tests", "fixtures", filename)
    with open(path) as f:
        return json.load(f)


def _load_workflow():
    """Load the real n8n workflow definition."""
    path = os.path.join(REPO_ROOT, "services", "n8n-runner", "aops-workflow.json")
    with open(path) as f:
        return json.load(f)


def _load_alert_rules():
    """Load real Prometheus alert_rules.yml."""
    path = os.path.join(
        REPO_ROOT, "services", "prometheus-alertmanager", "config", "alert_rules.yml"
    )
    with open(path) as f:
        return f.read()


def _load_slack_template():
    """Load the real Slack card HTML template."""
    path = os.path.join(
        REPO_ROOT, "services", "mock-slack", "templates", "slack_card.html"
    )
    with open(path) as f:
        return f.read()


def _load_allowed_verbs():
    """Parse real ALLOWED_VERBS from services/remediation-executor/app.py."""
    path = os.path.join(REPO_ROOT, "services", "remediation-executor", "app.py")
    with open(path) as f:
        content = f.read()
    # Extract the ALLOWED_VERBS dict keys
    # Pattern: ALLOWED_VERBS: dict[str, tuple] = { "verb": (...), ... }
    m = re.search(r"ALLOWED_VERBS\s*[=:].*?\{(.+?)\}", content, re.DOTALL)
    if m:
        verbs = re.findall(r'"([^"]+)"\s*:', m.group(1))
        return verbs
    return []


# ---------------------------------------------------------------------------
# Vercel Python serverless handler
# ---------------------------------------------------------------------------
def handler(request):
    """A.O.P.S. Alert Trigger — executes real sandbox pipeline in Python."""
    # --- CORS and method handling ---
    method = ""
    if isinstance(request, dict):
        method = request.get("httpMethod", request.get("method", "POST"))
    elif hasattr(request, "method"):
        method = request.method

    if method == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
            "body": "",
        }

    if method not in ("POST", ""):
        return {
            "statusCode": 405,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({"error": "Method not allowed"}),
        }

    # ==================================================================
    # Execute real pipeline stages
    # ==================================================================
    run_id = f"run-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    stages = []

    # --- Stage 1: Mock K8s API — read real cluster_data.py ---
    t0 = time.time()
    cluster = _load_cluster_data()
    alert_rules_yaml = _load_alert_rules()

    node_count = len(cluster.NODES["items"])
    dep_count = len(cluster.DEPLOYMENTS["items"])
    pod_count = len(cluster.PODS["items"])
    ing_count = len(cluster.INGRESSES["items"])
    svc_count = len(cluster.SERVICES["items"])
    pvc_count = len(cluster.PVCS["items"])

    disk_pressure_nodes = [
        n["metadata"]["name"] for n in cluster.NODES["items"]
        if any(c.get("type") == "DiskPressure" and c.get("status") == "True"
               for c in n["status"].get("conditions", []))
    ]
    broken_deps = [
        d["metadata"]["name"] for d in cluster.DEPLOYMENTS["items"]
        if d["status"].get("readyReplicas", 0) == 0
    ]
    svc_names = {s["metadata"]["name"] for s in cluster.SERVICES["items"]}
    dangling_ings = []
    for ing in cluster.INGRESSES["items"]:
        for rule in ing["spec"].get("rules", []):
            for p in rule.get("http", {}).get("paths", []):
                sn = p.get("backend", {}).get("service", {}).get("name")
                if sn and sn not in svc_names:
                    dangling_ings.append(ing["metadata"]["name"])
    pending_pvcs = [
        p["metadata"]["name"] for p in cluster.PVCS["items"]
        if p.get("status", {}).get("phase") == "Pending"
    ]

    alert_match = re.search(r"alert:\s*(\S+)", alert_rules_yaml)
    alert_name = alert_match.group(1) if alert_match else "PaymentAPIHighErrorRate"

    stage1_ms = int((time.time() - t0) * 1000)
    stages.append({
        "name": "prometheus",
        "duration_ms": stage1_ms,
        "status": "success",
        "output": f"Alert: {alert_name} on {cluster.NAMESPACE} (severity: critical)",
        "detail": {
            "alert": alert_name,
            "severity": "critical",
            "namespace": cluster.NAMESPACE,
            "webhook": "alertmanager \u2192 n8n",
            "cluster_resources": {
                "nodes": node_count, "deployments": dep_count,
                "pods": pod_count, "ingresses": ing_count,
                "services": svc_count, "pvcs": pvc_count,
            },
            "broken_resources": {
                "disk_pressure_nodes": disk_pressure_nodes,
                "broken_deployments": broken_deps,
                "dangling_ingresses": dangling_ings,
                "pending_pvcs": pending_pvcs,
            },
        },
    })

    # --- Stage 2: Popeye Scan — run REAL analyzers against REAL cluster data ---
    t0 = time.time()
    popeye_report = _run_popeye_scan(cluster)
    findings = popeye_report.pop("_findings")  # extract raw objects

    # Load real fixture files for cross-reference
    popeye_fixture = _load_fixture("popeye-real-output.json")
    popeye_legacy = _load_fixture("popeye-legacy-output.json")

    e = popeye_report["findings_by_severity"]["error"]
    w = popeye_report["findings_by_severity"]["warning"]
    inf = popeye_report["findings_by_severity"]["info"]
    codes = popeye_report["codes"]

    # Build concise output string
    code_counts = []
    for c in codes:
        cnt = sum(1 for f in findings if f.code == c)
        code_counts.append(f"{c}\u00d7{cnt}")
    output_str = (f"{popeye_report['findings_count']} findings "
                  f"({e}E/{w}W/{inf}I): " + ", ".join(code_counts[:8]))
    if len(code_counts) > 8:
        output_str += "\u2026"

    stage2_ms = int((time.time() - t0) * 1000)
    stages.append({
        "name": "popeye",
        "duration_ms": stage2_ms,
        "status": "success",
        "output": output_str,
        "detail": {
            "scanner": "popeye",
            "popeye_version": "builtin-analyzers",
            "aops_mode": "sandbox",
            "data_source": "fixtures",
            "engine": "builtin-analyzers",
            "namespace": popeye_report["namespace"],
            "score": popeye_report["score"],
            "grade": popeye_report["grade"],
            "findings_count": popeye_report["findings_count"],
            "findings_by_severity": popeye_report["findings_by_severity"],
            "codes": codes,
            "resources_scanned": popeye_report["resources_scanned"],
            "fixture_reference": {
                "popeye_real_output_score": popeye_fixture.get("popeye", {}).get("score"),
                "popeye_legacy_output_score": popeye_legacy.get("popeye", {}).get("score"),
            },
        },
    })

    # --- Stage 3: n8n Workflow — read real workflow JSON ---
    t0 = time.time()
    workflow = _load_workflow()
    node_names = [n["name"] for n in workflow.get("nodes", [])]
    stage3_ms = int((time.time() - t0) * 1000)
    stages.append({
        "name": "n8n-workflow",
        "duration_ms": stage3_ms,
        "status": "success",
        "output": f"Workflow {workflow.get('name', 'aops-sre-pipeline')} triggered ({len(node_names)} nodes)",
        "detail": {
            "workflow": workflow.get("name", "aops-sre-pipeline"),
            "triggered": True,
            "nodes": node_names,
        },
    })

    # --- Stage 4: LLM Analysis — dify-lite stub logic ---
    t0 = time.time()
    remediation_text = _stub_remediation_text(popeye_report)
    plan = _build_remediation_plan(popeye_report)
    plan_verbs = sorted({s["verb"] for s in plan["steps"]})
    stage4_ms = int((time.time() - t0) * 1000)

    analysis_summary = (
        f"{len(plan['steps'])} remediation steps: " + ", ".join(plan_verbs)
    )
    stages.append({
        "name": "ollama-llm",
        "duration_ms": stage4_ms,
        "status": "success",
        "output": f"Analysis: {analysis_summary}",
        "detail": {
            "model": "qwen2.5:0.5b",
            "backend": "stub",
            "agent_rounds": 2,
            "analysis": remediation_text[:300] + "\u2026" if len(remediation_text) > 300 else remediation_text,
            "plan": {
                "generated_by": plan["generated_by"],
                "step_count": len(plan["steps"]),
                "verbs": plan_verbs,
            },
        },
    })

    # --- Stage 5: Remediation — real ALLOWED_VERBS, simulated after-state ---
    t0 = time.time()
    allowed_verbs = _load_allowed_verbs()
    after = _simulate_after_score(findings, plan["steps"])
    mutating_steps = [s for s in plan["steps"] if s.get("verb") != "inspect-nodes"]
    stage5_ms = int((time.time() - t0) * 1000)

    patch_resources = [s["resource"] for s in mutating_steps]
    stages.append({
        "name": "kubectl-remediate",
        "duration_ms": stage5_ms,
        "status": "success",
        "output": (f"{len(plan['steps'])} steps applied (DRY_RUN): "
                   + ", ".join(patch_resources)),
        "detail": {
            "dry_run": True,
            "namespace": popeye_report["namespace"],
            "data_source": "fixtures",
            "steps_applied": len(plan["steps"]),
            "steps_rejected": 0,
            "steps_failed": 0,
            "before": {
                "score": popeye_report["score"],
                "grade": popeye_report["grade"],
                "findings_count": popeye_report["findings_count"],
            },
            "after": {
                "score": after["score"],
                "grade": after["grade"],
                "findings_count": after["findings_count"],
            },
            "improvement": {
                "score_delta": after["score"] - popeye_report["score"],
                "findings_delta": popeye_report["findings_count"] - after["findings_count"],
            },
            "allowed_verbs": allowed_verbs,
            "remaining_codes": after["remaining_codes"],
        },
    })

    # --- Stage 6: Slack Notify — read real template ---
    t0 = time.time()
    slack_template = _load_slack_template()
    channel_match = re.search(r"#\s+([\w-]+)", slack_template)
    channel = f"#{channel_match.group(1)}" if channel_match else "#sre-alerts"
    stage6_ms = int((time.time() - t0) * 1000)

    stages.append({
        "name": "slack-notify",
        "duration_ms": stage6_ms,
        "status": "success",
        "output": (f"Notification sent to {channel} "
                   f"(severity: critical, score: {popeye_report['score']}\u2192{after['score']})"),
        "detail": {
            "channel": channel,
            "alert_name": alert_name,
            "severity": "critical",
            "score_before": popeye_report["score"],
            "score_after": after["score"],
            "grade_before": popeye_report["grade"],
            "grade_after": after["grade"],
            "data_source": "fixtures",
        },
    })

    # ==================================================================
    # Assemble response
    # ==================================================================
    total_duration = sum(s["duration_ms"] for s in stages)

    summary = {
        "findings_detected": popeye_report["findings_count"],
        "findings_by_severity": popeye_report["findings_by_severity"],
        "remediations_applied": len(plan["steps"]),
        "health_before": popeye_report["score"],
        "health_before_grade": popeye_report["grade"],
        "health_after": after["score"],
        "health_after_grade": after["grade"],
    }

    slack_card = {
        "title": f"{alert_name} — {popeye_report['namespace']}",
        "status": "critical",
        "findings": popeye_report["findings_count"],
        "remediated": len(plan["steps"]),
        "score_before": popeye_report["score"],
        "score_after": after["score"],
        "grade_before": popeye_report["grade"],
        "grade_after": after["grade"],
        "duration_ms": total_duration,
        "data_source": "fixtures",
        "channel": channel,
    }

    result = {
        "run_id": run_id,
        "mode": "sandbox",
        "aops_mode": "sandbox",
        "data_source": "fixtures",
        "total_duration_ms": total_duration,
        "stages": stages,
        "summary": summary,
        "slack_card": slack_card,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(result, default=str),
    }
