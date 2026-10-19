#!/bin/bash
set -e
cd /tmp
# 与 kubeadm 的 podSubnet 保持一致
sed -i 's|# - name: CALICO_IPV4POOL_CIDR|- name: CALICO_IPV4POOL_CIDR|' calico.yaml
sed -i 's|#   value: "192.168.0.0/16"|  value: "10.233.64.0/18"|' calico.yaml
grep -n -A1 CALICO_IPV4POOL_CIDR calico.yaml | head -4

echo "=== apply calico ==="
kubectl apply -f calico.yaml

echo "=== wait calico-node ==="
kubectl -n kube-system rollout status daemonset/calico-node --timeout=240s || true
kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=180s || true

echo "=== nodes ==="
kubectl get nodes
kubectl -n kube-system get pods | grep calico
