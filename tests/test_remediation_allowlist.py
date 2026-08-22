#!/usr/bin/env python3
"""Tests for the remediation allowlist — the boundary that makes plan-driven
(and therefore LLM-influenced) remediation safe.

Before this existed, execute_remediation(plan) ignored `plan` entirely and ran
a hardcoded list of fixes. Now the plan drives execution, so the allowlist is
what stops a malformed or hostile plan from reaching kubectl.

Run: python3 tests/test_remediation_allowlist.py
"""
from __future__ import annotations

import os
import sys
import unittest

import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DRY_RUN", "1")


def _load(module_name: str, service: str):
    """Both services are named app.py — load each by path, not by sys.path."""
    path = os.path.join(ROOT, "services", service, "app.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


executor = _load("aops_executor", "remediation-executor")
dify = _load("aops_dify", "dify-lite")

NS = "payment-prod"


class TestValidateStep(unittest.TestCase):

    def ok(self, **kw):
        step = {"id": "s", "verb": "set-image", "resource": "deployment/payment-api",
                "namespace": NS, "args": {"image": "nginx:latest"}}
        step.update(kw)
        return executor.validate_step(step, NS)

    def test_wellformed_step_passes(self):
        self.assertTrue(self.ok()[0])

    def test_unknown_verb_rejected(self):
        for verb in ("delete-namespace", "exec", "apply-anything", "", None, 42):
            passed, why = self.ok(verb=verb)
            self.assertFalse(passed, f"verb {verb!r} should be rejected")
            self.assertIn("allowlist", why)

    def test_cross_namespace_step_rejected(self):
        passed, why = self.ok(namespace="kube-system")
        self.assertFalse(passed)
        self.assertIn("kube-system", why)

    def test_missing_resource_rejected(self):
        self.assertFalse(self.ok(resource=None)[0])
        self.assertFalse(self.ok(resource="")[0])

    def test_non_object_step_rejected(self):
        for junk in (None, "delete everything", 7, ["a"]):
            self.assertFalse(executor.validate_step(junk, NS)[0])

    def test_cluster_scoped_step_allowed_with_null_namespace(self):
        passed, _ = self.ok(verb="create-storageclass",
                            resource="storageclass/fast-ssd", namespace=None)
        self.assertTrue(passed)


class TestResourceChecking(unittest.TestCase):

    def test_kind_must_match_handler(self):
        with self.assertRaises(ValueError):
            executor._check_resource("secret/db-creds", ("deployment",))
        with self.assertRaises(ValueError):
            executor._check_resource("deployment/x", ("ingress",))

    def test_injection_shapes_rejected(self):
        for bad in ("deployment/a;rm -rf /", "deployment/../../etc/passwd",
                    "deployment/A_B", "deployment/", "--kubeconfig=/tmp/x",
                    "deployment/x y"):
            with self.assertRaises(ValueError, msg=bad):
                executor._check_resource(bad, ("deployment",))

    def test_valid_names_accepted(self):
        self.assertEqual(
            executor._check_resource("deployment/payment-api", ("deployment",)),
            "payment-api")


class TestExecutePlan(unittest.TestCase):

    def setUp(self):
        self.ran = []
        self._orig = executor._kubectl
        executor._kubectl = lambda args, input_data=None: (
            self.ran.append(args) or {"ok": True, "stdout": "ok",
                                      "stderr": "", "returncode": 0})

    def tearDown(self):
        executor._kubectl = self._orig

    def test_rejected_steps_never_reach_kubectl(self):
        plan = {"namespace": NS, "steps": [
            {"id": "evil", "verb": "delete-namespace", "resource": "namespace/kube-system"},
            {"id": "evil2", "verb": "set-image", "resource": "deployment/x",
             "namespace": "kube-system", "args": {"image": "x"}},
        ]}
        results = executor.execute_plan(plan, NS)
        self.assertEqual([r["status"] for r in results], ["rejected", "rejected"])
        self.assertEqual(self.ran, [], "a rejected step reached kubectl")

    def test_allowed_step_runs(self):
        plan = {"namespace": NS, "steps": [
            {"id": "fix", "verb": "set-image", "resource": "deployment/payment-api",
             "namespace": NS, "args": {"container": "payment-api", "image": "nginx:latest"}},
        ]}
        results = executor.execute_plan(plan, NS)
        self.assertEqual(results[0]["status"], "applied")
        self.assertIn(["set", "image", "deployment/payment-api",
                       "payment-api=nginx:latest", "-n", NS], self.ran)

    def test_handler_exception_is_contained(self):
        plan = {"namespace": NS, "steps": [
            {"id": "noimage", "verb": "set-image",
             "resource": "deployment/payment-api", "namespace": NS, "args": {}},
        ]}
        results = executor.execute_plan(plan, NS)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(self.ran, [])

    def test_every_allowlisted_verb_has_a_callable_handler(self):
        for verb, (handler, mutating) in executor.ALLOWED_VERBS.items():
            self.assertTrue(callable(handler), verb)
            self.assertIsInstance(mutating, bool)


class TestPlanGeneration(unittest.TestCase):
    """dify-lite's plans must only ever contain allowlisted verbs."""

    def setUp(self):
        self.dify = dify

    def test_generated_plan_is_fully_executable(self):
        report = {
            "namespace": NS,
            "data_source": "live-cluster",
            "engine": "builtin-analyzers",
            "score": 20, "grade": "F",
            "issues": {NS: [
                {"code": "POP-001", "name": "payment-api-abc", "severity": 3, "message": ""},
                {"code": "POP-002", "name": "payment-worker-xyz", "severity": 3, "message": ""},
                {"code": "PVC-001", "name": "payment-data-pvc", "severity": 2, "message": ""},
                {"code": "ING-001", "name": "payment-ingress", "severity": 2, "message": ""},
                {"code": "NO-002", "name": "worker-prod-02", "severity": 2, "message": ""},
            ]},
        }
        plan = self.dify.build_plan(report, backend="stub")
        self.assertEqual(plan["namespace"], NS)
        self.assertTrue(plan["steps"])
        for step in plan["steps"]:
            passed, why = executor.validate_step(step, NS)
            self.assertTrue(passed, f"{step['id']}: {why}")

    def test_no_findings_yields_empty_plan(self):
        plan = self.dify.build_plan({"namespace": NS, "issues": {NS: []}})
        self.assertEqual(plan["steps"], [])

    def test_plan_carries_provenance(self):
        plan = self.dify.build_plan(
            {"namespace": NS, "data_source": "fixtures", "issues": {NS: []}})
        self.assertEqual(plan["source"]["data_source"], "fixtures")


if __name__ == "__main__":
    unittest.main(verbosity=2)
