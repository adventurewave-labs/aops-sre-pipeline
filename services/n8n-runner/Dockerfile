FROM python:3.11-slim

LABEL org.opencontainers.image.title="A.O.P.S. n8n Workflow Runner"
LABEL org.opencontainers.image.description="Lightweight workflow executor — loads aops-workflow.json and executes the pipeline"

WORKDIR /app
COPY app.py aops-workflow.json ./

EXPOSE 5678

HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5678/healthz')"]

CMD ["python3", "app.py"]
