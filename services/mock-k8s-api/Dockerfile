FROM python:3.11-slim

LABEL org.opencontainers.image.title="A.O.P.S. Mock K8s API"
LABEL org.opencontainers.image.description="Mock Kubernetes API serving a deliberately-broken payment-prod cluster"

WORKDIR /app
COPY app.py cluster_data.py .

EXPOSE 8001

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8001/healthz')"]

CMD ["python3", "app.py"]
