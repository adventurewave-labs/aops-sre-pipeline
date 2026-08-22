#!/usr/bin/env python3
"""
HTTP wrapper around the Popeye scanner.

Exposes a tiny REST surface so n8n can trigger a scan over HTTP:

  GET  /healthz            -> liveness probe
  POST /scan?namespace=X   -> runs all analyzers, returns Popeye-shaped JSON
  GET  /scan?namespace=X   -> same (idempotent, safe)

Mirrors the way Robusta and K8sGPT expose scanner invocations to external
orchestrators.
"""

from __future__ import annotations

import os
import sys
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scanner  # noqa: E402

BIND_HOST = os.environ.get("POPEYE_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("POPEYE_PORT", "8004"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [popeye-api] %(levelname)s %(message)s")
log = logging.getLogger("popeye-api")


class PopeyeHandler(BaseHTTPRequestHandler):
    server_version = "AOPSPopeye/0.1"

    def _send(self, code, obj, ctype="application/json"):
        body = json.dumps(obj, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _scan(self, ns: str):
        try:
            return self._send(200, scanner.build_report(ns))
        except scanner.ClusterUnreachable as e:
            # 503, never 200-with-fixtures. A caller acting on this report
            # must be able to tell "cluster is broken" from "cluster is fine".
            log.error("scan refused: %s", e)
            return self._send(503, {
                "status": "cluster_unreachable",
                "message": str(e),
                "aops_mode": scanner.AOPS_MODE,
                "hint": "Point KUBECONFIG at a reachable cluster, or run with "
                        "AOPS_MODE=sandbox to scan the bundled fixtures.",
            })
        except Exception as e:
            log.exception("scan failed")
            return self._send(500, {"status": "error", "message": str(e)})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/healthz", "/livez", "/readyz"):
            reachable, detail = scanner.CLIENT.probe()
            return self._send(200, {
                "status": "ok",
                "scanner": "popeye",
                "aops_mode": scanner.AOPS_MODE,
                "popeye_mode": scanner.POPEYE_MODE,
                "popeye_binary": scanner.POPEYE_BIN or None,
                "data_source": scanner.CLIENT.data_source,
                "cluster_reachable": reachable,
                "cluster_detail": detail,
            })
        if u.path == "/scan":
            qs = parse_qs(u.query)
            ns = (qs.get("namespace") or [scanner.NAMESPACE])[0]
            log.info("GET /scan?namespace=%s", ns)
            return self._scan(ns)
        return self._send(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        # Identical semantics to GET /scan — scanners are typically triggered
        # as actions rather than reads.
        u = urlparse(self.path)
        if u.path == "/scan":
            qs = parse_qs(u.query)
            ns = (qs.get("namespace") or [scanner.NAMESPACE])[0]
            log.info("POST /scan?namespace=%s", ns)
            return self._scan(ns)
        return self._send(404, {"status": "error", "message": "not found"})

    def do_HEAD(self):
        return self.do_GET()

    def log_message(self, fmt, *args):
        pass


def main():
    log.info("Popeye scanner API on %s:%d (mode=%s, data_source=%s, engine=%s)",
             BIND_HOST, BIND_PORT, scanner.AOPS_MODE,
             scanner.CLIENT.data_source,
             "popeye-binary" if (scanner.POPEYE_BIN and
                                 scanner.POPEYE_MODE == "real" and
                                 scanner.AOPS_MODE == "real")
             else "builtin-analyzers")
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), PopeyeHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
