FROM python:3.11-slim

LABEL org.opencontainers.image.title="A.O.P.S. Remediation Executor"
LABEL org.opencontainers.image.description="Applies real kubectl fixes and re-scans for before/after comparison"

# Install kubectl
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -fsSL "https://dl.k8s.io/release/v1.29.4/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    apt-get remove -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py .

# Mount kubeconfig from host
VOLUME /root/.kube

EXPOSE 8005

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8005/healthz')"]

CMD ["python3", "app.py"]
