FROM python:3.11-slim

LABEL org.opencontainers.image.title="A.O.P.S. Dify-lite Agent"
LABEL org.opencontainers.image.description="OpenAI-compatible agentic reasoning service with Ollama backend"

WORKDIR /app
COPY app.py .

EXPOSE 8002

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8002/healthz')"]

CMD ["python3", "app.py"]
