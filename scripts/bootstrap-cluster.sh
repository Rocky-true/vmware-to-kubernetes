#!/bin/bash
# 通用 kubeadm 集群引导（幂等）：等待就绪 → worker标签 → 镜像加速 → CNI → StorageClass
# 用法: NODE_PASSWORD=xxx bash bootstrap-cluster.sh
# 可选: WAIT=600 MIRROR1=... MIRROR2=... INSECURE_REGISTRY=...
set -e

PASS="${NODE_PASSWORD:-}"
WAIT="${WAIT:-600}"
MIRROR1="${MIRROR1:-https://docker.1ms.run}"
MIRROR2="${MIRROR2:-https://hub.rat.dev}"
SELF_IP=$(hostname -I | awk '{print $1}')

echo "== 1) 等待 kubeconfig"
for _ in $(seq 1 $((WAIT / 10))); do
  [ -f /root/.kube/config ] && break
  sleep 10
done
[ -f /root/.kube/config ] || { echo "kubeconfig 未生成（Kubekey 尚未完成）" >&2; exit 1; }

echo "== 2) 等待全部节点 Ready"
for _ in $(seq 1 $((WAIT / 10))); do
  total=$(kubectl get nodes --no-headers 2>/dev/null | wc -l || echo 0)
  ready=$(kubectl get nodes --no-headers 2>/dev/null | grep -c ' Ready ' || true)
  if [ "$total" -gt 0 ] && [ "$ready" -eq "$total" ]; then
    break
  fi
  sleep 10
done
kubectl get nodes -o wide || true

echo "== 3) 补齐 worker 角色标签"
for node in $(kubectl get nodes --no-headers -l '!node-role.kubernetes.io/control-plane' -o name 2>/dev/null || true); do
  kubectl label "$node" node-role.kubernetes.io/worker= --overwrite >/dev/null 2>&1 || true
done

echo "== 4) 配置镜像加速（docker + containerd）"
cat >/tmp/node-mirror.sh <<'SCRIPT'
#!/bin/bash
M1="${MIRROR1:-https://docker.1ms.run}"
M2="${MIRROR2:-https://hub.rat.dev}"
INSECURE="${INSECURE_REGISTRY:-}"
mkdir -p /etc/docker
if [ -n "$INSECURE" ]; then
  printf '{"registry-mirrors":["%s","%s"],"insecure-registries":["%s"]}\n' "$M1" "$M2" "$INSECURE" >/etc/docker/daemon.json
else
  printf '{"registry-mirrors":["%s","%s"]}\n' "$M1" "$M2" >/etc/docker/daemon.json
fi
systemctl restart docker 2>/dev/null || true
if command -v containerd >/dev/null 2>&1; then
  mkdir -p /etc/containerd/certs.d/docker.io
  cat >/etc/containerd/certs.d/docker.io/hosts.toml <<TOML
server = "https://docker.io"
[host."$M1"]
  capabilities = ["pull", "resolve"]
[host."$M2"]
  capabilities = ["pull", "resolve"]
TOML
  systemctl restart containerd 2>/dev/null || true
fi
echo "mirror ok: $(hostname)"
SCRIPT
chmod +x /tmp/node-mirror.sh

if [ -n "$PASS" ] && ! command -v sshpass >/dev/null 2>&1; then
  apt-get update -y >/dev/null 2>&1 || true
  apt-get install -y sshpass >/dev/null 2>&1 || true
fi

NODES=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}')
for ip in $NODES; do
  if [ "$ip" = "$SELF_IP" ]; then
    MIRROR1="$MIRROR1" MIRROR2="$MIRROR2" INSECURE_REGISTRY="${INSECURE_REGISTRY:-}" bash /tmp/node-mirror.sh
  elif [ -n "$PASS" ]; then
    sshpass -p "$PASS" scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null /tmp/node-mirror.sh root@"$ip":/tmp/node-mirror.sh
    sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"$ip" \
      "MIRROR1='$MIRROR1' MIRROR2='$MIRROR2' INSECURE_REGISTRY='${INSECURE_REGISTRY:-}' bash /tmp/node-mirror.sh"
  else
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null /tmp/node-mirror.sh root@"$ip":/tmp/node-mirror.sh
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"$ip" \
      "MIRROR1='$MIRROR1' MIRROR2='$MIRROR2' INSECURE_REGISTRY='${INSECURE_REGISTRY:-}' bash /tmp/node-mirror.sh"
  fi
