#!/bin/bash
set -e
PASS="CourseK8s@2026"
CFG='{
  "registry-mirrors": ["https://docker.1ms.run", "https://hub.rat.dev"],
  "insecure-registries": ["172.16.0.11:5000"]
}'

mkdir -p /etc/docker
echo "$CFG" > /etc/docker/daemon.json
systemctl restart docker || true

for ip in 172.16.0.12 172.16.0.13 172.16.0.21 172.16.0.22 172.16.0.23; do
  echo ">>> docker mirror on $ip"
  sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$ip "mkdir -p /etc/docker; printf '%s' '$CFG' > /etc/docker/daemon.json; systemctl restart docker || true" || echo "WARN $ip"
done

echo "=== wait 20s ==="
sleep 20
kubectl get nodes
kubectl -n kube-system get pods | grep calico || true
