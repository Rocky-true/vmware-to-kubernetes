# vmware-to-kubernetes

把 VMware 虚拟机上的 Docker Compose 业务迁移到 Kubernetes，并支持在阿里云一键创建 **3 主 N 从 kubeadm HA 集群**。

项目适合中小项目、课程实验和 POC：输入一个可运行的 `docker-compose.yml`，输出可审查的 K8s 清单，并完成镜像分发、部署、滚动等待和 HTTP 验证。

## 目录

- [运维工具简介](#运维工具简介)
- [项目概览](#项目概览)
- [架构](#架构)
- [功能特性](#功能特性)
- [环境要求](#环境要求)
- [快速开始：阿里云](#快速开始阿里云)
- [数据迁移（datamigrate）](#数据迁移datamigrate)
- [k8slab 使用手册](#k8slab-使用手册)
- [部署到已有 K8s 集群](#部署到已有-k8s-集群)
- [Compose 转换规则](#compose-转换规则)
- [示例项目](#示例项目)
- [运维与安全](#运维与安全)
- [故障排查](#故障排查)
- [测试与验证](#测试与验证)
- [目录结构](#目录结构)
- [License](#license)

## 运维工具简介

`vmware-to-kubernetes` 是一个面向**云计算运维、DevOps、SRE 和平台工程**的 Kubernetes 自动化运维工具。它覆盖云资源创建、集群安装、应用发布、状态检查和资源清理，并提供部署验证与故障排查手册，适合课程实验、POC、中小项目和内部运维平台。

适合运维工程师、DevOps/SRE、平台工程人员，以及需要做 Kubernetes 课程实验或迁移验证的开发者。

核心能力：

- 在阿里云创建 3 主 N 从 kubeadm HA 集群，包含 VPC、安全组、ECS 和 SLB。
- 把 Docker Compose 转换为可审查的 Kubernetes 清单，支持本机构建、离线镜像包和私有仓库。
- 提供 `doctor`、`status`、`destroy` 等运维命令，以及部署后的 HTTP 验证。
- 内置安全默认值：收敛公网入口、随机节点密码、内网 SLB API、忽略敏感文件。
- 附带数据迁移工具，支持 MySQL/PostgreSQL/Redis/MongoDB/SQLite 与文件卷，并已在阿里云 K8s 完成 PostgreSQL 端到端验证。

**相关关键词**：运维工具、云计算运维、Kubernetes 运维、K8s 集群部署、DevOps 工具、SRE、自动化运维、Terraform、Kubekey、Docker Compose 迁移、阿里云运维、故障排查、SLB、Ingress。

> 本项目不是通用监控、日志、CMDB、堡垒机或备份平台；这些能力需要结合 Prometheus、Grafana、Loki、ELK、云监控等外部系统。

## 项目概览

### 工具组成

| 工具 | 作用 | 典型命令 |
|---|---|---|
| `k8slab.py` | 创建阿里云 VPC/ECS/安全组/SLB，安装 Kubekey 集群，并部署应用 | `k8slab.py up` |
| `course-selection/vm2k8s.py` | 把 Compose 转成 K8s 清单，部署到已有集群 | `vm2k8s.py ship` |
| `course-selection/datamigrate.py` | 迁移数据库和文件卷，独立于一键部署 | `datamigrate.py run` |

`k8slab` 内部会调用 `vm2k8s`；也可以只使用 `vm2k8s`，把应用部署到已有 kubeadm/K3s 集群。`datamigrate` 独立于部署流程；需要搬迁数据时使用，生产迁移需自行验证。

### 适用场景

- 已能通过 `docker compose up` 运行的前端 + API + 数据库/缓存项目。
- 无状态服务多副本，或单实例有状态服务（MySQL、PostgreSQL、Redis、MongoDB）。
- 内部工具、管理后台、官网、POC、课程和实验项目。
- 目标集群为已有 kubeadm/K3s 集群，或由本工具创建的阿里云集群。

### 不适用场景

- 依赖 host 网络、`privileged`、GPU/串口直通或 systemd 管理的项目。
- 需要 RWX 共享存储，或需要多个副本同时写同一数据目录。
- 大型微服务集群，或强依赖 Service Mesh、灰度、HPA、NetworkPolicy 的项目。
- 生产级数据库、消息队列；建议使用云数据库或 Operator。
- Windows 容器。
- 强依赖复杂启动顺序和初始化脚本的项目；需要改写为 initContainer/Job。

## 架构

![vmware-to-kubernetes 架构](docs/assets/architecture-v3.png)

### 集群拓扑

- 3 台 master 组成 etcd 三副本控制面，默认通过内网 SLB 暴露 API Server 6443。
- N 台 worker 运行工作负载；默认 3 台，可通过 `--workers` 调整。
- 默认安装 Calico CNI 和 local-path StorageClass。
- 默认使用 Docker 24 + cri-dockerd；如需 containerd，可参考相邻的 `k8s-iac` 项目。
- 节点、VPC、安全组、SSH 密钥、SLB 均由 Terraform 创建。
- `--no-ha` 可关闭 SLB，API 回退到 master1 单点。

## 功能特性

- 一条命令创建阿里云基础设施和 3 控制面 kubeadm HA 集群。
- Compose 通用转换：端口、环境变量、命令、卷、健康检查、资源限制、secrets/configs。
- 支持 `ports` 的 NodePort 暴露；可选 nginx Ingress。
- 镜像交付支持本机构建、离线镜像包和外部镜像仓库。
- 默认直接拉取镜像；已有现成仓库时用 `--registry` 指定，没有就什么都不用加。
- 自动生成 `06-deploy.sh`（apply + NodePort 分配）和 `MIGRATION.md`（服务表 + 风险告警）。
- 针对国内网络适配：Kubekey 国内镜像、节点镜像加速、local-path、SSH 重试、systemd 托管后台任务。
- 支持 `--depends-init` 将 Compose 启动依赖近似转换为 TCP 等待 initContainer。
- 支持 `--strict-bind`，避免目录型 bind mount 被静默跳过。
- 支持离线 CNI/local-path manifest 和 SHA256 校验。

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10+ / macOS / Linux |
| Python | 3.8+，依赖 `pyyaml` |
| SSH | `ssh`、`scp`、`ssh-keygen` |
| Terraform | 缺失时自动下载；也可通过 `TERRAFORM_PATH` 指定 |
| Docker | 可选，仅 `build` / `registry` 镜像模式需要 |
| 阿里云 | AK/SK；RAM 用户建议 `AliyunECSFullAccess` + `AliyunVPCFullAccess`；启用 SLB 时还需 `AliyunSLBFullAccess` |
| 网络 | 可访问阿里云 API；master 可访问 `registry.cn-beijing.aliyuncs.com` |
| 项目输入 | 可运行的 `docker-compose.yml` |

使用 `k8slab` 时本机不需要安装 kubectl 或 Kubekey，集群操作在 master 上执行；只用 `vm2k8s` 部署到已有集群时，需要本机 kubectl 或 SSH。

如果目标网络无法访问 GitHub 或 `registry.terraform.io`，可先导出带 provider 的离线工具包：

```bash
python k8slab.py export --out k8slab-kit.zip --with-provider
```

### 输入要求

- 工具以单个 `docker-compose.yml` 为输入，不解析裸进程、systemd unit 或 VM 镜像。
- 没有 compose 文件时，需要先根据 `docker run`、systemd、端口和挂载信息自行编写，并确保 `docker compose up` 能跑通。
- 转换器只生成部署清单和空 PVC。数据迁移不在主流程内；`datamigrate.py` 负责生成计划、脚本并执行迁移，生产环境仍需自行安排停机窗口、备份、校验与回滚。

## 快速开始：阿里云

### 1. 安装依赖并检查环境

```bash
python -m pip install pyyaml
python k8slab.py doctor
```

### 2. 创建集群并部署示例

没有现成镜像仓库时，直接拉取即可，不需要额外参数：

```bash
python k8slab.py up \
  --compose examples/library-app/docker-compose.yml \
  --ak <AccessKeyId> --sk <AccessKeySecret> \
  --nodeport-start 30111
```

如果已有现成镜像仓库（里面已经有 K8s 基础镜像），直接指定地址：

```bash
python k8slab.py up \
  --compose examples/library-app/docker-compose.yml \
  --ak <AccessKeyId> --sk <AccessKeySecret> \
  --registry registry.example.com/my-k8s \
  --registry-user <用户名> --registry-password <密码> \
  --nodeport-start 30111
```

仓库不需要认证时，去掉 `--registry-user` 和 `--registry-password`。只有希望业务镜像也推送到该仓库时，才需要加 `--image-mode registry`。

也可以使用环境变量提供凭证：

```bash
export ALICLOUD_ACCESS_KEY=<AccessKeyId>
export ALICLOUD_SECRET_KEY=<AccessKeySecret>
python k8slab.py up --compose examples/library-app/docker-compose.yml
```

该示例包含 `build` 段，默认的 `auto` 模式会在本机 Docker 中构建镜像；没有 Docker 时，请先准备 `images.tar.gz`，再加 `--image-mode save --bundle images.tar.gz`。

示例项目文件说明：

| 文件 | 是否必需 | 用途 |
|---|---|---|
| `examples/library-app/docker-compose.yml` | 是 | 工具的唯一输入 |
| `examples/library-app/app.env` | compose 引用 | `env_file` |
| `examples/library-app/backend/Dockerfile` | `--image-mode build` 时需要 | 构建后端镜像 |
| `examples/library-app/frontend/nginx.conf` | compose 引用 | 单文件 bind mount |
| `images.tar.gz` | `--image-mode save` 时需要 | 预导出的镜像包 |

### 3. 查看状态与销毁

```bash
python k8slab.py status
python k8slab.py destroy
```

运行目录默认是 `~/.k8slab/runs/<项目名>-<时间戳>`，里面有 `run.json`、`node_password.txt`、`kubekey-cluster.yaml` 和生成的 K8s 清单。不要分享整个运行目录。

### `up` 执行流程

1. Terraform 创建 VPC、安全组、SSH 密钥、3 台 master、N 台 worker，以及内网 SLB API 入口。
2. Kubekey 安装 kubeadm HA 集群（etcd 三副本 + Calico）。
3. 配置节点镜像加速、安装 local-path 默认 StorageClass。
4. `vm2k8s` 转换 Compose，构建或分发镜像。
5. apply 清单、分配 NodePort、等待滚动更新、执行 HTTP 验证。

## 数据迁移（datamigrate）

> 独立于 `k8slab up` 的迁移流程，已在阿里云 K8s 上完成端到端验证。生产迁移仍需结合停机窗口、备份、校验和回滚预案人工执行。

`course-selection/datamigrate.py` 根据 Compose 生成迁移计划，并在目标集群完成数据库/文件卷迁移，不保证零停机。

- `plan` 只分析 Compose 并生成计划，不连接源端或目标集群。
- `run` 按阶段执行，支持 `--dry-run`、`--resume`、`--only`、`--quiesce`。
- 数据库通过源容器/目标 Pod 内的客户端备份恢复；原始卷通过临时容器/Pod 归档解包。

### 1. 生成计划

```bash
python course-selection/datamigrate.py plan \
  --compose docker-compose.yml --out data-migration --namespace app \
  --source-ssh devops@192.168.110.128 --source-key keys/id_ed25519 \
  --source-dir /home/devops/apps/library-app \
  --target-ssh root@<master1> --target-key keys/id_ed25519
```

### 2. 预览

```bash
python course-selection/datamigrate.py run \
  --plan data-migration/migration-plan.json --phase all --dry-run
```

### 3. 分阶段执行

```bash
python course-selection/datamigrate.py run --plan data-migration/migration-plan.json --phase preflight
python course-selection/datamigrate.py run --plan data-migration/migration-plan.json --phase dump
python course-selection/datamigrate.py run --plan data-migration/migration-plan.json --phase transfer
python course-selection/datamigrate.py run --plan data-migration/migration-plan.json --phase restore --yes
python course-selection/datamigrate.py run --plan data-migration/migration-plan.json --phase verify
python course-selection/datamigrate.py run --plan data-migration/migration-plan.json --phase start
```

### 支持范围

| 数据 | 备份 | 恢复 |
|---|---|---|
| MySQL / MariaDB | `mysqldump` / `mariadb-dump` | `mysql` / `mariadb` 客户端导入 |
| PostgreSQL | `pg_dump` 纯 SQL | `psql` 导入 |
| Redis | `redis-cli --rdb` | RDB 恢复到 PVC（目标不要启用 AOF） |
| MongoDB | `mongodump --archive --gzip` | `mongorestore` |
| SQLite / 命名卷 | `tar.gz` 归档 | helper Pod 解包 |
| bind 目录 | `tar.gz` 归档 | 仅备份，不自动恢复 |

### 常用选项

| 选项 | 说明 |
|---|---|
| `--out` | 计划输出目录；默认 `data-migration/<namespace>` |
| `--namespace` | 目标命名空间；默认 Compose 目录名 |
| `--source-ssh` / `--target-ssh` | 源/目标 SSH；省略时分别使用本机 Docker/kubectl |
| `--source-project` | Compose 项目名；默认取 Compose 目录名 |
| `--source-dir` | 源项目目录；SSH 模式下解析 bind 路径 |
| `--target-dir` | 目标节点工件目录；默认 `~/datamigrate/<namespace>` |
| `--kubectl` | 目标端 kubectl 命令，默认 `kubectl` |
| `--include` / `--exclude` | 只迁移 / 排除指定服务（逗号分隔） |
| `--database-map` | 服务=数据库名，覆盖自动探测 |
| `--container-map` | 服务=容器名/ID，覆盖自动探测 |
| `--volume-map` | 源卷名映射，如 `mysql-data=myproj_mysql-data` |
| `--include-bind-dirs` | 把 bind 目录加入归档计划（仅备份） |
| `--ephemeral` | 目标使用 emptyDir；原始卷不恢复 |
| `--helper-image` | 原始卷恢复镜像；镜像缺少 `sh`/`tar` 时使用 |
| `--resume` | 跳过已完成的 dump |
| `--only` | 只执行指定 job ID 或服务（逗号分隔） |
| `--phase` | `preflight` / `dump` / `transfer` / `restore` / `verify` / `start` / `all` |
| `--yes` | 跳过 restore 前确认 |
| `--quiesce` / `--no-quiesce` | dump 时暂停源服务容器（默认关闭） |
| `--verify-rows` / `--no-verify-rows` | 行数校验（默认开启） |
| `--stop-apps` / `--no-stop-apps` | 恢复前缩容应用（默认开启） |
| `--clean-volume` / `--no-clean-volume` | 恢复前清空目标卷 |
| `--keep-helper` | 保留 helper Pod 便于排查 |

### 注意事项

- 未指定 `--source-ssh` / `--target-ssh` 时，分别使用本机 Docker 和 kubectl。
- plan 生成 `migration-plan.json`、`RUNBOOK.md`、`scripts/*.sh`、`scripts/*.ps1`；dump 阶段生成 `artifacts/`。
- 源端卷名默认按 `<项目名>_<卷名>` 和原名探测；特殊卷名使用 `--volume-map`。
- SSH 源端解析 bind 路径时必须提供 `--source-dir`；bind 目录只归档，不自动恢复。
- Redis 原始卷恢复需要目标 PVC；未检测到卷或使用 `--ephemeral` 时会告警并跳过原始恢复。
- SQLite 位于 bind 目录时不会自动转换，计划中会给出告警。
- 密码不写入计划：执行时在容器/Pod 内通过环境变量读取。
- `restore` 会先缩容非数据库服务（保留 MySQL/PostgreSQL/MongoDB），失败时保持停止；修复后执行 `verify`，再执行 `start`。
- SQLite 和原始卷恢复需要停止目标 Deployment，工具自动处理。
- 原始卷恢复默认复用服务镜像；distroless 等镜像缺少工具时加 `--helper-image busybox:1.36`。
- 生产迁移请先在预发环境演练，并保留可回滚备份；不要把本工具当作生产迁移的唯一保障。

## k8slab 使用手册

### 命令

| 命令 | 说明 |
|---|---|
| `python k8slab.py doctor` | 检查 Python、SSH、Terraform、Docker 和文件完整性 |
| `python k8slab.py up` | 创建阿里云资源、安装集群、部署应用 |
| `python k8slab.py status` | 查看集群、Pod、Service 和访问地址 |
| `python k8slab.py destroy` | 销毁资源，停止计费 |
| `python k8slab.py export` | 导出可离线使用的新电脑工具包 |

### 常用参数

| 参数 | 说明 |
|---|---|
| `--compose PATH` | 必填，Compose 文件路径 |
| `--project NAME` | 项目名，默认取 Compose 所在目录名 |
| `--namespace NAME` | 目标命名空间，默认由项目名生成 |
| `--ak` / `--sk` | 阿里云凭证；也可用 `ALICLOUD_ACCESS_KEY` / `ALICLOUD_SECRET_KEY` |
| `--region` | 地域，默认 `cn-guangzhou` |
| `--workers N` | worker 数量，默认 3 |
| `--password VALUE` | 节点 root 密码；不传则自动生成 |
| `--no-ha` | 不创建 SLB，API 回退到 master1（省费用） |
| `--nodeport-start N` | NodePort 起始端口，默认 30080 |
| `--image-mode MODE` | `auto` / `build` / `save` / `registry` |
| `--bundle PATH` | `save` 模式使用的镜像包 |
| `--registry ADDR[/NS]` | 已有镜像仓库；不填则直接拉取 |
| `--registry-user USER` | 仓库用户名；不需要认证时省略 |
| `--registry-password PASS` | 仓库密码；不需要认证时省略 |
| `--ingress` | 生成 nginx Ingress（可选） |
| `--workdir PATH` | 指定运行目录 |
| `--dry-run` | 只检查并展示计划，不创建资源 |

其余参数（网络 CIDR、超时、bind 校验、仓库 insecure 等）按需使用 `python k8slab.py up --help` 查看，不需要一开始就填。

### 镜像模式

| 模式 | 适用场景 | 行为 |
|---|---|---|
| `auto` | 默认 | 有 `--bundle` 用 `save`；有 `build` 且本机 Docker 可用用 `build`；否则直接拉取 |
| `build` | 含 `build` 段 | 本机 Docker 构建并在需要时导出镜像包 |
| `save` | 离线环境 | 使用 `--bundle` 指定的镜像包 |
| `registry` | 业务镜像也走仓库 | 配合 `--registry`，构建并推送，节点从仓库拉取 |

### 镜像仓库（可选）

**没有现成仓库（默认）**：不传 `--registry`，节点从公网或镜像加速拉取。

**已有现成仓库**（仓库里已有 K8s 基础镜像）：直接写地址；需要认证再带用户名密码。

```bash
python k8slab.py up \
  --compose examples/library-app/docker-compose.yml \
  --ak <AccessKeyId> --sk <AccessKeySecret> \
  --registry registry.example.com/my-k8s \
  --registry-user <用户名> --registry-password <密码> \
  --nodeport-start 30111
```

- ACR、Harbor 都可以，填控制台给的仓库地址即可。
- 地址格式是 `host/namespace`；不写 namespace 时默认 `kubesphereio`。
- 仓库里没有 K8s 基础镜像时，不要指定 `--registry`，按默认方式直接拉取。
- 只有业务镜像也要推送到仓库时，才加 `--image-mode registry`。
- 节点不能访问公网、必须用 master 做本地仓库时，属于高级场景，见 `--help` 的 `--private-registry`。

### 可选：Ingress

默认用 NodePort，不需要额外配置。需要域名访问时加 `--ingress`：

```bash
python k8slab.py up \
  --compose examples/library-app/docker-compose.yml \
  --ingress
```

- `k8slab` 默认使用 `<master1公网IP>.nip.io`，无需配置 DNS。
- 已有域名时加 `--ingress-host example.com`。
- 单独转换时写 `--ingress --ingress-host example.com`。
- 集群需要 ingress-controller；TLS、路径映射等高级参数见 `--help`。
## 部署到已有 K8s 集群

只做转换和部署时，用 `vm2k8s`。两种常用方式：

```bash
# 本机 kubectl 已指向目标集群
python course-selection/vm2k8s.py ship \
  --compose docker-compose.yml --namespace app

# 通过 SSH 部署到远程 master
python course-selection/vm2k8s.py ship \
  --compose docker-compose.yml --namespace app \
  --remote root@<master1> --ssh-key keys/id_ed25519 \
  --node-password '<节点密码>'
```

只生成清单、不部署：

```bash
python course-selection/vm2k8s.py convert \
  --compose docker-compose.yml --out k8s --namespace app
```

默认镜像模式是 `auto`；离线时用 `--image-mode save --bundle images.tar.gz`。生成物、镜像分发和高级参数见 `--help` 以及生成的 `MIGRATION.md`。

## Compose 转换规则

| Compose | Kubernetes |
|---|---|
| `image` / `build` | Deployment |
| `ports` / `expose` | Service；`ports` 分配 NodePort |
| `environment` / `env_file` | ConfigMap / Secret |
| named volume / 单文件 bind | PVC / ConfigMap |
| `healthcheck` | HTTP 或 TCP 探针 |
| `depends_on` | 默认忽略；`--depends-init` 等待 TCP |

主要限制：

- 只支持单个 Compose 文件；`profiles` 不展开。
- `expose` 不生成 NodePort；目录型 bind 默认跳过，`--strict-bind` 可改为直接失败。
- named volume 使用 local-path 时为单节点 RWO，应用保持单副本。
- `--depends-init` 只等 TCP 端口，不等业务健康检查。
- 固定 Kubernetes 1.28.2 和 Docker 24 + cri-dockerd；生产环境应升级并改用 containerd。
- `datamigrate` 默认需要停机窗口，恢复会覆盖同名对象，不做增量同步。

## 示例项目

`examples/` 下提供四个可直接运行的全栈示例：

| 项目 | 组件 | 演示点 |
|---|---|---|
| `todo-app` | nginx + FastAPI + SQLite | PVC 持久化 |
| `weather-dashboard` | nginx + FastAPI | 基础 Web 服务 |
| `url-shortener` | nginx + FastAPI + Redis | Redis 密码、env 注入 |
| `library-app` | nginx + FastAPI + PostgreSQL | bind 配置、HTTP 探针、resources、Secret |

`course-selection/` 是选课系统源码（FastAPI + MySQL + Redis Lua），附带 1000 并发抢课压测脚本。

## 运维与安全

查看状态和销毁资源：

```bash
python k8slab.py status
python k8slab.py status --run "<workdir>"
python k8slab.py destroy --run "<workdir>"
```

安全默认值：

- `k8slab` 自动生成节点密码，写入权限 600 的 `node_password.txt`。
- `terraform.tfvars`、`keys/`、`*.tfstate`、`node_password.txt`、`backend.tf` 已在 `.gitignore`。
- SSH/NodePort 默认只允许本机公网 IP，6443 不对公网开放。
- 不要分享整个运行目录；`kubekey-cluster.yaml` 包含节点密码。

需要 OSS 远程 state 或手动部署时，参考 `backend.tf.example` 和 `deploy.ps1`。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| Kubekey 卡住 | `dl.k8s.io` 不可达 | 安装脚本固定 `KKZONE=cn` |
| SLB 后端健康检查失败 | 安全组未放行 VPC 内 6443，或 SLB 与 ECS 不在同一 VPC | 检查 `intra` 规则；`terraform output api_endpoint` 应为 SLB 地址 |
| PVC Pending | kubeadm 无默认 StorageClass | 自动安装 local-path 并设为默认 |
| 节点拉取镜像失败 | Docker Hub 不可达 | 配置节点镜像加速，或使用离线镜像包 |
| local-path 拉取失败 | Docker Hub 偶发不通 | bootstrap 自动预拉取重试；仍失败时离线导入 |
| Redis 启动失败 | 未复制 command/env，或缺少密码参数 | 检查 `command`/`args` 和 Secret 注入 |
| SSH 中断导致安装失败 | 前后台进程绑定 | Kubekey 由 systemd 托管，SSH 操作自动重试 |
| bind 目录没有进入清单 | 目录型 bind 默认跳过 | 使用 `--strict-bind` 提前失败，或手工改为 ConfigMap/PVC |
| depends_on 未等待依赖 | 默认不建模启动顺序 | 加 `--depends-init`，或让应用自行重试连接 |

## 测试与验证

```bash
python course-selection/tests/run_fixtures.py
python tests/test_datamigrate.py
python course-selection/tests/test_k8slab.py
```

四个示例已在 VMware k3s 和阿里云 K8s 完成部署、读写和 PVC 持久化验证；数据迁移已在阿里云完成 PostgreSQL 端到端验证。

### 数据迁移上云验证（阿里云 K8s）

- 源：VMware 上的 `library-app` PostgreSQL（4 本书）
- 目标：阿里云 3 主 3 从 K8s 新集群，`library` 命名空间
- 流程：`plan → preflight → dump → transfer → restore → verify → start` 全部通过
- 结果：目标页面 200，`/api/stats` total=4，源/目标 `books` 均为 4

## 目录结构

```text
.
├── k8slab.py                     # 集群编排入口
├── main.tf / variables.tf / outputs.tf
├── scripts/                      # 安装、镜像、Ingress 脚本
├── course-selection/
│   ├── vm2k8s.py                 # Compose → K8s
│   ├── datamigrate.py            # 数据迁移
│   └── tests/                    # 转换器与 k8slab 测试
├── tests/                        # datamigrate 测试
├── examples/                     # 四个全栈示例
└── docs/assets/                  # 架构图
```

运行目录（`~/.k8slab/runs/`）、`keys/`、`data-migration/`、`*-k8s/`、`terraform.tfstate` 和 `node_password.txt` 默认不提交。

## License

MIT
