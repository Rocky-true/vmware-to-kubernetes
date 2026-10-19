#!/bin/bash
# 在 master1 上安装 Kubekey（国内镜像）
set -e
export KKZONE=cn
cd /root
curl -sfL https://get-kk.kubesphere.io | VERSION=v3.1.10 sh -
chmod +x kk
./kk version
