#!/bin/bash
# master1 上执行：本地registry + 构建镜像 + 配置各节点运行时(适配docker/containerd) + 部署K8s
set -e
: "${NODE_PASSWORD:?missing NODE_PASSWORD}"
: "${MASTER1_IP:?missing MASTER1_IP}"
: "${NODE_IPS:?missing NODE_IPS (space separated)}"
: "${NODE_PORT:=30080}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null 2>&1 || true
command -v docker >/dev/null 2>&1 || apt-get install -y docker.io >/dev/null 2>&1
apt-get install -y sshpass >/dev/null 2>&1 || true

# 节点运行时配置脚本：同时兼容 docker(cri-dockerd) 与 containerd
cat > /tmp/node-registry.sh <<'EOS'
#!/bin/bash
set -e
if command -v docker >/dev/null 2>&1; then
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://docker.1ms.run", "https://hub.rat.dev"],
  "insecure-registries": ["__MASTER1_IP__:5000"]
}
EOF
  systemctl restart docker || true
fi
if [ -f /etc/containerd/config.toml ]; then
  CONF=/etc/containerd/config.toml
  if ! grep -q "docker.1ms.run" "$CONF" 2>/dev/null; then
    cat >> "$CONF" <<'EOF'

[plugins."io.containerd.grpc.v1.cri".registry.mirrors."docker.io"]
  endpoint = ["https://docker.1ms.run", "https://hub.rat.dev"]

[plugins."io.containerd.grpc.v1.cri".registry.mirrors."__MASTER1_IP__:5000"]
  endpoint = ["http://__MASTER1_IP__:5000"]
EOF
  fi
  systemctl restart containerd || true
fi
EOS
sed -i "s|__MASTER1_IP__|${MASTER1_IP}|g" /tmp/node-registry.sh
chmod +x /tmp/node-registry.sh

# 先配置 master1 自身（构建需要拉基础镜像）
bash /tmp/node-registry.sh

# 本地 registry（模拟 Harbor）
docker ps -a --format '{{.Names}}' | grep -q '^registry$' || \
  docker run -d -p 5000:5000 --restart=always --name registry registry:2

# 构建并推送应用镜像
cd /root/course-selection
docker build -t ${MASTER1_IP}:5000/course-app:1.1 ./app
docker push ${MASTER1_IP}:5000/course-app:1.1

# 配置其余节点
for ip in $NODE_IPS; do
  if [ "$ip" = "$MASTER1_IP" ]; then continue; fi
  echo ">>> configure runtime on $ip"
  sshpass -p "$NODE_PASSWORD" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$ip "bash -s" < /tmp/node-registry.sh || echo "WARN: $ip failed"
done

# 默认存储类（kubeadm 不自带 StorageClass）
if ! kubectl get storageclass local-path >/dev/null 2>&1; then
  echo ">>> install local-path-provisioner"
  curl -sfL --max-time 60 https://cdn.jsdelivr.net/gh/rancher/local-path-provisioner@v0.0.30/deploy/local-path-storage.yaml -o /tmp/local-path.yaml
  kubectl apply -f /tmp/local-path.yaml
  kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' 2>/dev/null || true
fi

# 部署清单（镜像指向私有registry）
sed -i "s|image: course-app:1.0|image: ${MASTER1_IP}:5000/course-app:1.1|" k8s/03-deployments.yaml
kubectl apply -f k8s/
kubectl -n course-selection wait --for=condition=Ready pod --all --timeout=300s || true
kubectl -n course-selection patch svc app -p \
  "{\"spec\":{\"type\":\"NodePort\",\"ports\":[{\"port\":8000,\"targetPort\":8000,\"nodePort\":${NODE_PORT}}]}}" 2>/dev/null || true

echo "=== PODS ==="
kubectl -n course-selection get pods,svc
echo "=== HEALTH ==="
curl -s "http://localhost:${NODE_PORT}/health" || true
