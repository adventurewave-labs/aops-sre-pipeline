# A.O.P.S. — Automated Off-the-shelf Pipeline SRE

[![CI](https://github.com/adventurewave-labs/aops-sre-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/adventurewave-labs/aops-sre-pipeline/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-7c5cff.svg)](LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/adventurewave-labs/aops-sre-pipeline)
[![UAT: 15/15](https://img.shields.io/badge/UAT-15%2F15-4ade80.svg)](#-test-it)
[![Sandbox E2E: ~55ms](https://img.shields.io/badge/sandbox%20E2E-~55ms-00d4ff.svg)](#-test-it)

> An alert-driven SRE pipeline: Prometheus fires → n8n catches → Popeye scans → an agent reasons over the findings → a validated plan is applied by kubectl → Slack gets the card.

An open-source, self-hosted alternative to the Robusta + K8sGPT stack, where every component is swappable and nothing about the pipeline's provenance is hidden from you.

🌐 **Landing page:** <https://aops-sre-pipeline.vercel.app>

![A.O.P.S. pipeline demo](site/aops-demo.gif)

```
   Prometheus + Alertmanager ──webhook──> n8n
                                          │
                                          ▼ POST /scan
                                   Popeye scanner ── reads ──> cluster state
                                          │                    (fixtures OR live)
                                          ▼ Popeye-shaped JSON
                                   dify-lite ── agentic reasoning ──> Ollama
                                          │
                                          ├──> Markdown runbook ──> Slack card
                                          │
                                          ▼ structured plan
                                   Remediation executor
                                          │ every step re-validated
                                          ▼ against an allowlist
                                       kubectl
```

---

## The two modes, and how to tell them apart

This is the most important thing to understand about A.O.P.S., so it is the first thing documented.

| | Sandbox mode | Real mode |
|---|---|---|
| Cluster state from | bundled fixtures (`mock-k8s-api`) | a live cluster via kubeconfig |
| Every report labelled | `"data_source": "fixtures"` | `"data_source": "live-cluster"` |
| Needs | Python 3.11 | Docker, kind, kubectl |
| Remediation | dry-run only | real `kubectl`, still dry by default |
| Verified by | CI on every push (`ci.yml`) | CI weekly and on real-mode PRs (`real-mode.yml`), on a live kind cluster |

**The scanner never degrades from one to the other.** If `AOPS_MODE=real` and the cluster is unreachable, `/scan` returns **503** with the reason. It does not fall back to fixtures and label them as real — a report that might be acted on has to be honest about what it describes. The remediation executor enforces the same rule from the other side: it refuses to apply real changes on the strength of a report whose `data_source` is not `live-cluster`.

Every scan says which it is:

```console
$ curl -s -XPOST 'localhost:8004/scan?namespace=payment-prod' | jq '{aops_mode, data_source, engine, score, grade}'
{
  "aops_mode": "sandbox",
  "data_source": "fixtures",
  "engine": "builtin-analyzers",
  "score": 0,
  "grade": "F"
}
```

The Slack card carries the same badge, so nobody reads a runbook without knowing where its facts came from.

---

## 🚀 Quick start

### Sandbox — no Docker, no cluster, ~10 seconds

```bash
git clone https://github.com/adventurewave-labs/aops-sre-pipeline.git
cd aops-sre-pipeline

./run.sh up-sandbox          # all services as plain Python processes
./run.sh alert               # fire the PaymentAPIHighErrorRate alert
./run.sh status               # services + health probes
open http://localhost:8003   # the rendered Slack card

./run.sh remediate           # plan-driven remediation (dry-run)
./run.sh stop
```

End-to-end latency through the full six-node workflow: **~55 ms**. This is what CI runs on every push, and what the UAT badge measures.

### GitHub Codespaces — zero setup

Click the badge above. The devcontainer pins `python:3.11-bookworm`, adds Docker-in-Docker
(needed for real mode, not for sandbox) and forwards every port. It deliberately does **not**
start anything during container creation — work inside `postCreateCommand` that fails or
overruns leaves the Codespace in recovery mode with no shell. Once the editor loads:

```bash
./run.sh up-sandbox   # six local Python services, healthy in ~2s
./run.sh alert        # fire an alert through the pipeline
```

Sandbox mode runs the services as plain Python processes against fixtures — no Docker, no
cluster. Docker is only used by real mode (`docker compose` + kind), below.

### Real mode — Docker + a kind cluster

```bash
# 1. Create a real cluster with the broken resources, kube-state-metrics and
#    the payment-api metrics exporter. Ends with a wiring self-check.
./scripts/setup-kind-cluster.sh up

# 2. Use the INTERNAL kubeconfig — see the note below, it matters
export KUBECONFIG_PATH=$(pwd)/.kubeconfig.internal

# 3. Start the stack against the cluster
docker compose -f docker-compose.yml -f docker-compose.real.yml \
               --profile monitoring up -d

# 4. Verify every link before believing any of it
./scripts/verify-real-mode.sh
```

> **The kubeconfig gotcha.** kind writes a kubeconfig pointing at `https://127.0.0.1:<random-port>`. Mounted into a container, `127.0.0.1` is *that container*, so every kubectl call is refused. `setup-kind-cluster.sh` writes two files — `.kubeconfig` (host-facing) and `.kubeconfig.internal` (`https://aops-demo-control-plane:6443`) — and `docker-compose.real.yml` joins the containers to the `kind` network so the internal one resolves. Mount the internal one.

---

## ✨ What's in the box

| Component | Sandbox | Real | Replaces |
|---|---|---|---|
| **Cluster state** | `mock-k8s-api` fixtures — 6 deliberately broken resources | kind/minikube via kubectl | your kube-apiserver |
| **Popeye scanner** | 14 built-in Python analyzers | upstream `popeye` binary, falling back to the same 14 analyzers *against the same cluster* | K8sGPT |
| **dify-lite** | deterministic rule-based remediator | 2-round agent loop against Ollama | Dify.ai (~8 containers) |
| **Ollama** | not started | `ollama/ollama` serving `qwen2.5:0.5b` or `llama3.1:8b` | any OpenAI-compatible API |
| **n8n** | Python workflow runner (same JSON) | `n8nio/n8n:latest`, workflow imported and activated by the `n8n-import` step | — |
| **Prometheus** | `alert.sh` (one curl) | `prom/prometheus` + `alertmanager`, scraping kube-state-metrics and the payment-api exporter | — |
| **Slack** | local HTML card renderer | real incoming webhook via `REAL_SLACK_WEBHOOK_URL` | Slack |
| **Remediation** | dry-run | real kubectl, allowlist-gated | manual SRE intervention |

The demo cluster ships **six broken resources**: 2 Nodes (one in `DiskPressure`), `payment-api` in `ImagePullBackOff`, `payment-worker` in `CrashLoopBackOff` from `OOMKilled`, a dangling Ingress, and a Pending PVC whose StorageClass was deleted. Popeye emits `NO-002`, `DPL-000/001`, `POP-001/002`, `MEM-001`, `ING-001`, `PVC-001/002`.

---

## 🔒 How remediation is kept safe

The agent does not get to run arbitrary commands. There are three gates, and each has tests:

1. **The plan is structured, not prose.** dify-lite returns a Markdown runbook *and* a machine-readable plan (`_dify_lite_plan`): a list of steps, each naming a `verb`, a `resource` and typed `args`. The runbook is for humans; the plan is what executes.
2. **The executor re-validates every step.** `ALLOWED_VERBS` in the remediation executor maps five verbs to five handlers — `set-image`, `set-resources`, `create-storageclass`, `set-ingress-backend`, `inspect-nodes`. An unknown verb, a resource of the wrong kind, a name that fails the pattern, or a step targeting another namespace is **rejected and recorded, never executed**. The executor's own `/healthz` publishes the allowlist.
3. **Provenance gates mutation.** Even with `DRY_RUN=0`, the executor refuses to apply anything when the scan it is acting on is not `data_source: live-cluster`.

`DRY_RUN=1` is the shipped default. Flip it deliberately.

```console
$ curl -s -XPOST localhost:8005/remediate -d '{"plan":{"namespace":"payment-prod","steps":[
    {"id":"evil","verb":"delete-namespace","resource":"namespace/kube-system"}]}}' | jq '.steps'
[
  {
    "step": "evil",
    "status": "rejected",
    "detail": "verb 'delete-namespace' is not in the allowlist (['create-storageclass', 'inspect-nodes', 'set-image', 'set-ingress-backend', 'set-resources'])"
  }
]
```

---

## 📡 Service endpoints

| Service | Port | Path | Purpose |
|---|---|---|---|
| mock-k8s-api | 8001 | `/api/v1/*`, `/apis/apps/v1/*`, `/apis/networking.k8s.io/v1/*` | fixture kube-apiserver (sandbox only) |
| dify-lite | 8002 | `POST /v1/chat/completions`, `GET /healthz` | reasoning + plan generation |
| slack-receiver | 8003 | `POST /webhook/slack`, `GET /` | Slack card, with provenance badge |
| popeye-scanner | 8004 | `POST /scan?namespace=…`, `GET /healthz` | scan; **503** if real mode can't reach the cluster |
| remediation-executor | 8005 | `POST /remediate`, `GET /status`, `GET /healthz` | allowlist-gated plan execution |
| n8n / n8n-runner | 5678 | `POST /webhook/aops-alert`, `GET /workflow`, `GET /runs` | workflow engine |
| prometheus | 9090 | UI, query API | `--profile monitoring` |
| alertmanager | 9093 | UI, webhook receiver | `--profile monitoring` |
| ollama | 11434 | `/api/chat`, `/v1/chat/completions` | LLM runtime |

---

## 🧪 Test it

```bash
# Unit — parser, allowlist, plan generation. No services needed.
python3 tests/test_popeye_parser.py
python3 tests/test_remediation_allowlist.py

# Sandbox integration
./run.sh up-sandbox
python3 scripts/smoke_test.py     # component smoke test
python3 scripts/run_uat.py        # 15-test acceptance matrix
./run.sh demo                     # scripted end-to-end demo

# Real mode — checks each link and names the broken one
./scripts/setup-kind-cluster.sh up
./scripts/verify-real-mode.sh
```

The UAT matrix covers the six broken resources, Bearer auth, Popeye findings, agent output structure, Slack card persistence, workflow shape, end-to-end latency, idempotency, malformed-input resilience, **scan provenance labelling**, **plan/allowlist agreement**, **refusal to mutate on fixture data**, and **rejection of out-of-allowlist plan steps**.

CI runs all of it on every push, builds all six images, validates both compose files, and **fails if the README's UAT badge disagrees with the suite's actual result**. The badge cannot drift from reality again.

---

## 📁 Repository layout

```
aops-sre-pipeline/
├── .github/workflows/ci.yml         # static + unit + sandbox + image + compose gates
├── .github/workflows/real-mode.yml  # weekly kind-cluster end-to-end gate
├── docker-compose.yml               # sandbox-safe base (no cluster required)
├── docker-compose.real.yml          # real-mode overlay: kind network, live provenance
├── run.sh                           # sandbox orchestrator
├── k8s-manifests/                   # the 6 deliberately broken resources
├── k8s-observability/
│   ├── kube-state-metrics.yaml      # the series every alert rule needs
│   └── payment-api-metrics.yaml     # RED metrics so the 5xx rule can fire
├── services/
│   ├── mock-k8s-api/                # fixture kube-apiserver + cluster_data.py
│   ├── popeye-scanner/              # KubeClient (fixtures|kubectl) + 14 analyzers
│   │                                #   + real-Popeye JSON parser
│   ├── dify-lite/                   # agent loop + structured plan generation
│   ├── mock-slack/                  # card renderer with provenance badge
│   ├── n8n-runner/                  # Python workflow executor + aops-workflow.json
│   ├── remediation-executor/        # allowlist-gated plan execution
│   └── prometheus-alertmanager/config/
├── scripts/
│   ├── setup-kind-cluster.sh        # cluster + observability + wiring self-check
│   ├── verify-real-mode.sh          # end-to-end real-mode verification
│   ├── smoke_test.py                # component smoke test
│   ├── run_uat.py                   # 14-test acceptance matrix
│   └── demo_script.sh               # scripted demo
├── tests/
│   ├── test_popeye_parser.py        # real-Popeye schema, fixtures from upstream
│   ├── test_remediation_allowlist.py
│   └── fixtures/
└── site/                            # Vercel landing page
```

---

## 🧠 Architecture decisions

- **Why Popeye over K8sGPT?** Popeye is rules-based: its findings are deterministic and cannot be hallucinated. The reasoning happens downstream in dify-lite, grounded in Popeye's structured JSON. **This is the whole design.** The LLM interprets facts it did not invent, and the plan it produces is re-validated against an allowlist before anything runs. That is what makes an autonomous remediation loop defensible.
- **Why a custom dify-lite over Dify.ai?** Dify ships ~8 containers (api/web/worker/sandbox + Postgres + Weaviate + Redis + nginx). dify-lite is one Python process implementing the same OpenAI-compatible surface with a 2-round tool-calling loop. Point the workflow at real Dify by changing one URL.
- **Why Ollama over a hosted API?** Self-hosted, no per-token cost, no data egress.
- **Why two kubeconfigs?** Because one of them does not work from inside a container, and quietly getting this wrong is the most common way a "real" kind demo turns out never to have touched a cluster.
- **Why does the scanner 503 instead of falling back?** Because the alternative — serving fixtures labelled as a real cluster — produces a confident, plausible, entirely fictional incident report. A loud failure is strictly better than a quiet lie.

---

## ⚠️ Limitations

Stated plainly, because the point of the provenance work above is that you can trust what this file says.

- **Real mode is verified against a live cluster by CI, but not on every push.** `.github/workflows/real-mode.yml` runs the four commands in [Real mode](#real-mode--docker--a-kind-cluster) above on a clean runner — create the kind cluster, bring the stack up against it, wait for a rule to fire, then gate on `scripts/verify-real-mode.sh` — weekly and on any PR touching the real-mode path. A cluster run costs ~20 minutes, so ordinary pushes are covered by `ci.yml` (sandbox pipeline, all six image builds, both compose configurations) and nothing else. Between weekly runs, the freshest evidence is your own: run that script — it checks each link and names the one that breaks.
- The built-in analyzers cover 14 codes (Node, Deployment, Pod, Ingress, Service, PVC). The real Popeye binary provides 100+.
- The remediation allowlist has five verbs, matching the demo's six broken resources. Extending it means adding a handler here — deliberately, not by widening a wildcard.
- `inspect-nodes` reports DiskPressure rather than clearing it; freeing disk on a node needs host access A.O.P.S. does not have.
- The `payment-api` metrics exporter is a stand-in: the real `payment-api` Deployment is deliberately in `ImagePullBackOff`, so it cannot serve `/metrics`. Prometheus scrapes real counters from the exporter and evaluates the real rule — but those counters are generated, not organic traffic.
- `PaymentAPIHighErrorRate` has `for: 5m`. It takes five to six minutes after startup to fire.

## 📜 License

MIT — see [LICENSE](LICENSE).

## 🙏 Acknowledgements

[Popeye](https://github.com/derailed/popeye) · [Ollama](https://github.com/ollama/ollama) · [n8n](https://github.com/n8n-io/n8n) · [Prometheus](https://github.com/prometheus/prometheus) + [Alertmanager](https://github.com/prometheus/alertmanager) · [kind](https://github.com/kubernetes-sigs/kind) · [kube-state-metrics](https://github.com/kubernetes/kube-state-metrics) · [Dify.ai](https://github.com/langgenius/dify)

---

**Maintained by [adventurewave-labs](https://github.com/adventurewave-labs).**
