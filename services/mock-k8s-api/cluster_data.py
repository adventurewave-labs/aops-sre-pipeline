"""
Source-of-truth state for the mock Kubernetes cluster.

Inventory:
  - 2 Nodes            (1 healthy, 1 DiskPressure)
  - 2 Deployments     (payment-api: ImagePullBackOff, payment-worker: CrashLoopBackOff)
  - 1 Ingress         (dangling - references non-existent payment-frontend service)
  - 1 PVC             (Pending - StorageClass fast-ssd no longer exists)
  - 1 Service         (payment-api; payment-frontend intentionally absent)

Every value returned by the mock K8s API is constructed from these dicts so the
state is fully deterministic and reproducible across the A.O.P.S. demo.
"""

NAMESPACE = "payment-prod"

# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
NODES = {
    "items": [
        {
            "metadata": {
                "name": "worker-prod-01",
                "labels": {
                    "kubernetes.io/role": "worker",
                    "topology.kubernetes.io/zone": "us-east-1a",
                    "node.kubernetes.io/instance-type": "m5.large",
                },
                "creationTimestamp": "2026-07-10T08:14:33Z",
            },
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True",
                     "lastHeartbeatTime": "2026-08-20T06:30:00Z",
                     "reason": "KubeletReady"},
                    {"type": "DiskPressure", "status": "False",
                     "lastHeartbeatTime": "2026-08-20T06:30:00Z"},
                    {"type": "MemoryPressure", "status": "False"},
                    {"type": "PIDPressure", "status": "False"},
                ],
                "capacity": {"cpu": "2", "memory": "8Gi", "pods": "110"},
                "allocatable": {"cpu": "1900m", "memory": "7Gi", "pods": "110"},
                "nodeInfo": {"kubeletVersion": "v1.29.4", "osImage": "Ubuntu 22.04.4 LTS"},
            },
        },
        {
            "metadata": {
                "name": "worker-prod-02",
                "labels": {
                    "kubernetes.io/role": "worker",
                    "topology.kubernetes.io/zone": "us-east-1b",
                },
                "creationTimestamp": "2026-07-10T08:14:35Z",
                "annotations": {
                    "node.alpha.kubernetes.io/ttl": "0",
                },
            },
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True",
                     "lastHeartbeatTime": "2026-08-20T06:30:00Z",
                     "reason": "KubeletReady"},
                    {"type": "DiskPressure", "status": "True",
                     "lastHeartbeatTime": "2026-08-20T06:32:11Z",
                     "reason": "KubeletHasDiskPressure",
                     "message": "Disk pressure on node worker-prod-02 "
                                "(usage 92%, threshold 85%)"},
                    {"type": "MemoryPressure", "status": "False"},
                    {"type": "PIDPressure", "status": "False"},
                ],
                "capacity": {"cpu": "2", "memory": "8Gi", "pods": "110"},
                "allocatable": {"cpu": "1500m", "memory": "6Gi", "pods": "110"},
                "nodeInfo": {"kubeletVersion": "v1.29.4", "osImage": "Ubuntu 22.04.4 LTS"},
            },
        },
    ],
    "kind": "NodeList",
    "apiVersion": "v1",
}

