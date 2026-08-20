#!/usr/bin/env python3
"""
Mock Slack receiver for the A.O.P.S. demo.

Receives the remediation card POSTed by n8n at /webhook/slack
(and the simpler /slack path), stores each card in memory, and serves
a Slack-card-style HTML page at / that you can leave open in a browser
to watch the demo unfold.

  GET  /             -> HTML view of all received cards (auto-refresh)
  POST /slack        -> Slack incoming-webhook shaped payload
  POST /webhook/slack -> same, n8n HTTP node will hit this URL
  GET  /alerts.json  -> JSON list of all received alerts (for UAT / scripts)
  GET  /healthz
"""

from __future__ import annotations

import os
import sys
import json
import time
import html
import re
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "templates", "slack_card.html")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [mock-slack] %(levelname)s %(message)s")
log = logging.getLogger("mock-slack")

BIND_HOST = os.environ.get("MOCK_SLACK_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("MOCK_SLACK_PORT", "8003"))

# In-memory alert store (the demo only needs to survive the lifetime of the
# process; persistence across restarts is out of scope.)
ALERTS: list[dict] = []


# ---------------------------------------------------------------------------
# Markdown → HTML (very small subset, enough for the demo)
# ---------------------------------------------------------------------------
def md_to_html(md: str) -> str:
    """Render a tiny subset of Markdown to HTML.

    Supports: # / ## headers, fenced ``` blocks, inline `code`,
    numbered list items, bullets, and paragraphs.
    """
    if not md:
        return ""
    # Escape HTML first
    out = html.escape(md)
    # Fenced code blocks
    out = re.sub(r"```(\w+)?\n(.*?)```",
                 lambda m: f'<pre>{m.group(2)}</pre>', out, flags=re.S)
    # Headers
    out = re.sub(r"^###\s+(.+)$", r"<h3>\1</h3>", out, flags=re.M)
    out = re.sub(r"^##\s+(.+)$", r"<h2>\1</h2>", out, flags=re.M)
    out = re.sub(r"^#\s+(.+)$", r"<h1>\1</h1>", out, flags=re.M)
    # Numbered list items (lines starting with N.)
    out = re.sub(r"^\s*(\d+)\.\s+(.+)$",
                 lambda m: f"<li>{m.group(2)}</li>", out, flags=re.M)
    # Bullets
    out = re.sub(r"^\s*-\s+(.+)$",
                 lambda m: f"<li>{m.group(1)}</li>", out, flags=re.M)
    # Inline code (after escaping)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    # Wrap consecutive <li>...</li> in <ul>...</ul>
    out = re.sub(r"((?:<li>.*</li>\s*)+)",
                 lambda m: f"<ul>{m.group(1)}</ul>", out, flags=re.S)
    # Paragraphs — split on double newlines not inside pre/ul
    parts = re.split(r"\n\n+", out)
    final = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith(("<h1>", "<h2>", "<h3>", "<ul>", "<pre>")):
            final.append(p)
        else:
            final.append(f"<p>{p}</p>")
    return "\n".join(final)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class SlackHandler(BaseHTTPRequestHandler):
    server_version = "AOPSMockSlack/0.1"

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, indent=2).encode("utf-8"))

    def do_GET(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        if u.path in ("/healthz", "/livez", "/readyz"):
            return self._send_json(200, {"status": "ok",
                                         "alerts_received": len(ALERTS)})
        if u.path == "/alerts.json":
            return self._send_json(200, {"alerts": ALERTS})
        if u.path == "/" or u.path == "/slack":
            return self._render_html()
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        if u.path not in ("/slack", "/webhook/slack"):
            return self._send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        log.info("POST %s body=%d chars", u.path, len(raw))

        # Accept either raw text or JSON envelopes
        alert = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "raw": raw[:2000],
        }
        try:
            payload = json.loads(raw)
            # Slack incoming-webhook shape:
            # { "text": "...", "blocks": [...], "attachments": [...],
            #   "channel": "...", "username": "..." }
            alert["raw_payload"] = payload
            # Extract a structured card shape that we can render nicely.
            text = (payload.get("text") or payload.get("message") or "")
            if not text and payload.get("attachments"):
                # n8n sometimes wraps the runbook in attachments[].text
                text = payload["attachments"][0].get("text", "") or \
                       payload["attachments"][0].get("fallback", "")
            if not text:
                text = raw
            alert["text"] = text
            alert["alert_name"] = payload.get("alert_name",
                                              "PaymentAPIHighErrorRate")
            alert["namespace"] = payload.get("namespace", "payment-prod")
            alert["severity"] = payload.get("severity", "critical")
            alert["score"] = payload.get("score", 73)
            alert["grade"] = payload.get("grade", "C")
            alert["trace"] = payload.get("trace", {})
            alert["duration_s"] = payload.get("duration_s")
            alert["remediation_html"] = md_to_html(text)
        except Exception as e:
            log.warning("payload not JSON: %s", e)
            alert["text"] = raw
            alert["alert_name"] = "PaymentAPIHighErrorRate"
            alert["namespace"] = "payment-prod"
            alert["severity"] = "critical"
            alert["score"] = 73
            alert["grade"] = "C"
            alert["remediation_html"] = md_to_html(raw)

        ALERTS.append(alert)
        log.info("Card stored (total=%d) alert=%s grade=%s",
                 len(ALERTS), alert["alert_name"], alert["grade"])
        return self._send_json(200, {"ok": True, "received": alert["ts"],
                                     "total_alerts": len(ALERTS)})

    def do_HEAD(self):
        return self.do_GET()

    def _render_html(self):
        try:
            with open(TEMPLATE_PATH, "r") as f:
                tpl = f.read()
        except Exception as e:
            return self._send_json(500, {"error": f"template missing: {e}"})

        from string import Template
        # The template uses Django-ish {{ }} syntax which `string.Template`
        # doesn't speak — we'll do a simple manual substitution.
        replacements = {
            "{{ total_alerts }}": str(len(ALERTS)),
            "{{ last_update }}": (ALERTS[-1]["ts"] if ALERTS else "—"),
            "{{ webhook_url }}": f"http://{BIND_HOST}:{BIND_PORT}/slack",
            "{% if not alerts %}": ("<div class='empty'>" if not ALERTS else "<!--"),
            "{% else %}": ("-->" if not ALERTS else ""),
            "{% endif %}": ("</div>" if not ALERTS else ""),
            "{% for a in alerts %}": "",
            "{% endfor %}": "",
        }
        # Render the alerts loop manually
        if ALERTS:
            # Replace the for loop body
            pattern = re.compile(
                r"\{% for a in alerts %\}(.*?)\{% endfor %\}", re.S)
            loop_body = pattern.search(tpl)
            if loop_body:
                rendered = []
                for a in ALERTS:
                    chunk = loop_body.group(1)
                    chunk = chunk.replace("{{ a.ts }}", html.escape(a.get("ts","")))
                    chunk = chunk.replace("{{ a.alert_name }}",
                                          html.escape(str(a.get("alert_name",""))))
                    chunk = chunk.replace("{{ a.namespace }}",
                                          html.escape(str(a.get("namespace",""))))
                    chunk = chunk.replace("{{ a.severity }}",
                                          html.escape(str(a.get("severity",""))))
                    chunk = chunk.replace("{{ a.score }}",
                                          str(a.get("score","")))
                    chunk = chunk.replace("{{ a.grade }}",
                                          html.escape(str(a.get("grade",""))))
                    if a.get("error"):
                        chunk = chunk.replace("{{ a.error }}",
                                              html.escape(str(a["error"])))
                    chunk = chunk.replace("{{ a.remediation_html | safe }}",
                                          a.get("remediation_html",""))
                    # Conditional on a.error
                    chunk = chunk.replace(
                        "{% if a.error %}",
                        "<!--" if not a.get("error") else "")
                    chunk = chunk.replace("{% else %}",
                                          "-->" if not a.get("error") else "")
                    chunk = chunk.replace("{% endif %}", "")
                    rendered.append(chunk)
                tpl = tpl[:loop_body.start()] + "".join(rendered) + tpl[loop_body.end():]

            # Empty state
            tpl = re.sub(r"\{% if not alerts %\}(.*?)\{% else %\}(.*?)\{% endif %\}",
                         (lambda m: m.group(1) if not ALERTS else m.group(2)),
                         tpl, flags=re.S)
        else:
            # No alerts — keep the empty branch
            tpl = re.sub(r"\{% if not alerts %\}(.*?)\{% else %\}(.*?)\{% endif %\}",
                         (lambda m: m.group(1)), tpl, flags=re.S)
            # Also remove any unfilled for-loop markers
            tpl = re.sub(r"\{% for a in alerts %\}(.*?)\{% endfor %\}",
                         "", tpl, flags=re.S)

        # Replace remaining simple vars
        for k, v in replacements.items():
            if k.startswith("{%"):
                continue  # already handled above
            tpl = tpl.replace(k, v)

        # Auto-refresh meta tag (every 3s) so the page picks up new alerts live
        tpl = tpl.replace("<meta charset=\"utf-8\">",
                          "<meta charset=\"utf-8\">\n  <meta http-equiv=\"refresh\" content=\"3\">")

        self._send(200, tpl.encode("utf-8"), ctype="text/html")

    def log_message(self, fmt, *args):
        pass


def main():
    log.info("Mock Slack on %s:%d (template=%s)",
             BIND_HOST, BIND_PORT, TEMPLATE_PATH)
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), SlackHandler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