done

echo "== 5) CNI（Calico）检查"
if ! kubectl get daemonset -n kube-system calico-node >/dev/null 2>&1; then
  if [ -n "${CALICO_MANIFEST:-}" ]; then
    cp "$CALICO_MANIFEST" /tmp/calico.yaml
  else
    curl -sfL --max-time 90 \
      https://cdn.jsdelivr.net/gh/projectcalico/calico@v3.27.4/manifests/calico.yaml \
      -o /tmp/calico.yaml
    sed -i 's|# - name: CALICO_IPV4POOL_CIDR|- name: CALICO_IPV4POOL_CIDR|' /tmp/calico.yaml
    sed -i 's|#   value: "192.168.0.0/16"|  value: "10.233.64.0/18"|' /tmp/calico.yaml
  fi
  if [ -n "${CALICO_SHA256:-}" ]; then
    echo "$CALICO_SHA256  /tmp/calico.yaml" | sha256sum -c -
  fi
  kubectl apply -f /tmp/calico.yaml
  kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s || true
else
  echo "Calico 已存在"
fi

echo "== 6) 默认 StorageClass（local-path）"
if ! kubectl get storageclass local-path >/dev/null 2>&1; then
  if [ -n "${LOCAL_PATH_MANIFEST:-}" ]; then
    cp "$LOCAL_PATH_MANIFEST" /tmp/local-path.yaml
  else
    curl -sfL --max-time 90 \
      https://cdn.jsdelivr.net/gh/rancher/local-path-provisioner@v0.0.30/deploy/local-path-storage.yaml \
      -o /tmp/local-path.yaml
  fi
  if [ -n "${LOCAL_PATH_SHA256:-}" ]; then
    echo "$LOCAL_PATH_SHA256  /tmp/local-path.yaml" | sha256sum -c -
  fi
  kubectl apply -f /tmp/local-path.yaml
fi
kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' >/dev/null 2>&1 || true

# 兜底：Docker Hub 拉取失败时，用镜像源预拉取并在所有节点重打标签
LP_IMG=$(kubectl -n local-path-storage get deploy local-path-provisioner \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
if [ -n "$LP_IMG" ]; then
  M1HOST=${MIRROR1#https://}; M1HOST=${M1HOST#http://}
  M2HOST=${MIRROR2#https://}; M2HOST=${M2HOST#http://}
  cat >/tmp/prepull-localpath.sh <<'SCRIPT'
#!/bin/bash
IMG="${LP_IMG:?}"
M1="${M1HOST:?}"
M2="${M2HOST:?}"
if ! command -v docker >/dev/null 2>&1; then
  echo "no docker, skip"
  exit 0
fi
if docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "image exists: $IMG"
  exit 0
fi
if timeout 120 docker pull "$M1/$IMG" && docker tag "$M1/$IMG" "$IMG"; then
  echo "prepulled from $M1"
  exit 0
fi
if timeout 120 docker pull "$M2/$IMG" && docker tag "$M2/$IMG" "$IMG"; then
  echo "prepulled from $M2"
  exit 0
fi
echo "prepull failed: $IMG" >&2
exit 1
SCRIPT
  chmod +x /tmp/prepull-localpath.sh
  for ip in $NODES; do
    if [ "$ip" = "$SELF_IP" ]; then
      LP_IMG="$LP_IMG" M1HOST="$M1HOST" M2HOST="$M2HOST" \
        bash /tmp/prepull-localpath.sh || true
    elif [ -n "$PASS" ]; then
      sshpass -p "$PASS" scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        /tmp/prepull-localpath.sh root@"$ip":/tmp/prepull-localpath.sh
      sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        root@"$ip" "LP_IMG='$LP_IMG' M1HOST='$M1HOST' M2HOST='$M2HOST' bash /tmp/prepull-localpath.sh" || true
    fi
  done
  # 清理失败Pod，避免 ImagePullBackOff 退避影响重试
  kubectl -n local-path-storage delete pod -l app=local-path-provisioner \
    --ignore-not-found >/dev/null 2>&1 || true
fi

kubectl -n local-path-storage rollout status deployment/local-path-provisioner --timeout=180s || true

echo "== 7) 集群状态"
kubectl get nodes
kubectl get storageclass
kubectl get pods -A | grep -Ev 'Running|Completed' || true
