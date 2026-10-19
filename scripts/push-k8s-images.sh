#!/bin/bash
# 把 K8s 基础镜像预推到 master 本地 registry，节点统一从 master 拉取
# 用法: REG=172.16.0.11 bash push-k8s-images.sh
set -e

REG="${REG:?需要 REG（master私网IP）}"
NS="${NS:-kubesphereio}"
SRC="${SRC:-registry.cn-beijing.aliyuncs.com/kubesphereio}"
K8S="${K8S_VERSION:-v1.28.2}"
CALICO="${CALICO_VERSION:-v3.27.4}"

push_one() {
  local name=$1 tag=$2
  local src="$SRC/$name:$tag"
  if ! docker image inspect "$src" >/dev/null 2>&1; then
    echo ">>> pull $src"
    docker pull "$src"
  fi
  docker tag "$src" "$REG/$NS/$name:$tag"
  echo ">>> push $REG/$NS/$name:$tag"
  docker push "$REG/$NS/$name:$tag" >/dev/null
}

push_one pause 3.9
push_one kube-apiserver "$K8S"
push_one kube-controller-manager "$K8S"
push_one kube-scheduler "$K8S"
push_one kube-proxy "$K8S"
push_one coredns 1.9.3
push_one k8s-dns-node-cache 1.22.20
push_one cni "$CALICO"
push_one node "$CALICO"
push_one kube-controllers "$CALICO"
push_one pod2daemon-flexvol "$CALICO"
push_one typha "$CALICO"

echo "=== registry images ==="
curl -ks "https://$REG/v2/_catalog"
