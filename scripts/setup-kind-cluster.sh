#!/usr/bin/env bash
# Setup script for A.O.P.S. real Kubernetes cluster using kind.
#
# Creates a local kind cluster, deploys deliberately-broken resources
# in the payment-prod namespace, and makes the kubeconfig available
# to the A.O.P.S. services.
#
# Usage:
#   ./scripts/setup-kind-cluster.sh            # Create + deploy + verify wiring
#   ./scripts/setup-kind-cluster.sh up-all     # ... and induce DiskPressure
#   ./scripts/setup-kind-cluster.sh verify     # Re-check the real-mode wiring
#   ./scripts/setup-kind-cluster.sh status     # Show cluster + resource status
#   ./scripts/setup-kind-cluster.sh reset      # Destroy + recreate
#   ./scripts/setup-kind-cluster.sh destroy    # Tear down the cluster

set -euo pipefail

CLUSTER_NAME="aops-demo"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFESTS_DIR="$PROJECT_ROOT/k8s-manifests"
KIND_CONFIG="$SCRIPT_DIR/kind-config.yaml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${RESET}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${RESET}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
log_error() { echo -e "${RED}[ERROR]${RESET} $*"; }


# ---- Prerequisites ----
check_prereqs() {
    log_info "Checking prerequisites..."
    local missing=()

    if ! command -v kind &>/dev/null; then
        missing+=("kind")
    fi
    if ! command -v kubectl &>/dev/null; then
        missing+=("kubectl")
    fi
    if ! command -v docker &>/dev/null; then
        missing+=("docker")
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing: ${missing[*]}"
        echo ""
        echo "Install them:"
        echo "  kind:   https://kind.sigs.k8s.io/docs/user/quick-start/"
        echo "  kubectl: https://kubernetes.io/docs/tasks/tools/"
        echo ""
        echo "On macOS:  brew install kind kubectl"
        echo "On Linux:  "
        echo "  curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.20.0/kind-linux-amd64"
        echo "  chmod +x ./kind && sudo mv ./kind /usr/local/bin/kind"
        echo "  curl -LO https://dl.k8s.io/release/v1.29.4/bin/linux/amd64/kubectl"
        echo "  chmod +x kubectl && sudo mv kubectl /usr/local/bin/"
        exit 1
    fi

    log_ok "kind $(kind version | head -1)"
    log_ok "kubectl $(kubectl version --client 2>/dev/null | head -1)"
    log_ok "docker $(docker --version)"
}


# ---- Create kind cluster ----
create_cluster() {
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        log_warn "Cluster '$CLUSTER_NAME' already exists"
        return 0
    fi

    log_info "Creating kind cluster '$CLUSTER_NAME'..."

    # Generate kind config if it doesn't exist
    if [[ ! -f "$KIND_CONFIG" ]]; then
        cat > "$KIND_CONFIG" <<'KINDCFG'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: 30080
        protocol: TCP
      # kube-state-metrics (Prometheus scrape target)
      - containerPort: 30081
        hostPort: 30081
        protocol: TCP
      # payment-api RED metrics (Prometheus scrape target)
      - containerPort: 30082
        hostPort: 30082
        protocol: TCP
  - role: worker
    labels:
      topology.kubernetes.io/zone: us-east-1a
  - role: worker
    labels:
      topology.kubernetes.io/zone: us-east-1b
KINDCFG
    fi

    kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG" --wait 120s
    log_ok "Cluster '$CLUSTER_NAME' created"

    # Export kubeconfig
    export_kubeconfig

    # Wait for all nodes to be Ready
    log_info "Waiting for nodes to be Ready..."
    local timeout=120
    local elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        # Do NOT pipe into grep here: under `set -euo pipefail`, grep exiting 1
        # on zero matches kills the script -- and zero matches is exactly the
        # success case (no node lacking ' Ready'). Keep grep inside `if`, where
        # its exit status is consumed rather than fatal.
        local nodes
        nodes=$(kubectl get nodes --no-headers 2>/dev/null) || nodes=""
        if [[ -n "$nodes" ]] && ! grep -qv ' Ready' <<<"$nodes"; then
            log_ok "All nodes Ready"
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done

    if [[ $elapsed -ge $timeout ]]; then
        log_error "Nodes did not become Ready within ${timeout}s"
        kubectl get nodes
        exit 1
    fi

    kubectl get nodes
}


# ---- Deploy broken resources ----
deploy_broken_resources() {
    log_info "Deploying broken resources to payment-prod namespace..."

    kubectl apply -f "$MANIFESTS_DIR"

    log_ok "Resources applied"
    echo ""
    kubectl get all -n payment-prod 2>/dev/null || true
    echo ""
    kubectl get ingress,pvc -n payment-prod 2>/dev/null || true
    echo ""

    # Wait for pods to start (they'll be in ImagePullBackOff / CrashLoopBackOff)
    log_info "Waiting 15s for pods to reach their broken state..."
    sleep 15

    echo ""
    log_info "Current pod status (should show failures):"
    kubectl get pods -n payment-prod -o wide
    echo ""
}


