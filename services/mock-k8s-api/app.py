#!/usr/bin/env python3
"""
Mock Kubernetes API server for the A.O.P.S. demo.

Serves the canonical Kubernetes REST API surface for the `payment-prod`
namespace on a 2-node cluster with 6 deliberately-broken resources:

  - 2 broken Deployments (payment-api ImagePullBackOff, payment-worker CrashLoopBackOff)
  - 1 dangling Ingress    (references non-existent 'payment-frontend' Service)
  - 1 Pending PVC          (storageClassName 'fast-ssd' no longer exists)
  - 1 DiskPressure Node    (worker-prod-02 at 92% disk usage)

Endpoints (subset of /api/v1 and /apis/*, the parts Popeye + n8n need):
  GET /api/v1/namespaces/{ns}/pods
  GET /api/v1/namespaces/{ns}/deployments  (alias; apps/v1 below is canonical)
  GET /apis/apps/v1/namespaces/{ns}/deployments
  GET /apis/networking.k8s.io/v1/namespaces/{ns}/ingresses
  GET /api/v1/namespaces/{ns}/services
  GET /api/v1/namespaces/{ns}/persistentvolumeclaims
  GET /api/v1/nodes
  GET /api/v1/namespaces/{ns}/events
  GET /healthz
  GET /version
  GET /openapi/v1   (tiny stub)

Auth: Bearer token "aops-demo-token" expected in Authorization header (matches
kubectl proxy default 'system:anonymous'-style read-only expectations).
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Make cluster_data importable
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cluster_data as cd  # noqa: E402

BIND_HOST = os.environ.get("MOCK_K8S_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("MOCK_K8S_PORT", "8001"))
AUTH_TOKEN = os.environ.get("MOCK_K8S_TOKEN", "aops-demo-token")
DISABLE_AUTH = os.environ.get("MOCK_K8S_DISABLE_AUTH", "0") == "1"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [mock-k8s] %(levelname)s %(message)s")
log = logging.getLogger("mock-k8s")


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, indent=2).encode("utf-8")


# Map URL prefix → cluster_data table
ROUTES = {
    # Core /api/v1
    r"^/api/v1/nodes/?$": ("nodes", cd.NODES),
    r"^/api/v1/namespaces/[^/]+/pods/?$": ("pods", cd.PODS),
    r"^/api/v1/namespaces/[^/]+/services/?$": ("services", cd.SERVICES),
    r"^/api/v1/namespaces/[^/]+/persistentvolumeclaims/?$": ("pvcs", cd.PVCS),
    r"^/api/v1/namespaces/[^/]+/events/?$": ("events", cd.EVENTS),
    # apps/v1
    r"^/apis/apps/v1/namespaces/[^/]+/deployments/?$": ("deployments", cd.DEPLOYMENTS),
    # networking.k8s.io/v1
    r"^/apis/networking\.k8s\.io/v1/namespaces/[^/]+/ingresses/?$":
        ("ingresses", cd.INGRESSES),
}


class K8sHandler(BaseHTTPRequestHandler):
    server_version = "AOPSMockK8s/0.1"

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Kubernetes-Pf-Flowschema-Id", "mock-aops")
        self.send_header("X-Kubernetes-Pf-Priority-Level", "mock-low")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code: int, obj):
        self._send(code, _json_bytes(obj))

    def _check_auth(self) -> bool:
        if DISABLE_AUTH:
            return True
        tok = self.headers.get("Authorization", "")
        if tok == f"Bearer {AUTH_TOKEN}":
            return True
        return False

    def _log_hit(self, kind: str, body_len: int):
        log.info("%s %s -> 200 (%d bytes)", self.command, self.path, body_len)

    # ---- routes
    def do_GET(self):
        if self.path in ("/healthz", "/livez", "/readyz"):
            return self._send(200, b"ok", "text/plain")
        if self.path == "/version":
            return self._send_json(200, {
                "major": "1", "minor": "29",
                "gitVersion": "v1.29.4",
                "gitCommit": "abcdef1234567890",
                "gitTreeState": "clean",
                "buildDate": "2026-07-10T08:00:00Z",
                "goVersion": "go1.21.5",
                "compiler": "gc",
                "platform": "linux/amd64",
            })
        if self.path == "/openapi/v1":
            # Tiny stub - many scanners probe this first.
            return self._send_json(200, {
                "swagger": "2.0",
                "info": {"title": "Kubernetes", "version": "v1.29.4"},
                "paths": {p: {"get": {}} for p in [
                    "/api/v1/nodes",
                    "/api/v1/namespaces/{namespace}/pods",
                ]},
            })
        if self.path.startswith("/api/v1/namespaces"):
            if not self._check_auth():
                return self._send_json(401, {
                    "kind": "Status", "status": "Failure",
                    "message": "Unauthorized",
                    "reason": "Unauthorized", "code": 401,
                })
        for pattern, (kind, payload) in ROUTES.items():
            import re
            if re.match(pattern, self.path):
                body = _json_bytes(payload)
                self._log_hit(kind, len(body))
                return self._send(200, body)
        return self._send_json(404, {
            "kind": "Status", "status": "Failure",
            "message": f"the server could not find the requested resource: {self.path}",
            "reason": "NotFound", "code": 404,
        })

    def do_HEAD(self):
        return self.do_GET()

    def log_message(self, fmt, *args):
        # Quiet down default stderr noise; routed through `log` above instead.
        pass


def main():
    log.info("Mock K8s API starting on %s:%d (auth=%s)",
             BIND_HOST, BIND_PORT, "disabled" if DISABLE_AUTH else "enabled")
    log.info("Cluster state: %d nodes, %d deployments, %d pods, %d ingresses, %d services, %d pvcs, %d events",
             len(cd.NODES["items"]), len(cd.DEPLOYMENTS["items"]),
             len(cd.PODS["items"]), len(cd.INGRESSES["items"]),
             len(cd.SERVICES["items"]), len(cd.PVCS["items"]),
             len(cd.EVENTS["items"]))
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), K8sHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
