FROM python:3.11-slim

LABEL org.opencontainers.image.title="A.O.P.S. Popeye Scanner"
LABEL org.opencontainers.image.description="Popeye Kubernetes sanitizer — runs real Popeye binary or built-in analyzers"

# Install Popeye Go binary (real tool from derailed/popeye)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://github.com/derailed/popeye/releases/download/v0.21.3/popeye_Linux_x86_64.tar.gz | \
    tar xz -C /usr/local/bin popeye && \
    chmod +x /usr/local/bin/popeye && \
    apt-get remove -y curl && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Install kubectl for cluster access
RUN curl -fsSL "https://dl.k8s.io/release/v1.29.4/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl

WORKDIR /app
COPY app.py scanner.py ./

EXPOSE 8004

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8004/healthz')"]

CMD ["python3", "app.py"]