# ---- Induce DiskPressure on worker-02 ----
induce_disk_pressure() {
    log_info "Inducing DiskPressure on worker-prod-02..."
    local node
    node=$(kubectl get nodes -l topology.kubernetes.io/zone=us-east-1b -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

    if [[ -z "$node" ]]; then
        log_warn "Could not find us-east-1b worker node, skipping DiskPressure"
        return 0
    fi

    # Fill disk to >85% on the worker node to trigger DiskPressure
    docker exec "${CLUSTER_NAME}-worker2" sh -c '
        # Create a large file to fill the overlay filesystem
        dd if=/dev/zero of=/var/tmp/disk-fill bs=1M count=1024 2>/dev/null || true
        # Also fill the kind data directory
        while true; do
            avail=$(df /kind | tail -1 | awk "{print \$4}")
            if [[ "$avail" -lt 1048576 ]]; then
                break
            fi
            dd if=/dev/zero of=/kind/disk-fill-$$ bs=1M count=100 2>/dev/null || break
        done
    ' 2>/dev/null || true

    log_info "Waiting for DiskPressure condition to appear..."
    local elapsed=0
    while [[ $elapsed -lt 60 ]]; do
        if kubectl get node "$node" -o jsonpath='{.status.conditions[?(@.type=="DiskPressure")].status}' 2>/dev/null | grep -q "True"; then
            log_ok "DiskPressure detected on $node"
            kubectl describe node "$node" | grep -A2 DiskPressure
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done

    log_warn "DiskPressure did not trigger (may need manual intervention)"
}


# ---- Export kubeconfig ----
#
# TWO kubeconfigs are needed and they are not interchangeable:
#
#   .kubeconfig           host-facing.  server: https://127.0.0.1:<random port>
#                         Used by kubectl on your laptop.
#   .kubeconfig.internal  container-facing. server: https://<cluster>-control-plane:6443
#                         Used by the A.O.P.S. containers, which are attached to
#                         the `kind` docker network.
#
# Mounting the host-facing one into a container is the classic kind mistake:
# 127.0.0.1 inside a container is that container, so every kubectl call gets
# connection-refused. `kind get kubeconfig --internal` is the fix.
export_kubeconfig() {
    local host_cfg="$PROJECT_ROOT/.kubeconfig"
    local int_cfg="$PROJECT_ROOT/.kubeconfig.internal"

    kind export kubeconfig --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
    kind get kubeconfig --name "$CLUSTER_NAME" > "$host_cfg"
    kind get kubeconfig --name "$CLUSTER_NAME" --internal > "$int_cfg"
    chmod 600 "$host_cfg" "$int_cfg"

    export KUBECONFIG="$host_cfg"
    log_info "Host kubeconfig      -> $host_cfg"
    log_info "Container kubeconfig -> $int_cfg  ($(grep -m1 server: "$int_cfg" | tr -s ' '))"
    echo ""
    echo "  export KUBECONFIG_PATH=$int_cfg   # <- this is the one docker compose wants"
}


# ---- kube-state-metrics + a metrics-emitting payment-api (D5) ----
#
# Every alert rule in services/prometheus-alertmanager/config/alert_rules.yml
# is written against kube-state-metrics series. Without KSM in the cluster,
# Prometheus scrapes nothing, no rule can ever fire, and the "real Prometheus
# fires" claim is unbacked. This deploys it.
deploy_observability() {
    log_info "Deploying kube-state-metrics into the cluster..."
    kubectl apply -f "$MANIFESTS_DIR/../k8s-observability/kube-state-metrics.yaml"
    kubectl -n kube-system rollout status deployment/kube-state-metrics \
        --timeout=120s || log_warn "kube-state-metrics did not become Ready in time"

    log_info "Deploying the payment-api metrics exporter (RED metrics)..."
    kubectl apply -f "$MANIFESTS_DIR/../k8s-observability/payment-api-metrics.yaml"
    kubectl -n payment-prod rollout status deployment/payment-api-metrics \
        --timeout=120s || log_warn "payment-api-metrics did not become Ready in time"

    log_info "NodePorts: kube-state-metrics :30081, payment-api metrics :30082"
    kubectl -n kube-system get svc kube-state-metrics -o wide || true

    log_ok "Observability stack deployed"
    echo ""
    echo "  Prometheus scrapes it at kube-state-metrics.kube-system.svc:8080"
    echo "  (from the host: http://localhost:30081/metrics)"
}


# ---- Verify the real-mode wiring end to end ----
verify_real_mode() {
    local failures=0
    local int_cfg="$PROJECT_ROOT/.kubeconfig.internal"

    echo ""
    echo -e "${BOLD}Verifying real-mode wiring${RESET}"

    _check() {
        local label="$1"; shift
        if "$@" >/dev/null 2>&1; then
            log_ok "$label"
        else
            log_error "$label"
            failures=$((failures + 1))
        fi
    }

    _check "cluster reachable from host"        kubectl get nodes
    _check "payment-prod namespace exists"      kubectl get ns payment-prod
    _check "broken deployments present"         kubectl -n payment-prod get deploy payment-api payment-worker
    _check "dangling ingress present"           kubectl -n payment-prod get ingress payment-ingress
    _check "pending PVC present"                kubectl -n payment-prod get pvc payment-data-pvc
    _check "internal kubeconfig written"        test -s "$int_cfg"
    _check "internal kubeconfig is container-facing" \
        bash -c "grep -q 'server: https://${CLUSTER_NAME}-control-plane:6443' '$int_cfg'"
    _check "kube-state-metrics running"         kubectl -n kube-system get deploy kube-state-metrics
    _check "payment-api metrics exporter running" \
        kubectl -n payment-prod get deploy payment-api-metrics
    _check "kube-state-metrics serving series"  \
        bash -c "kubectl -n kube-system exec deploy/kube-state-metrics -- true 2>/dev/null || curl -fsS http://localhost:30081/metrics | grep -q kube_pod_info"
    _check "payment-api emitting http_requests_total" \
        bash -c "curl -fsS http://localhost:30082/metrics | grep -q http_requests_total"
    _check "kind docker network exists"         docker network inspect kind

    echo ""
    if [[ $failures -eq 0 ]]; then
        log_ok "Real-mode wiring verified (all checks passed)"
        echo ""
        echo "  Next:"
        echo "    export KUBECONFIG_PATH=$int_cfg"
        echo "    docker compose --profile monitoring up -d"
        echo "    ./scripts/verify-real-mode.sh      # end-to-end pipeline check"
    else
        log_error "$failures check(s) failed — real mode is NOT wired correctly"
        return 1
    fi
}


# ---- Status ----
show_status() {
    if ! kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        log_warn "Cluster '$CLUSTER_NAME' does not exist"
        return 1
    fi

    export_kubeconfig

    echo ""
    echo -e "${BOLD}Cluster:${RESET}"
    kubectl get nodes -o wide
    echo ""
    echo -e "${BOLD}Namespace payment-prod:${RESET}"
    kubectl get all -n payment-prod 2>/dev/null || true
    kubectl get ingress,pvc -n payment-prod 2>/dev/null || true
    echo ""
    echo -e "${BOLD}Broken State:${RESET}"
    echo -e "  ${RED}●${RESET} payment-api:    ImagePullBackOff (bad image tag)"
    echo -e "  ${RED}●${RESET} payment-worker: CrashLoopBackOff (OOMKilled, 128Mi limit)"
    echo -e "  ${RED}●${RESET} payment-ingress:  Dangling (references missing payment-frontend)"
    echo -e "  ${RED}●${RESET} payment-data-pvc: Pending (missing StorageClass fast-ssd)"
    echo ""
}


# ---- Destroy ----
destroy_cluster() {
    log_info "Destroying cluster '$CLUSTER_NAME'..."
    kind delete cluster --name "$CLUSTER_NAME" 2>/dev/null || true
    rm -f "$PROJECT_ROOT/.kubeconfig" "$PROJECT_ROOT/.kubeconfig.internal"
    log_ok "Cluster destroyed"
}


# ---- Main ----
case "${1:-up}" in
    up)
        check_prereqs
        create_cluster
        deploy_broken_resources
        deploy_observability
        verify_real_mode
        echo ""
        log_ok "Kind cluster is ready!"
        echo ""
        echo "  Next steps:"
        echo "    export KUBECONFIG_PATH=$PROJECT_ROOT/.kubeconfig.internal"
        echo "    docker compose -f docker-compose.yml -f docker-compose.real.yml \\"
        echo "                   --profile monitoring up -d"
        echo "    ./scripts/verify-real-mode.sh"
        echo "    # Or for sandbox mode (no cluster needed):"
        echo "    docker compose --profile sandbox up -d"
        echo ""
        ;;
    up-all)
        check_prereqs
        create_cluster
        deploy_broken_resources
        deploy_observability
        induce_disk_pressure
        verify_real_mode
        echo ""
        log_ok "Kind cluster fully ready with all broken resources!"
        ;;
    destroy)
        destroy_cluster
        ;;
    reset)
        destroy_cluster
        echo ""
        $0 up
        ;;
    status)
        show_status
        ;;
    verify)
        export_kubeconfig
        verify_real_mode
        ;;
    kubeconfig)
        export_kubeconfig
        echo "$PROJECT_ROOT/.kubeconfig"
        ;;
    *)
        echo "Usage: $0 {up|up-all|destroy|reset|status|verify|kubeconfig}"
        echo ""
        echo "  up          Create cluster + deploy broken resources"
        echo "  up-all      Create cluster + deploy + induce DiskPressure"
        echo "  destroy     Tear down the cluster"
        echo "  reset       Destroy + recreate"
        echo "  status      Show cluster and resource status"
        echo "  verify      Check the real-mode wiring (kubeconfig, KSM, resources)"
        echo "  kubeconfig  Export kubeconfig path for docker compose"
        exit 1
        ;;
esac
