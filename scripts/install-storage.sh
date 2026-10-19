#!/bin/bash
set -e
echo "=== install local-path-provisioner ==="
curl -sfL --max-time 60 https://cdn.jsdelivr.net/gh/rancher/local-path-provisioner@v0.0.30/deploy/local-path-storage.yaml -o /tmp/local-path.yaml
kubectl apply -f /tmp/local-path.yaml

echo "=== set default storageclass ==="
kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' 2>/dev/null || true

kubectl -n local-path-storage rollout status deployment/local-path-provisioner --timeout=180s || true
kubectl get storageclass
