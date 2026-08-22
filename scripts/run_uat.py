#!/usr/bin/env python3
"""A.O.P.S. UAT (User Acceptance Test) runner.

Runs the full test matrix against a running A.O.P.S. stack and emits a
structured JSON results file that the report generator consumes.

Test matrix (14 tests):
  T01  mock-k8s-api serves the 6 broken resources
  T02  mock-k8s-api enforces Bearer auth
  T03  popeye-scanner emits Popeye-shaped JSON with expected findings
  T04  dify-lite health endpoint reports backend selection
  T05  dify-lite chat completion produces a structured remediation
  T06  mock-slack stores the rendered Slack card
  T07  n8n-runner workflow has 6 nodes in expected order
  T08  end-to-end alert flow completes in <2s
  T09  idempotency: firing twice produces two distinct runs
  T10  resilience: dify-lite survives a malformed message body
  T11  scan reports its data provenance and never mislabels fixtures
  T12  agent emits a structured plan using only allowlisted verbs
  T13  executor never applies real changes from fixture-backed reports
  T14  executor rejects plan steps outside its allowlist

Each test records: name, status (pass/fail), duration_ms, evidence (compact
JSON snapshot), notes.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import socket

BASE = {
    "k8s":     "http://localhost:8001",
    "popeye":  "http://localhost:8004",
    "dify":    "http://localhost:8002",
    "slack":   "http://localhost:8003",
    "n8n":     "http://localhost:5678",
    "remediation": "http://localhost:8005",
}
TOKEN = "aops-demo-token"
RESULTS: list[dict] = []


def http_get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")}
    except Exception as e:
        return 0, {"error": str(e)}


def http_post(url, payload, headers=None, timeout=120):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = (json.dumps(payload).encode()
            if isinstance(payload, (dict, list))
            else (payload.encode() if isinstance(payload, str) else b""))
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")}
    except Exception as e:
        return 0, {"error": str(e)}


def record(test_id, name, status, duration_ms, evidence=None, notes=""):
    RESULTS.append({
        "test_id": test_id,
        "name": name,
        "status": status,
        "duration_ms": duration_ms,
        "evidence": evidence or {},
        "notes": notes,
    })
    sym = "✓" if status == "pass" else "✗"
    print(f"  [{sym}] {test_id}  {name}  ({duration_ms}ms)")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def t01_mock_k8s_resources():
    t0 = time.time()
    st, nodes = http_get(f"{BASE['k8s']}/api/v1/nodes",
                         headers={"Authorization": f"Bearer {TOKEN}"})
    st, deps = http_get(f"{BASE['k8s']}/apis/apps/v1/namespaces/payment-prod/deployments",
                        headers={"Authorization": f"Bearer {TOKEN}"})
    st, ings = http_get(f"{BASE['k8s']}/apis/networking.k8s.io/v1/namespaces/payment-prod/ingresses",
                        headers={"Authorization": f"Bearer {TOKEN}"})
    st, svcs = http_get(f"{BASE['k8s']}/api/v1/namespaces/payment-prod/services",
                        headers={"Authorization": f"Bearer {TOKEN}"})
    st, pvcs = http_get(f"{BASE['k8s']}/api/v1/namespaces/payment-prod/persistentvolumeclaims",
                        headers={"Authorization": f"Bearer {TOKEN}"})
    node_names = [n["metadata"]["name"] for n in nodes["items"]]
    disk_pressure = [n["metadata"]["name"] for n in nodes["items"]
                     if any(c.get("type") == "DiskPressure" and c.get("status") == "True"
                             for c in n["status"].get("conditions", []))]
    dep_broken = [d["metadata"]["name"] for d in deps["items"]
                  if d["status"].get("readyReplicas", 0) == 0]
    ing_backends = [i["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"]
                    for i in ings["items"]]
    svc_names = [s["metadata"]["name"] for s in svcs["items"]]
    pending_pvcs = [(p["metadata"]["name"], p["status"]["phase"])
                    for p in pvcs["items"]]

    expected_nodes = 2
    expected_disk = ["worker-prod-02"]
    expected_broken_deps = ["payment-api", "payment-worker"]
    expected_ing_backend = "payment-frontend"
    expected_pvcs = [("payment-data-pvc", "Pending")]

    passed = (
        len(node_names) == expected_nodes and
        disk_pressure == expected_disk and
        set(dep_broken) == set(expected_broken_deps) and
        ing_backends == [expected_ing_backend] and
        expected_ing_backend not in svc_names and  # the dangling part
        pending_pvcs == expected_pvcs
    )
    record("T01", "mock-k8s-api serves the 6 broken resources",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={
               "nodes": node_names, "disk_pressure_nodes": disk_pressure,
               "broken_deployments": dep_broken,
               "ingress_backends": ing_backends,
               "services": svc_names, "pending_pvcs": pending_pvcs,
               "dangling_service_confirmed": expected_ing_backend not in svc_names,
           },
           notes="6 broken resources: 2 nodes(1 DiskPressure), 2 broken Deployments, 1 dangling Ingress, 1 Pending PVC")


def t02_k8s_auth():
    t0 = time.time()
    st_noauth, _ = http_get(f"{BASE['k8s']}/api/v1/namespaces/payment-prod/pods")
    st_auth, _ = http_get(f"{BASE['k8s']}/api/v1/namespaces/payment-prod/pods",
                          headers={"Authorization": f"Bearer {TOKEN}"})
    passed = st_noauth == 401 and st_auth == 200
    record("T02", "mock-k8s-api enforces Bearer auth",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={"without_auth_status": st_noauth,
                     "with_auth_status": st_auth})


def t03_popeye_findings():
    t0 = time.time()
    st, scan = http_post(f"{BASE['popeye']}/scan?namespace=payment-prod", {})
    expected_codes = {"NO-002", "DPL-000", "DPL-001", "POP-001", "POP-002",
                       "MEM-001", "ING-001", "PVC-001", "PVC-002"}
    found_codes = {i["code"] for i in scan["issues"]["payment-prod"]}
    missing = expected_codes - found_codes
    passed = (st == 200 and scan["scanner"] == "popeye" and
              scan["findings_count"] >= 15 and
              scan["findings_by_severity"]["error"] >= 8 and
              not missing)
    record("T03", "popeye-scanner emits Popeye-shaped JSON with expected findings",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={
               "status": st, "score": scan["score"], "grade": scan["grade"],
               "findings_count": scan["findings_count"],
               "by_severity": scan["findings_by_severity"],
               "expected_codes": sorted(expected_codes),
               "found_codes": sorted(found_codes),
               "missing_codes": sorted(missing),
           })


def t04_dify_health():
    t0 = time.time()
    st, health = http_get(f"{BASE['dify']}/healthz")
    passed = (st == 200 and health["backend_active"] in ("stub", "ollama"))
    record("T04", "dify-lite health endpoint reports backend selection",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence=health)


def t05_dify_remediation_structure():
    t0 = time.time()
    _, scan = http_post(f"{BASE['popeye']}/scan?namespace=payment-prod", {})
    payload = {
        "model": "qwen2.5:0.5b",
        "messages": [
            {"role": "system", "content": "You are AURA-SRE."},
            {"role": "user", "content": json.dumps(scan)},
        ],
    }
    st, completion = http_post(f"{BASE['dify']}/v1/chat/completions", payload)
    text = completion.get("choices", [{}])[0].get("message", {}).get("content", "")
    must_have_sections = [
        "Root Cause",
        "Remediation Steps",
        "Risk",
        "Suggested Additional Alerts",
    ]
    present = [s for s in must_have_sections if s in text]
    kubectl_count = text.count("kubectl")
    trace = completion.get("_dify_lite_trace", {})
    passed = (st == 200 and len(present) == len(must_have_sections)
              and kubectl_count >= 4
              and trace.get("backend") in ("stub", f"ollama:qwen2.5:0.5b"))
    record("T05", "dify-lite chat completion produces a structured remediation",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={
               "status": st,
               "backend": trace.get("backend"),
               "completion_chars": len(text),
               "sections_present": present,
               "sections_missing": [s for s in must_have_sections if s not in text],
               "kubectl_command_count": kubectl_count,
               "agent_rounds": len(trace.get("rounds", [])),
               "agent_duration_s": completion.get("_dify_lite_duration_s"),
           })


def t06_mock_slack_storage():
    # Fire an alert first to ensure we have something stored
    http_post(f"{BASE['n8n']}/webhook/aops-alert",
              {"alertname": "PaymentAPIHighErrorRate",
               "namespace": "payment-prod", "severity": "critical"})
    t0 = time.time()
    st, alerts = http_get(f"{BASE['slack']}/alerts.json")
    passed = (st == 200 and len(alerts["alerts"]) >= 1 and
              "remediation_html" in alerts["alerts"][-1] and
              "alert_name" in alerts["alerts"][-1])
    last = alerts["alerts"][-1] if alerts["alerts"] else {}
    record("T06", "mock-slack stores the rendered Slack card",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={
               "total_alerts": len(alerts["alerts"]),
               "last_alert": {
                   "ts": last.get("ts"),
                   "alert_name": last.get("alert_name"),
                   "namespace": last.get("namespace"),
                   "score": last.get("score"),
                   "grade": last.get("grade"),
                   "remediation_html_chars": len(last.get("remediation_html", "")),
                   "text_chars": len(last.get("text", "")),
               },
           })


def t07_workflow_shape():
    t0 = time.time()
    st, wf = http_get(f"{BASE['n8n']}/workflow")
    node_names = [n["name"] for n in wf["nodes"]]
    expected_order = ["Alertmanager Webhook", "Popeye Scan",
                       "Dify Agent Reasoning", "Post to Slack",
                       "Execute Remediation", "Respond to Alertmanager"]
    edges = wf.get("connections", {})
    chain_ok = (
        edges.get("Alertmanager Webhook", {}).get("main", [[{}]])[0][0].get("node") == "Popeye Scan"
        and edges.get("Popeye Scan", {}).get("main", [[{}]])[0][0].get("node") == "Dify Agent Reasoning"
        and {e["node"]
             for lst in edges.get("Dify Agent Reasoning", {}).get("main", [])
             for e in lst} == {"Post to Slack", "Execute Remediation"}
    )
    passed = (st == 200 and node_names == expected_order and chain_ok)
    record("T07", "n8n-runner workflow has 6 nodes in expected order",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={
               "nodes": node_names,
               "edge_chain_verified": chain_ok,
               "workflow_name": wf.get("name"),
           })


def t08_end_to_end_latency():
    t0 = time.time()
    st, result = http_post(f"{BASE['n8n']}/webhook/aops-alert",
                            {"alertname": "PaymentAPIHighErrorRate",
                             "namespace": "payment-prod", "severity": "critical"})
    duration_s = result.get("duration_s")
    trace = result.get("trace", [])
    all_200 = all(t["status"] == 200 for t in trace)
    passed = (st == 200 and duration_s is not None and
              duration_s < 2.0 and all_200)
    record("T08", "end-to-end alert flow completes in <2s",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={
               "status": st,
               "total_duration_s": duration_s,
               "node_durations_s": {t["node"]: t["duration_s"] for t in trace},
               "all_nodes_200": all_200,
               "run_id": result.get("run_id"),
           })


def t09_idempotency():
    # Fire 3 alerts, verify each gets a unique run_id
    run_ids = []
    for _ in range(3):
        st, r = http_post(f"{BASE['n8n']}/webhook/aops-alert",
                          {"alertname": "PaymentAPIHighErrorRate"})
        run_ids.append(r.get("run_id"))
    t0 = time.time()
    unique = len(set(run_ids))
    st, alerts = http_get(f"{BASE['slack']}/alerts.json")
    passed = (unique == 3 and len(alerts["alerts"]) >= 3)
    record("T09", "idempotency: firing N times produces N distinct runs",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={
               "run_ids": run_ids,
               "unique_run_ids": unique,
               "slack_alerts_total": len(alerts["alerts"]),
           })


def t10_dify_resilience():
    t0 = time.time()
    # Send a malformed payload: messages as a string instead of list
    bad_payloads = [
        {"messages": "not a list"},
        {"messages": [{"role": "user", "content": "abc"}]},
        {"foo": "bar"},
    ]
    statuses = []
    for p in bad_payloads:
        st, _ = http_post(f"{BASE['dify']}/v1/chat/completions", p)
        statuses.append(st)
    # All should return 400 or 200 gracefully, never 500
    no_500 = all(s != 500 for s in statuses)
    record("T10", "resilience: dify-lite survives malformed message body",
           "pass" if no_500 else "fail",
           int((time.time() - t0) * 1000),
           evidence={"payloads": bad_payloads, "statuses": statuses,
                     "no_500s": no_500})


def t11_scan_provenance():
    """Every report must state where its data came from."""
    t0 = time.time()
    st, scan = http_post(f"{BASE['popeye']}/scan?namespace=payment-prod", None)
    st_h, health = http_get(f"{BASE['popeye']}/healthz")
    required = ("data_source", "engine", "aops_mode")
    has_all = all(k in scan for k in required)
    # In sandbox the data is fixtures and must say so — never "live-cluster".
    labelled_honestly = (scan.get("data_source") == "fixtures"
                         and scan.get("aops_mode") == "sandbox")
    passed = (st == 200 and has_all and labelled_honestly
              and health.get("data_source") == "fixtures")
    record("T11", "scan reports its data provenance and never mislabels fixtures",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={"data_source": scan.get("data_source"),
                     "engine": scan.get("engine"),
                     "aops_mode": scan.get("aops_mode"),
                     "healthz_data_source": health.get("data_source"),
                     "has_all_provenance_fields": has_all})


def t12_remediation_plan_is_structured_and_allowlisted():
    """dify-lite emits an executable plan; the executor advertises its allowlist."""
    t0 = time.time()
    st, resp = http_post(f"{BASE['dify']}/v1/chat/completions", {
        "messages": [{"role": "user", "content": json.dumps(_scan_report())}]})
    plan = (resp or {}).get("_dify_lite_plan") or {}
    steps = plan.get("steps", [])
    st_h, health = http_get(f"{BASE['remediation']}/healthz")
    allowed = set(health.get("allowed_verbs", []))
    verbs = {s.get("verb") for s in steps}
    passed = (st == 200 and bool(steps) and bool(allowed)
              and verbs.issubset(allowed)
              and all(s.get("id") and s.get("resource") for s in steps))
    record("T12", "agent emits a structured plan using only allowlisted verbs",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={"step_count": len(steps), "verbs": sorted(v for v in verbs if v),
                     "allowed_verbs": sorted(allowed),
                     "generated_by": plan.get("generated_by")})


def t13_executor_refuses_to_mutate_on_fixture_data():
    """The D2 guarantee, end to end: no real changes from fixture-backed reports."""
    t0 = time.time()
    st, health = http_get(f"{BASE['remediation']}/healthz")
    dry = health.get("dry_run")
    st_r, result = http_post(f"{BASE['remediation']}/remediate", {})
    status = (result or {}).get("status")
    # Sandbox ships dry_run=1, so the run completes but mutates nothing. With
    # dry_run=0 against fixtures the executor must refuse outright.
    ok_dry = (dry is True and status == "completed"
              and result.get("data_source") == "fixtures"
              and result.get("steps_rejected", 0) == 0)
    ok_refuse = (dry is False and status == "refused")
    passed = st_r == 200 and (ok_dry or ok_refuse)
    record("T13", "executor never applies real changes from fixture-backed reports",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={"dry_run": dry, "status": status,
                     "data_source": (result or {}).get("data_source"),
                     "steps_applied": (result or {}).get("steps_applied"),
                     "steps_rejected": (result or {}).get("steps_rejected")})


def t14_executor_rejects_steps_outside_its_allowlist():
    """A hostile or malformed plan must be rejected, not executed."""
    t0 = time.time()
    hostile = {"plan": {"plan_version": 1, "namespace": "payment-prod",
                        "generated_by": "uat/hostile", "steps": [
        {"id": "wipe-kube-system", "verb": "delete-namespace",
         "resource": "namespace/kube-system", "namespace": "kube-system"},
        {"id": "cross-ns", "verb": "set-image", "resource": "deployment/x",
         "namespace": "kube-system", "args": {"image": "evil:latest"}},
        {"id": "shell-inject", "verb": "set-image",
         "resource": "deployment/a;rm -rf /", "namespace": "payment-prod",
         "args": {"image": "x"}},
    ]}}
    st, result = http_post(f"{BASE['remediation']}/remediate", hostile)
    steps = (result or {}).get("steps", [])
    none_applied = all(s.get("status") != "applied" for s in steps)
    passed = (st == 200 and len(steps) == 3 and none_applied
              and (result or {}).get("steps_applied", 1) == 0)
    record("T14", "executor rejects plan steps outside its allowlist",
           "pass" if passed else "fail",
           int((time.time() - t0) * 1000),
           evidence={"outcomes": [{s.get("step"): s.get("status")} for s in steps],
                     "steps_applied": (result or {}).get("steps_applied"),
                     "steps_rejected": (result or {}).get("steps_rejected")})


def _scan_report() -> dict:
    st, scan = http_post(f"{BASE['popeye']}/scan?namespace=payment-prod", None)
    return scan


def main():
    print("=== A.O.P.S. UAT test matrix ===\n")
    t01_mock_k8s_resources()
    t02_k8s_auth()
    t03_popeye_findings()
    t04_dify_health()
    t05_dify_remediation_structure()
    t06_mock_slack_storage()
    t07_workflow_shape()
    t08_end_to_end_latency()
    t09_idempotency()
    t10_dify_resilience()
    t11_scan_provenance()
    t12_remediation_plan_is_structured_and_allowlisted()
    t13_executor_refuses_to_mutate_on_fixture_data()
    t14_executor_rejects_steps_outside_its_allowlist()

    passed = sum(1 for r in RESULTS if r["status"] == "pass")
    failed = len(RESULTS) - passed
    print(f"\n=== Summary: {passed}/{len(RESULTS)} passed, {failed} failed ===")

    out = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": len(RESULTS),
        "passed": passed,
        "failed": failed,
        "results": RESULTS,
    }
    out_path = os.environ.get(
        "AOPS_UAT_OUT",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "var", "uat-results.json"))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nResults saved to {out_path}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