# ---------------------------------------------------------------------------
# Deployments (2 broken)
# ---------------------------------------------------------------------------
DEPLOYMENTS = {
    "items": [
        {
            "metadata": {
                "name": "payment-api",
                "namespace": "payment-prod",
                "creationTimestamp": "2026-08-15T10:00:00Z",
                "labels": {"app": "payment-api", "tier": "api"},
                "annotations": {"deployment.kubernetes.io/revision": "9"},
            },
            "spec": {
                "replicas": 3,
                "selector": {"matchLabels": {"app": "payment-api"}},
                "template": {
                    "metadata": {"labels": {"app": "payment-api"}},
                    "spec": {
                        "containers": [{
                            "name": "payment-api",
                            "image": "registry.internal/payments/payment-api:broken-v9",
                            "ports": [{"containerPort": 8080}],
                            "resources": {
                                "requests": {"cpu": "200m", "memory": "256Mi"},
                                "limits":   {"cpu": "500m", "memory": "512Mi"},
                            },
                            "env": [{"name": "LOG_LEVEL", "value": "info"}],
                        }],
                        "restartPolicy": "Always",
                    },
                },
                "strategy": {"type": "RollingUpdate",
                              "rollingUpdate": {"maxUnavailable": "25%", "maxSurge": "25%"}},
            },
            "status": {
                "observedGeneration": 9,
                "replicas": 3,
                "updatedReplicas": 3,
                "readyReplicas": 0,
                "availableReplicas": 0,
                "unavailableReplicas": 3,
                "conditions": [
                    {"type": "Available", "status": "False",
                     "reason": "MinimumReplicasUnavailable",
                     "message": "Deployment does not have minimum availability."},
                    {"type": "Progressing", "status": "False",
                     "reason": "ProgressDeadlineExceeded",
                     "message": "ReplicaSet \"payment-api-7d8f6c5b9x\" has timed out progressing."},
                ],
            },
        },
        {
            "metadata": {
                "name": "payment-worker",
                "namespace": "payment-prod",
                "creationTimestamp": "2026-08-15T10:00:00Z",
                "labels": {"app": "payment-worker", "tier": "worker"},
                "annotations": {"deployment.kubernetes.io/revision": "3"},
            },
            "spec": {
                "replicas": 2,
                "selector": {"matchLabels": {"app": "payment-worker"}},
                "template": {
                    "metadata": {"labels": {"app": "payment-worker"}},
                    "spec": {
                        "containers": [{
                            "name": "payment-worker",
                            "image": "registry.internal/payments/payment-worker:v3",
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits":   {"cpu": "200m", "memory": "128Mi"},
                            },
                            "env": [{"name": "QUEUE", "value": "payments"}],
                        }],
                        "restartPolicy": "Always",
                    },
                },
                "strategy": {"type": "RollingUpdate"},
            },
            "status": {
                "observedGeneration": 3,
                "replicas": 2,
                "readyReplicas": 0,
                "availableReplicas": 0,
                "unavailableReplicas": 2,
                "conditions": [
                    {"type": "Available", "status": "False",
                     "reason": "MinimumReplicasUnavailable",
                     "message": "Deployment does not have minimum availability."},
                ],
            },
        },
    ],
    "kind": "DeploymentList",
    "apiVersion": "apps/v1",
}

