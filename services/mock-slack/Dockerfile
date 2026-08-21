FROM python:3.11-slim

LABEL org.opencontainers.image.title="A.O.P.S. Slack Receiver"
LABEL org.opencontainers.image.description="Slack-card HTML receiver with auto-refresh, supports real Slack webhooks"

WORKDIR /app
COPY app.py ./
COPY templates/ templates/

EXPOSE 8003

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8003/healthz')"]

CMD ["python3", "app.py"]
