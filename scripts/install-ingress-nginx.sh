#!/bin/bash
# 安装 ingress-nginx（演示环境：controller 直接监听节点 80/443）
set -e

URL="https://cdn.jsdelivr.net/gh/kubernetes/ingress-nginx@controller-v1.11.3/deploy/static/provider/baremetal/deploy.yaml"
curl -sfL --max-time 120 "$URL" -o /tmp/ingress-nginx.yaml
kubectl apply -f /tmp/ingress-nginx.yaml

# 不用 LoadBalancer：让 controller 用 hostNetwork 监听节点 80/443
kubectl -n ingress-nginx patch deployment ingress-nginx-controller \
  --type=strategic \
  -p '{"spec":{"template":{"spec":{"hostNetwork":true,"dnsPolicy":"ClusterFirstWithHostNet"}}}}'

kubectl -n ingress-nginx rollout status deployment/ingress-nginx-controller \
  --timeout=300s || true
kubectl -n ingress-nginx get pods,svc