# ---------------------------------------------------------------------------
# Pods (children of broken Deployments)
# ---------------------------------------------------------------------------
PODS = {
    "items": [
        # payment-api pods — all ImagePullBackOff
        {
            "metadata": {
                "name": "payment-api-7d8f6c5b9x-abc12",
                "namespace": "payment-prod",
                "labels": {"app": "payment-api", "pod-template-hash": "7d8f6c5b9x"},
                "ownerReferences": [{"kind": "ReplicaSet",
                                     "name": "payment-api-7d8f6c5b9x",
                                     "controller": True}],
            },
            "spec": {
                "nodeName": "worker-prod-01",
                "containers": [{"name": "payment-api",
                                "image": "registry.internal/payments/payment-api:broken-v9"}],
            },
            "status": {
                "phase": "Pending",
                "startTime": "2026-08-20T06:18:42Z",
                "containerStatuses": [{
                    "name": "payment-api",
                    "state": {"waiting": {
                        "reason": "ImagePullBackOff",
                        "message": "Failed to pull image \"registry.internal/payments/payment-api:broken-v9\": rpc error: code = Unknown desc = Error response from daemon: manifest unknown",
                    }},
                    "ready": False,
                    "restartCount": 0,
                    "image": "registry.internal/payments/payment-api:broken-v9",
                }],
            },
        },
        {
            "metadata": {
                "name": "payment-api-7d8f6c5b9x-def34",
                "namespace": "payment-prod",
                "labels": {"app": "payment-api", "pod-template-hash": "7d8f6c5b9x"},
            },
            "spec": {
                "nodeName": "worker-prod-02",
                "containers": [{"name": "payment-api",
                                "image": "registry.internal/payments/payment-api:broken-v9"}],
            },
            "status": {
                "phase": "Pending",
                "startTime": "2026-08-20T06:18:44Z",
                "containerStatuses": [{
                    "name": "payment-api",
                    "state": {"waiting": {
                        "reason": "ImagePullBackOff",
                        "message": "manifest unknown",
                    }},
                    "ready": False,
                    "restartCount": 0,
                }],
            },
        },
        {
            "metadata": {
                "name": "payment-api-7d8f6c5b9x-ghi56",
                "namespace": "payment-prod",
                "labels": {"app": "payment-api", "pod-template-hash": "7d8f6c5b9x"},
            },
            "spec": {
                "nodeName": "worker-prod-01",
                "containers": [{"name": "payment-api",
                                "image": "registry.internal/payments/payment-api:broken-v9"}],
            },
            "status": {
                "phase": "Pending",
                "startTime": "2026-08-20T06:18:45Z",
                "containerStatuses": [{
                    "name": "payment-api",
                    "state": {"waiting": {"reason": "ImagePullBackOff",
                                          "message": "manifest unknown"}},
                    "ready": False,
                    "restartCount": 0,
                }],
            },
        },
        # payment-worker pods — CrashLoopBackOff from OOMKilled
        {
            "metadata": {
                "name": "payment-worker-5c8b9d2-x9pqr",
                "namespace": "payment-prod",
                "labels": {"app": "payment-worker", "pod-template-hash": "5c8b9d2"},
            },
            "spec": {
                "nodeName": "worker-prod-02",
                "containers": [{
                    "name": "payment-worker",
                    "image": "registry.internal/payments/payment-worker:v3",
                    "resources": {"requests": {"memory": "128Mi"},
                                  "limits":   {"memory": "128Mi"}},
                }],
            },
            "status": {
                "phase": "Running",
                "startTime": "2026-08-20T06:21:10Z",
                "containerStatuses": [{
                    "name": "payment-worker",
                    "state": {"waiting": {
                        "reason": "CrashLoopBackOff",
                        "message": "back-off 5m0s restarting failed container=payment-worker "
                                   "pod=payment-worker-5c8b9d2-x9pqr_payment-prod(abc123)",
                    }},
                    "lastState": {"terminated": {
                        "reason": "OOMKilled",
                        "exitCode": 137,
                        "message": "Container was OOMKilled (exit code 137) "
                                   "— limit 128Mi, peak 132Mi",
                        "startedAt": "2026-08-20T06:25:11Z",
                        "finishedAt": "2026-08-20T06:25:13Z",
                    }},
                    "ready": False,
                    "restartCount": 8,
                }],
            },
        },
        {
            "metadata": {
                "name": "payment-worker-5c8b9d2-z2stu",
                "namespace": "payment-prod",
                "labels": {"app": "payment-worker", "pod-template-hash": "5c8b9d2"},
            },
            "spec": {
                "nodeName": "worker-prod-01",
                "containers": [{
                    "name": "payment-worker",
                    "image": "registry.internal/payments/payment-worker:v3",
                    "resources": {"limits": {"memory": "128Mi"}},
                }],
            },
            "status": {
                "phase": "Running",
                "startTime": "2026-08-20T06:21:12Z",
                "containerStatuses": [{
                    "name": "payment-worker",
                    "state": {"waiting": {
                        "reason": "CrashLoopBackOff",
                        "message": "back-off 5m0s restarting failed container",
                    }},
                    "lastState": {"terminated": {
                        "reason": "OOMKilled",
                        "exitCode": 137,
                        "message": "Container was OOMKilled (exit code 137)",
                    }},
                    "ready": False,
                    "restartCount": 7,
                }],
            },
        },
    ],
    "kind": "PodList",
    "apiVersion": "v1",
}

# ---------------------------------------------------------------------------
# Ingresses (1 dangling - points to missing service)
# ---------------------------------------------------------------------------
INGRESSES = {
    "items": [
        {
            "metadata": {
                "name": "payment-ingress",
                "namespace": "payment-prod",
                "creationTimestamp": "2026-08-15T10:00:00Z",
                "labels": {"app": "payment"},
                "annotations": {
                    "nginx.ingress.kubernetes.io/rewrite-target": "/",
                    "cert-manager.io/cluster-issuer": "letsencrypt-prod",
                },
            },
            "spec": {
                "ingressClassName": "nginx",
                "tls": [{"hosts": ["payments.example.com"],
                         "secretName": "payments-tls"}],
                "rules": [{
                    "host": "payments.example.com",
                    "http": {
                        "paths": [{
                            "path": "/",
                            "pathType": "Prefix",
                            "backend": {"service": {
                                "name": "payment-frontend",
                                "port": {"number": 80},
                            }},
                        }],
                    },
                }],
            },
            "status": {"loadBalancer": {"ingress": []}},
        },
    ],
    "kind": "IngressList",
    "apiVersion": "networking.k8s.io/v1",
}

