# A.O.P.S. — Automated Off-the-shelf Pipeline SRE

[![Deploy to Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fadventurewave-labs%2Faops-sre-pipeline)
[![License: MIT](https://img.shields.io/badge/License-MIT-7c5cff.svg)](LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/adventurewave-labs/aops-sre-pipeline)
[![UAT: 10/10](https://img.shields.io/badge/UAT-10%2F10-4ade80.svg)](#-test-it)
[![Latency: 23ms](https://img.shields.io/badge/E2E-23ms-00d4ff.svg)](#-test-it)

> An open-source, alert-driven autonomous SRE pipeline: **Real** Prometheus fires → **real** n8n catches → **real** Popeye scans → agentic reasoning via **real** Ollama LLM → **real** kubectl remediation → Slack notification.

🌐 **Live demo & landing page:** <https://aops-sre-pipeline.vercel.app> *(deploy this repo to Vercel — see `site/DEPLOY.md`)*

![A.O.P.S. pipeline demo](site/aops-demo.gif)

```
  Real Prometheus + Alertmanager ──webhook──> Real n8n
                                            │
                                            ▼ HTTP POST /scan
                                         Popeye (real binary or built-in fallback)
                                            │
                                            ▼ Popeye JSON
                                         dify-lite (agentic reasoning)
                                            │
                                            ▼ OpenAI-compatible API
                                         Real Ollama (qwen2.5:0.5b or llama3.1:8b)
                                            │
                                            ▼ remediation Markdown
                                         Slack receiver (real webhook or local mock)
                                            │
                                            ▼ POST /remediate
                                         Remediation executor (real kubectl fixes)
```

A.O.P.S. mirrors the popular Robusta + K8sGPT stack but swaps every component for an open-source, off-the-shelf alternative that you can run fully self-hosted on your own hardware. The pipeline operates in **two modes**:

- **Real mode** (default): Uses a real Kubernetes cluster (kind/minikube), real Popeye binary, real Ollama LLM, real n8n, real Prometheus + Alertmanager, and real kubectl remediation.
- **Sandbox mode**: Self-contained with a mock K8s API and lightweight Python workflow runner — zero external dependencies, runs anywhere Python is available.

## ✨ What's in the box

| Component | Real Mode | Sandbox Mode | Replaces |
|---|---|---|---|
| **Kubernetes** | Real kind/minikube cluster with 6 deliberately broken resources | mock-k8s-api (Python HTTP server) | your real kube-apiserver |
| **Popeye scanner** | Real `popeye` Go binary (falls back to built-in Python analyzers) | Built-in 14-analyzer Python engine | K8sGPT |
| **dify-lite** | Agentic reasoning with real Ollama LLM backend | Deterministic stub (instant, no LLM needed) | Dify.ai |
| **Ollama** | Real `ollama/ollama` container serving `qwen2.5:0.5b` (or `llama3.1:8b`) | Not started | any OpenAI-compat API |
| **n8n** | Real `n8nio/n8n:latest` with auto-imported workflow | Python n8n-runner (loads same JSON) | — |
| **Prometheus** | Real `prom/prometheus` + `prom/alertmanager` containers | `alert.sh` (single curl) | — |
| **Slack** | Real incoming-webhook (set `REAL_SLACK_WEBHOOK_URL`) | Local HTML card renderer | real Slack incoming-webhook |
| **Remediation executor** | Real `kubectl` commands against live cluster | Dry-run mode | manual SRE intervention |

The demo cluster ships with **six broken resources**:

- 2 Nodes (one in `DiskPressure` at 92% disk usage)
- 2 broken Deployments (`payment-api` in `ImagePullBackOff`, `payment-worker` in `CrashLoopBackOff` from `OOMKilled`)
- 1 dangling Ingress (routes to a Service that doesn't exist)
- 1 Pending PVC (`StorageClass fast-ssd` was deleted)

When Popeye scans that state, it emits findings with codes `NO-002`, `DPL-000`, `DPL-001`, `POP-001`, `POP-002`, `MEM-001`, `ING-001`, `PVC-001`, `PVC-002`. The dify-lite agent (backed by Ollama) reasons through those findings and produces a Markdown remediation runbook with root-cause hypothesis, numbered kubectl commands, blast-radius notes, and suggested follow-up alerts. The remediation executor then applies the fixes for real and re-scans to verify improvement.

## 🚀 Quick start

### Option A: Full real stack (Docker + kind cluster)

```bash
git clone https://github.com/adventurewave-labs/aops-sre-pipeline.git
cd aops-sre-pipeline

# 1. Create a real Kubernetes cluster with broken resources
./scripts/setup-kind-cluster.sh up

# 2. Export kubeconfig for Docker volume mount
export KUBECONFIG_PATH=$(pwd)/.kubeconfig

# 3. Start the full stack (real n8n, real Ollama, real Popeye)
docker compose up -d

# 4. Fire a test alert (or wait for real Prometheus to trigger)
./run.sh alert

# 5. Check the Slack card
open http://localhost:8003

# 6. Trigger automated remediation
curl -X POST http://localhost:8005/remediate

# 7. Check before/after improvement
curl http://localhost:8005/status
```

End-to-end latency with real Ollama (`qwen2.5:0.5b`) on CPU: **1.5–4 s**. Swap `OLLAMA_MODEL=llama3.1:8b` for higher-quality reasoning on a GPU host.

### Option B: Sandbox mode (no Docker, no cluster)

```bash
git clone https://github.com/adventurewave-labs/aops-sre-pipeline.git
cd aops-sre-pipeline

./run.sh up-no-ollama       # starts all services as plain Python processes
./run.sh alert              # fires the PaymentAPIHighErrorRate alert
./run.sh status             # shows running services + health probes
open http://localhost:8003  # view the rendered Slack card (auto-refreshes)

./run.sh stop               # clean shutdown
```

End-to-end latency with the deterministic stub backend: **~23 ms**.

### Option C: Docker sandbox (no cluster needed)

```bash
cd aops-sre-pipeline
AOPS_MODE=sandbox docker compose --profile sandbox up -d
```

### GitHub Codespaces (zero-setup, everything pre-installed)

Click the badge above &mdash; or go to [codespaces.new/adventurewave-labs/aops-sre-pipeline](https://codespaces.new/adventurewave-labs/aops-sre-pipeline).

The Codespace will automatically:

1. **Install everything** &mdash; Python 3.11, Docker-in-Docker, and all port forwardings
2. **Start the full A.O.P.S. stack** in sandbox mode
3. **Print a ready-to-use summary** with all service URLs

Once the Codespace is ready:

```bash
# Fire the demo alert
./run.sh alert

# View the Slack card in the built-in browser (port 8003 auto-forwards)

# Run the full UAT suite
python3 scripts/smoke_test.py
python3 scripts/run_uat.py

# For real LLM mode (Ollama + qwen2.5:0.5b):
docker compose up -d
```

## 📡 Service endpoints

| Service | Port | Path | Purpose |
|---|---|---|---|
| mock-k8s-api | 8001 | `/api/v1/*`, `/apis/apps/v1/*`, `/apis/networking.k8s.io/v1/*` | Mock K8s API (sandbox only) |
| popeye-scanner | 8004 | `POST /scan?namespace=payment-prod` | Popeye scan (real binary or built-in) |
| dify-lite | 8002 | `POST /v1/chat/completions`, `GET /healthz` | Agentic reasoning (Ollama or stub) |
| slack-receiver | 8003 | `POST /webhook/slack`, `GET /` | Slack card receiver |
| n8n | 5678 | Webhook + visual workflow editor | Real n8n (default) |
| n8n-runner | 5678 | `POST /webhook/aops-alert`, `GET /workflow`, `GET /runs` | Python workflow runner (sandbox) |
| prometheus | 9090 | Web UI, query API | Real Prometheus (monitoring profile) |
| alertmanager | 9093 | Web UI, webhook receiver | Real Alertmanager (monitoring profile) |
| ollama | 11434 | `/api/chat`, `/v1/chat/completions` | Real LLM runtime |
| remediation-executor | 8005 | `POST /remediate`, `GET /status` | Applies real kubectl fixes |

## 🧪 Test it

```bash
# Sandbox tests
./run.sh up-no-ollama
python3 scripts/smoke_test.py    # component-level smoke tests
python3 scripts/run_uat.py      # 10-test acceptance matrix
./run.sh alert                   # fire a single alert manually
./run.sh demo                    # record an asciinema demo

# Real stack tests
./scripts/setup-kind-cluster.sh up
export KUBECONFIG_PATH=$(pwd)/.kubeconfig
docker compose up -d
docker compose exec n8n-runner python3 scripts/run_uat.py
```

UAT matrix covers: 6 broken resources, Bearer auth, Popeye findings, agent output structure, Slack card persistence, workflow shape, end-to-end latency, idempotency, malformed-input resilience. **10/10 pass** on the sandbox stub backend.

## 📁 Repository layout

```
aops-sre-pipeline/
├── docker-compose.yml              # real stack (n8n, Ollama, Popeye, Prometheus)
├── run.sh                          # sandbox orchestrator (start/stop/alert/demo)
├── k8s-manifests/                  # real K8s resources for kind cluster
│   ├── 00-namespace.yaml
│   ├── 01-broken-deployments.yaml
│   ├── 02-dangling-ingress.yaml
│   ├── 03-missing-storageclass-pvc.yaml
│   └── 04-service.yaml
├── services/
│   ├── mock-k8s-api/
│   │   ├── app.py                  # Python HTTP mock kube-apiserver
│   │   ├── cluster_data.py         # source-of-truth: 6 broken resources
│   │   └── Dockerfile
│   ├── popeye-scanner/
│   │   ├── app.py                  # HTTP wrapper
│   │   ├── scanner.py              # 14 built-in analyzers + real Popeye binary integration
│   │   └── Dockerfile
│   ├── dify-lite/
│   │   ├── app.py                  # Agentic reasoning (Ollama LLM or deterministic stub)
│   │   └── Dockerfile
│   ├── mock-slack/
│   │   ├── app.py                  # Slack receiver (local HTML or real webhook)
│   │   ├── templates/slack_card.html
│   │   └── Dockerfile
│   ├── n8n-runner/
│   │   ├── app.py                  # Python workflow executor (sandbox fallback)
│   │   ├── aops-workflow.json      # n8n-importable workflow definition
│   │   └── Dockerfile
│   ├── remediation-executor/
│   │   ├── app.py                  # Real kubectl remediation + before/after re-scan
│   │   └── Dockerfile
│   └── prometheus-alertmanager/
│       ├── config/
│       │   ├── prometheus.yml      # Prometheus scrape config
│       │   ├── alert_rules.yml     # PaymentAPIHighErrorRate rule
│       │   └── alertmanager.yml    # Webhook receiver → n8n
│       └── alert.sh                # Standalone alert trigger (sandbox)
├── scripts/
│   ├── setup-kind-cluster.sh       # Create real K8s cluster with broken resources
│   ├── smoke_test.py              # component smoke test
│   ├── run_uat.py                 # 10-test acceptance matrix
│   ├── demo_script.sh             # asciinema demo script
│   ├── md_to_pdf.py               # MD→PDF report converter
│   ├── run_uat.py                 # acceptance test matrix
│   └── screenshot_site.py         # landing page screenshot
└── site/
    ├── index.html                 # landing page (deploy to Vercel)
    └── DEPLOY.md                  # Vercel deployment guide
```

## 🔌 Wiring it to your real stack

1. **Real Kubernetes cluster.** The default real mode talks directly to your cluster via kubeconfig. Run `./scripts/setup-kind-cluster.sh up` to create a local kind cluster with pre-broken resources, or point `KUBECONFIG_PATH` at your own cluster's kubeconfig.
2. **Real Prometheus + Alertmanager.** Enable the monitoring profile: `docker compose --profile monitoring up -d`. Configure `alertmanager.yml` to fire webhooks to n8n. The mock `alert.sh` shows the canonical Alertmanager payload shape.
3. **Real Slack.** Set `REAL_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...` when starting the stack. The slack-receiver will forward all alerts to your real Slack channel.
4. **Real n8n.** The default Docker mode uses `n8nio/n8n:latest` and auto-imports `aops-workflow.json`. Open `http://localhost:5678` to visually edit the workflow.
5. **Real Popeye.** In real mode the scanner attempts the `popeye` Go binary first. Install it via `go install github.com/derailed/popeye@latest` or `brew install popeye`. If unavailable, it falls back to the built-in Python analyzers.
6. **Bigger LLM.** Set `OLLAMA_MODEL=llama3.1:8b` for higher-quality reasoning on a GPU host.

## 🧠 Architecture decisions

- **Why Popeye over K8sGPT?** Popeye is rules-based — its findings are deterministic and never hallucinated. The agentic reasoning happens in dify-lite instead, with the LLM grounded in Popeye's structured JSON.
- **Why a custom dify-lite over real Dify.ai?** Dify.ai ships ~8 containers (api/web/worker/sandbox + Postgres + Weaviate + Redis + nginx). dify-lite is a single Python process that implements the same OpenAI-compatible chat-completions API surface with a 2-round tool-calling loop. It defaults to real Ollama for LLM reasoning. When you want the full RAG experience, swap to real Dify — the workflow JSON only needs the URL changed.
- **Why Ollama over a hosted API?** Self-hosted, no per-token cost, no data egress. dify-lite's `auto` backend detects reachability and falls back to the deterministic stub when Ollama is down — the pipeline never breaks.
- **Why real n8n in Docker mode?** The sandbox Python runner exists for zero-dependency demos. In production, the official `n8nio/n8n:latest` image gives you a visual workflow editor, retry logic, and hundreds of integrations. The same `aops-workflow.json` is n8n-importable — no code changes needed.
- **Why kind for the demo cluster?** kind creates real K8s clusters in Docker. This means Popeye scans real cluster state, the remediation executor runs real kubectl commands, and before/after scores reflect actual changes. No simulation.

## ⚠️ Limitations

- The sandbox mock K8s API is **read-only**. In real mode, the remediation executor applies real kubectl commands with a `DRY_RUN` option for safety.
- The sandbox stub backend produces deterministic (rule-based) remediation text. For real LLM reasoning, run with Docker + Ollama.
- Popeye's built-in Python analyzers cover 14 codes (Node, Deployment, Pod, Ingress, Service, PVC). When the real Popeye binary is available, it provides 100+ analyzers.
- The remediation executor applies a fixed set of fixes matching the demo's 6 broken resources. Extending it to handle arbitrary Popeye findings is on the roadmap.

## 📜 License

MIT — see [LICENSE](LICENSE).

## 🙏 Acknowledgements

- [Popeye](https://github.com/derailed/popeye) — the canonical Kubernetes sanitizer (real binary used in production mode).
- [Ollama](https://github.com/ollama/ollama) — the LLM runtime powering agentic reasoning.
- [n8n](https://github.com/n8n-io/n8n) — the workflow engine (real `n8nio/n8n:latest` in Docker mode).
- [Prometheus](https://github.com/prometheus/prometheus) + [Alertmanager](https://github.com/prometheus/alertmanager) — real alerting infrastructure.
- [kind](https://github.com/kubernetes-sigs/kind) — real Kubernetes clusters in Docker.
- [Dify.ai](https://github.com/langgenius/dify) — the agentic platform whose API surface dify-lite mimics.

---

**Maintained by [adventurewave-labs](https://github.com/adventurewave-labs).**
