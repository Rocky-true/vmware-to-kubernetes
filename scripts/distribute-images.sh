#!/bin/bash
# 通用镜像离线分发：把 /root 下镜像包加载到所有节点
# 用法: TARBALL=/root/images.tar.gz NODE_PASSWORD=xxx bash distribute-images.sh
# 特性: 自动发现节点、兼容 docker/ctr/nerdctl、幂等
set -e

TARBALL="${TARBALL:-/root/images.tar.gz}"
PASS="${NODE_PASSWORD:-}"
SELF_IP=$(hostname -I | awk '{print $1}')

if [ ! -f "$TARBALL" ]; then
  echo "镜像包不存在: $TARBALL" >&2
  exit 1
fi

REMOTE_LOAD='if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then docker load -i /tmp/images.tar.gz; elif command -v ctr >/dev/null 2>&1; then ctr -n k8s.io images import /tmp/images.tar.gz; elif command -v nerdctl >/dev/null 2>&1; then nerdctl -n k8s.io load -i /tmp/images.tar.gz; else echo "no supported runtime" >&2; exit 1; fi; rm -f /tmp/images.tar.gz'

load_local() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker load -i "$TARBALL"
  elif command -v ctr >/dev/null 2>&1; then
    ctr -n k8s.io images import "$TARBALL"
  elif command -v nerdctl >/dev/null 2>&1; then
    nerdctl -n k8s.io load -i "$TARBALL"
  else
    echo "本机无 docker/ctr/nerdctl" >&2
    exit 1
  fi
}

echo ">>> local ($SELF_IP)"
load_local

if ! command -v kubectl >/dev/null 2>&1; then
  echo "无 kubectl，只能加载本机" >&2
  exit 0
fi

NODES=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' 2>/dev/null || true)
if [ -z "$NODES" ]; then
  echo "无法获取节点列表（集群未就绪？）" >&2
  exit 1
fi

if [ -n "$PASS" ] && ! command -v sshpass >/dev/null 2>&1; then
  echo ">>> 安装 sshpass"
  apt-get update -y >/dev/null 2>&1 || true
  apt-get install -y sshpass >/dev/null 2>&1 || true
fi

for ip in $NODES; do
  [ "$ip" = "$SELF_IP" ] && continue
  echo ">>> $ip"
  if [ -n "$PASS" ]; then
    sshpass -p "$PASS" scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      "$TARBALL" root@"$ip":/tmp/images.tar.gz
    sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      root@"$ip" "$REMOTE_LOAD"
  else
    scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      "$TARBALL" root@"$ip":/tmp/images.tar.gz
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      root@"$ip" "$REMOTE_LOAD"
  fi
done

echo "=== 分发完成: $(echo "$NODES" | wc -l) 个节点 ==="
