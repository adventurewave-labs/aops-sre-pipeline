FROM python:3.11-slim

LABEL org.opencontainers.image.title="A.O.P.S. Popeye Scanner"
LABEL org.opencontainers.image.description="Popeye Kubernetes sanitizer — runs real Popeye binary or built-in analyzers"

# Install Popeye Go binary (real tool from derailed/popeye)
ARG POPEYE_VERSION=v0.22.1
ARG KUBECTL_VERSION=v1.29.4
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL "https://github.com/derailed/popeye/releases/download/${POPEYE_VERSION}/popeye_linux_amd64.tar.gz" | \
    tar xz -C /usr/local/bin popeye && \
    chmod +x /usr/local/bin/popeye && \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    popeye version && kubectl version --client && \
    apt-get purge -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py scanner.py ./

EXPOSE 8004

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8004/healthz')"]

CMD ["python3", "app.py"]
