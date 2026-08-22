#!/usr/bin/env python3
"""Unit tests for the real-Popeye JSON parser and the KubeClient routing.

The fixtures in tests/fixtures/ follow the schema emitted by
derailed/popeye v0.22.1 (internal/report/builder.go -> Report/Section/Issue),
including the pre-0.21 `sanitizers` spelling. Before this suite existed the
parser assumed `sections` was a dict and silently returned zero findings for
every valid input.

Run: python3 tests/test_popeye_parser.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "services", "popeye-scanner"))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

import scanner  # noqa: E402


def load(name: str) -> dict:
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class TestRealPopeyeParser(unittest.TestCase):

    def setUp(self):
        self.findings = scanner._parse_real_popeye(load("popeye-real-output.json"))

    def test_parses_findings_from_real_schema(self):
        # sections is a LIST; issues maps FQN -> LIST. Regression guard for the
        # original parser, which returned [] for all valid Popeye output.
        self.assertTrue(self.findings, "parser returned no findings for valid Popeye JSON")

    def test_drops_ok_level_entries(self):
        self.assertTrue(all(f.severity > scanner.S_OK for f in self.findings))
        self.assertNotIn("payment-api-7d8f6c5b9x-ok999",
                         [f.name for f in self.findings])

    def test_counts(self):
        # 1 node warn + 2 pod errors + 1 pod info + 1 pvc error = 5
        self.assertEqual(len(self.findings), 5)
        self.assertEqual(sum(1 for f in self.findings if f.severity == scanner.S_ERROR), 3)
        self.assertEqual(sum(1 for f in self.findings if f.severity == scanner.S_WARN), 1)
        self.assertEqual(sum(1 for f in self.findings if f.severity == scanner.S_INFO), 1)

    def test_extracts_popeye_code_from_message(self):
        codes = {f.code for f in self.findings}
        self.assertIn("POP-204", codes)
        self.assertIn("POP-401", codes)
        self.assertIn("POP-1002", codes)

    def test_strips_code_prefix_from_message(self):
        f = next(f for f in self.findings if f.code == "POP-204")
        self.assertFalse(f.message.startswith("["))
        self.assertIn("ImagePullBackOff", f.message)

    def test_strips_namespace_from_fqn(self):
        names = {f.name for f in self.findings}
        self.assertIn("payment-api-7d8f6c5b9x-abc12", names)
        self.assertTrue(all("/" not in n for n in names))

    def test_cluster_scoped_names_kept_whole(self):
        node = next(f for f in self.findings if f.group == "nodes")
        self.assertEqual(node.name, "aops-demo-worker2")

    def test_legacy_sanitizers_key_and_string_levels(self):
        findings = scanner._parse_real_popeye(load("popeye-legacy-output.json"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, scanner.S_WARN)
        self.assertEqual(findings[0].code, "POP-206")

    def test_unrecognised_schema_returns_empty_not_crash(self):
        for junk in ({}, {"popeye": {}}, {"popeye": {"sections": "nope"}},
                     {"popeye": {"sections": [{"issues": {"a": "nope"}}]}}):
            self.assertEqual(scanner._parse_real_popeye(junk), [])

    def test_report_assembles_from_parsed_findings(self):
        report = scanner._assemble_report("payment-prod", self.findings, 0.0,
                                          engine="popeye-binary")
        self.assertEqual(report["engine"], "popeye-binary")
        self.assertEqual(report["findings_count"], 5)
        self.assertIn(report["grade"], list("ABCDF"))


class TestGradeFromScore(unittest.TestCase):
    def test_boundaries(self):
        for score, grade in ((100, "A"), (90, "A"), (89, "B"), (80, "B"),
                             (79, "C"), (70, "C"), (69, "D"), (60, "D"),
                             (59, "F"), (0, "F")):
            self.assertEqual(scanner.grade_from_score(score), grade, score)


class TestKubectlRouting(unittest.TestCase):
    """The real backend must translate every API path the analyzers use."""

    ANALYZER_PATHS = [
        "/api/v1/nodes",
        "/apis/apps/v1/namespaces/payment-prod/deployments",
        "/api/v1/namespaces/payment-prod/pods",
        "/apis/networking.k8s.io/v1/namespaces/payment-prod/ingresses",
        "/api/v1/namespaces/payment-prod/services",
        "/api/v1/namespaces/payment-prod/persistentvolumeclaims",
    ]

    def test_every_analyzer_path_has_a_kubectl_route(self):
        client = scanner.KubectlClient()
        calls = []
        client._run = lambda args: (calls.append(args), "{}")[1]
        for path in self.ANALYZER_PATHS:
            client.get(path)
        self.assertEqual(len(calls), len(self.ANALYZER_PATHS))
        self.assertIn(["get", "nodes", "-o", "json"], calls)
        self.assertIn(["get", "pods", "-o", "json", "-n", "payment-prod"], calls)

    def test_unknown_path_raises_rather_than_returning_empty(self):
        client = scanner.KubectlClient()
        client._run = lambda args: "{}"
        with self.assertRaises(scanner.ClusterUnreachable):
            client.get("/apis/batch/v1/namespaces/x/jobs")

    def test_real_backend_never_silently_returns_empty_items(self):
        # The whole point of D2: a broken cluster must raise, not look healthy.
        client = scanner.KubectlClient()
        def boom(args):
            raise scanner.ClusterUnreachable("connection refused")
        client._run = boom
        with self.assertRaises(scanner.ClusterUnreachable):
            client.get("/api/v1/nodes")
        self.assertFalse(client.probe()[0])

    def test_mode_selects_backend(self):
        self.assertIsInstance(scanner.make_client("real"), scanner.KubectlClient)
        self.assertIsInstance(scanner.make_client("sandbox"), scanner.MockHTTPClient)
        self.assertEqual(scanner.make_client("real").data_source, "live-cluster")
        self.assertEqual(scanner.make_client("sandbox").data_source, "fixtures")


if __name__ == "__main__":
    unittest.main(verbosity=2)
