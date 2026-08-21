#!/usr/bin/env bash
# Setup script for A.O.P.S. real Kubernetes cluster using kind.
#
# Creates a local kind cluster, deploys deliberately-broken resources
# in the payment-prod namespace, and makes the kubeconfig available
# to the A.O.P.S. services.
#
# Usage:
#   ./scripts/setup-kind-cluster.sh          # Create + deploy broken resources
#   ./scripts/setup-kind-cluster.sh destroy   # Tear down the cluster
#   ./scripts/setup-kind-cluster.sh status    # Show cluster + resource status
#   ./scripts/setup-kind-cluster.sh reset      # Destroy + recreate

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
    log_ok "kubectl $(kubectl version --client --short 2>/dev/null || kubectl version --client)"
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
        local not_ready
        not_ready=$(kubectl get nodes --no-headers 2>/dev/null | grep -v ' Ready' | wc -l)
        if [[ "$not_ready" -eq 0 ]]; then
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
export_kubeconfig() {
    local kubeconfig
    kubeconfig="$PROJECT_ROOT/.kubeconfig"
    kind export kubeconfig --name "$CLUSTER_NAME" 2>/dev/null || true
    # Copy to project dir for docker volume mount
    cp "${HOME}/.kube/config" "$kubeconfig" 2>/dev/null || true
    export KUBECONFIG="$kubeconfig"
    log_info "Kubeconfig exported to $kubeconfig"
    echo "  Set KUBECONFIG_PATH=$kubeconfig when running docker compose"
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
    rm -f "$PROJECT_ROOT/.kubeconfig"
    log_ok "Cluster destroyed"
}


# ---- Main ----
case "${1:-up}" in
    up)
        check_prereqs
        create_cluster
        deploy_broken_resources
        echo ""
        log_ok "Kind cluster is ready!"
        echo ""
        echo "  Next steps:"
        echo "    export KUBECONFIG_PATH=$(pwd)/.kubeconfig"
        echo "    docker compose up -d"
        echo "    # Or for sandbox mode (no cluster needed):"
        echo "    AOPS_MODE=sandbox docker compose --profile sandbox up -d"
        echo ""
        ;;
    up-all)
        check_prereqs
        create_cluster
        deploy_broken_resources
        induce_disk_pressure
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
    kubeconfig)
        export_kubeconfig
        echo "$PROJECT_ROOT/.kubeconfig"
        ;;
    *)
        echo "Usage: $0 {up|up-all|destroy|reset|status|kubeconfig}"
        echo ""
        echo "  up          Create cluster + deploy broken resources"
        echo "  up-all      Create cluster + deploy + induce DiskPressure"
        echo "  destroy     Tear down the cluster"
        echo "  reset       Destroy + recreate"
        echo "  status      Show cluster and resource status"
        echo "  kubeconfig  Export kubeconfig path for docker compose"
        exit 1
        ;;
esac