# ---------------------------------------------------------------------------
# Services
# NOTE: 'payment-frontend' is intentionally absent — this is what makes the
#       Ingress 'dangling' / orphaned.
# ---------------------------------------------------------------------------
SERVICES = {
    "items": [
        {
            "metadata": {"name": "payment-api", "namespace": "payment-prod",
                          "creationTimestamp": "2026-08-15T10:00:00Z",
                          "labels": {"app": "payment-api"}},
            "spec": {
                "type": "ClusterIP",
                "selector": {"app": "payment-api"},
                "ports": [{"name": "http", "port": 80, "targetPort": 8080,
                           "protocol": "TCP"}],
                "sessionAffinity": "None",
            },
            "status": {"loadBalancer": {}},
        },
    ],
    "kind": "ServiceList",
    "apiVersion": "v1",
}

# ---------------------------------------------------------------------------
# PVCs (1 Pending)
# ---------------------------------------------------------------------------
PVCS = {
    "items": [
        {
            "metadata": {
                "name": "payment-data-pvc",
                "namespace": "payment-prod",
                "creationTimestamp": "2026-08-19T15:30:00Z",
                "labels": {"app": "payment-api", "tier": "data"},
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": "fast-ssd",
                "volumeMode": "Filesystem",
                "resources": {"requests": {"storage": "50Gi"}},
            },
            "status": {
                "phase": "Pending",
                "conditions": [
                    {"type": "Bound", "status": "False",
                     "reason": "ProvisioningFailed",
                     "message": "PersistentVolumeClaim \"payment-data-pvc\" is waiting "
                                "for a volume to be created (no matching StorageClass "
                                "'fast-ssd' found)"},
                ],
            },
        },
    ],
    "kind": "PersistentVolumeClaimList",
    "apiVersion": "v1",
}

# ---------------------------------------------------------------------------
# Events (recent cluster events that explain the failures)
# ---------------------------------------------------------------------------
EVENTS = {
    "items": [
        {
            "metadata": {"name": "payment-api.17a8f6c5b9x", "namespace": "payment-prod"},
            "involvedObject": {"kind": "Deployment", "name": "payment-api",
                                "namespace": "payment-prod"},
            "reason": "FailedDeploy", "type": "Warning",
            "message": "Progress deadline exceeded for Deployment/payment-api",
            "lastTimestamp": "2026-08-20T06:25:42Z",
            "count": 1,
        },
        {
            "metadata": {"name": "payment-worker.5c8b9d2", "namespace": "payment-prod"},
            "involvedObject": {"kind": "Pod", "name": "payment-worker-5c8b9d2-x9pqr",
                                "namespace": "payment-prod"},
            "reason": "BackOff", "type": "Warning",
            "message": "Back-off restarting failed container",
            "lastTimestamp": "2026-08-20T06:30:11Z",
            "count": 8,
        },
        {
            "metadata": {"name": "worker-prod-02.diskpressure"},
            "involvedObject": {"kind": "Node", "name": "worker-prod-02"},
            "reason": "NodeDiskPressure", "type": "Warning",
            "message": "Node worker-prod-02 has DiskPressure (usage 92%)",
            "lastTimestamp": "2026-08-20T06:32:11Z",
            "count": 3,
        },
        {
            "metadata": {"name": "payment-data-pvc.pending"},
            "involvedObject": {"kind": "PersistentVolumeClaim",
                                "name": "payment-data-pvc",
                                "namespace": "payment-prod"},
            "reason": "ProvisioningFailed", "type": "Warning",
            "message": "no matching StorageClass 'fast-ssd' found",
            "lastTimestamp": "2026-08-19T15:30:11Z",
            "count": 5,
        },
    ],
    "kind": "EventList",
    "apiVersion": "v1",
}

ALL = {
    "nodes": NODES,
    "deployments": DEPLOYMENTS,
    "pods": PODS,
    "ingresses": INGRESSES,
    "services": SERVICES,
    "pvcs": PVCS,
    "events": EVENTS,
}
