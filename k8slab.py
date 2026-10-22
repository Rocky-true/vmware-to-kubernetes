#!/usr/bin/env python3
"""
k8slab —— 阿里云 Kubernetes 迁移实验室一体化工具（全新电脑可用）

一条命令完成：
  创建 VPC/ECS → 安装 kubeadm HA 集群 → 配置镜像加速/存储类
  → vm2k8s 转换 compose → 镜像构建/分发 → 部署 → HTTP 验证

快速开始（全新电脑）：
  1. 安装 Python 3.8+、OpenSSH（ssh/scp/ssh-keygen）
  2. python k8slab.py doctor
  3. python k8slab.py up --compose examples/todo-app/docker-compose.yml \
         --ak <AccessKeyId> --sk <AccessKeySecret>
  4. python k8slab.py destroy --run <workdir>   # 验证完及时销毁

其他命令：
  status   查看运行环境
  export   打包成可在全新电脑使用的 zip 工具包
"""
import argparse
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = Path.home() / ".k8slab"
TF_VERSION = "1.9.8"
HTTP_PORTS = (80, 3000, 5000, 8000, 8080, 8081, 8888)
K8S_VERSION = "v1.28.2"
KK_VERSION = "v3.1.10"


# ---------------- 基础 ----------------

def log(msg):
    print(msg, flush=True)


def die(msg):
    print("\n[ERROR] " + msg, file=sys.stderr, flush=True)
    sys.exit(1)


def shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def run(cmd, cwd=None, env=None, timeout=None, capture=False, check=True,
        quiet=False, allow_timeout=False):
    printable = " ".join(str(c) for c in cmd)
    printable = re.sub(r"(-var node_password=)\S+", r"\1***", printable)
    printable = re.sub(r"(NODE_PASSWORD=)'[^']*'", r"\1'***'", printable)
    printable = re.sub(r"(--node-password\s+)\S+", r"\1***", printable)
    printable = re.sub(r"(--password\s+)\S+", r"\1***", printable)
    if not quiet:
        log("+ " + printable)
    try:
        r = subprocess.run(
            cmd, cwd=cwd, env=env, timeout=timeout, text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError:
        die("找不到命令: %s" % cmd[0])
    except subprocess.TimeoutExpired:
        if allow_timeout:
            return subprocess.CompletedProcess(cmd, 1, "", "timeout")
        die("命令超时: %s" % printable)
    if check and r.returncode != 0:
        if capture:
            log((r.stdout or "")[-3000:])
            log((r.stderr or "")[-3000:])
        die("命令失败(exit=%d): %s" % (r.returncode, printable))
    return r


def ensure_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        log("缺少 pyyaml，正在安装 ...")
        r = run([sys.executable, "-m", "pip", "install", "pyyaml"],
                check=False, capture=True, quiet=True)
        if r.returncode != 0:
            die("pyyaml 安装失败，请手动执行: %s -m pip install pyyaml"
                % sys.executable)
        import yaml
        return yaml


def which(name):
    p = shutil.which(name)
    if p:
        return p
    if os.name == "nt" and not name.endswith(".exe"):
        return shutil.which(name + ".exe")
    return None


def sanitize_name(s, maxlen=20):
    s = re.sub(r"[^a-z0-9-]", "-", str(s).lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s or "app")[:maxlen]


# ---------------- terraform ----------------

def tf_platform():
    osname = {"windows": "windows", "linux": "linux",
              "darwin": "darwin"}.get(platform.system().lower())
    if not osname:
        die("不支持的系统: %s" % platform.system())
    arch = platform.machine().lower()
    arch = "arm64" if arch in ("arm64", "aarch64") else "amd64"
    return osname, arch


def detect_public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                ip = r.read().decode("utf-8", "replace").strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                return ip
        except Exception:
            continue
    return ""


def download_terraform():
    osname, arch = tf_platform()
    exe = "terraform.exe" if osname == "windows" else "terraform"
    dest = CACHE / "bin" / exe
    if dest.is_file():
        return str(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    urls = [
        "https://releases.hashicorp.com/terraform/%s/terraform_%s_%s_%s.zip"
        % (TF_VERSION, TF_VERSION, osname, arch),
        "https://mirrors.huaweicloud.com/terraform/%s/terraform_%s_%s_%s.zip"
        % (TF_VERSION, TF_VERSION, osname, arch),
    ]
    zip_path = dest.parent / "terraform.zip"
    last_err = None
    for url in urls:
        log("下载 Terraform: %s" % url)
        try:
            with urllib.request.urlopen(url, timeout=60) as r, \
                    open(zip_path, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total and done % (1024 * 1024 * 10) < 262144:
                        log("  %d%% (%d/%d MB)"
                            % (done * 100 // total, done // 1048576,
                               total // 1048576))
            with zipfile.ZipFile(zip_path) as z:
                z.extract(exe, dest.parent)
            zip_path.unlink()
            if osname != "windows":
                os.chmod(dest, 0o755)
            log("[OK] Terraform 已安装: %s" % dest)
            return str(dest)
        except Exception as e:
            last_err = e
            log("[WARN] 下载失败: %s" % e)
    die("Terraform 自动下载失败（%s）。可手动下载后设置环境变量 "
        "TERRAFORM_PATH 指向 terraform 可执行文件。" % last_err)


def find_terraform():
    for c in (os.environ.get("TERRAFORM_PATH"), which("terraform"),
              str(CACHE / "bin" / ("terraform.exe" if os.name == "nt"
                                   else "terraform"))):
        if c and Path(c).is_file():
            return str(c)
    return download_terraform()


def tf_env(ak, sk):
    env = os.environ.copy()
    env["ALICLOUD_ACCESS_KEY"] = ak
    env["ALICLOUD_SECRET_KEY"] = sk
    env["TF_IN_AUTOMATION"] = "1"
    return env


def tf_outputs(tf, workdir, env):
    r = run([tf, "output", "-json", "-no-color"], cwd=workdir, env=env,
            capture=True, timeout=120, quiet=True)
    data = json.loads(r.stdout or "{}")
    out = {}
    for k, v in data.items():
        out[k] = v.get("value") if isinstance(v, dict) and "value" in v else v
    return out


# ---------------- ssh/scp ----------------

def ssh_argv(host, key):
    return ["ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            "-i", key, host]


def scp_argv(key):
    return ["scp", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
            "-i", key]


def ssh_run(host, key, cmd, timeout=None, capture=False, check=True,
            quiet=False, allow_timeout=False):
    return run(ssh_argv(host, key) + [cmd], timeout=timeout,
               capture=capture, check=check, quiet=quiet,
               allow_timeout=allow_timeout)


def scp_file(local, host, dest, key):
    run(scp_argv(key) + [Path(local).name, "%s:%s" % (host, dest)],
        cwd=str(Path(local).resolve().parent), timeout=1800)


# ---------------- 工作目录/密钥 ----------------

def wait_ssh(host, key, timeout=300):
    log("==> 等待节点 SSH 就绪（首次开机约30-90秒）")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = ssh_run(host, key, "echo SSH_OK", timeout=20, capture=True,
                    check=False, quiet=True, allow_timeout=True)
        if "SSH_OK" in (r.stdout or ""):
            log("[OK] SSH 已就绪")
            return
        time.sleep(5)
    die("SSH 在 %ds 内未就绪，请检查安全组(22端口)与实例状态" % timeout)


def existing_key_pair_name(workdir):
    p = Path(workdir) / "terraform.tfstate"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    for res in data.get("resources", []):
        if res.get("type") == "alicloud_key_pair":
            for inst in res.get("instances", []):
                name = (inst.get("attributes") or {}).get("key_pair_name")
                if name:
                    return name
    return None


def prepare_workdir(workdir):
    required = ["main.tf", "variables.tf", "outputs.tf",
                "cloud-init/common-init.yaml",
                "scripts/install-kk.sh", "scripts/bootstrap-cluster.sh",
                "scripts/distribute-images.sh",
                "course-selection/vm2k8s.py",
                "course-selection/datamigrate.py"]
    for f in required:
        src = BASE / f
        if not src.is_file():
            die("工具包缺少文件: %s（请从完整仓库或 zip 工具包运行）" % f)
        dst = workdir / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for f in ("README.md", ".terraform.lock.hcl", "terraform.tfvars.example"):
        src = BASE / f
        if src.is_file():
            shutil.copy2(src, workdir / f)
    # 复用本机已下载的 provider（避免 GitHub/registry 不可达导致 init 失败）
    tf_dir = BASE / ".terraform"
    if tf_dir.is_dir() and not (workdir / ".terraform").is_dir():
        shutil.copytree(tf_dir, workdir / ".terraform")


def gen_key(workdir):
    key = workdir / "id_ed25519"
    if key.is_file() and Path(str(key) + ".pub").is_file():
        return key
    if not which("ssh-keygen"):
        die("缺少 ssh-keygen（Windows 可在“可选功能”中安装 OpenSSH 客户端）")
    run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "k8slab",
         "-f", str(key)], check=True, capture=True, quiet=True)
    return key


def render_kubekey(workdir, outputs, password, private_registry=None):
    yaml = ensure_yaml()
    masters = ["master%d" % (i + 1)
               for i in range(len(outputs["master_private_ips"]))]
    workers = ["worker%d" % (i + 1)
               for i in range(len(outputs["worker_private_ips"]))]
    hosts = []
    for name, ip in zip(masters, outputs["master_private_ips"]):
        hosts.append({"name": name, "address": ip, "internalAddress": ip,
                      "user": "root", "password": password})
    for name, ip in zip(workers, outputs["worker_private_ips"]):
        hosts.append({"name": name, "address": ip, "internalAddress": ip,
                      "user": "root", "password": password})
    cfg = {
        "apiVersion": "kubekey.kubesphere.io/v1alpha2",
        "kind": "Cluster",
        "metadata": {"name": "k8slab"},
        "spec": {
            "hosts": hosts,
            "roleGroups": (
                {"etcd": masters, "control-plane": masters,
                 "worker": workers, "registry": [masters[0]]}
                if private_registry else
                {"etcd": masters, "control-plane": masters,
                 "worker": workers}),
            "controlPlaneEndpoint": {
                "domain": "lb.kubesphere.local",
                "address": (outputs.get("api_endpoint")
                            or outputs["master_private_ips"][0]),
                "port": 6443,
            },
            "kubernetes": {"version": K8S_VERSION, "clusterName": "k8slab",
                           "autoRenewCerts": True},
            "etcd": {"type": "kubekey"},
            "network": {"plugin": "calico",
                        "kubePodsCIDR": "10.233.64.0/18",
                        "kubeServiceCIDR": "10.233.0.0/18"},
            "registry": (
                {"privateRegistry": private_registry,
                 "namespaceOverride": "kubesphereio",
                 "insecureRegistries": []}
                if private_registry else {"privateRegistry": ""}),
        },
    }
    path = workdir / "kubekey-cluster.yaml"

    class NoAliasDumper(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        yaml.dump(cfg, f, Dumper=NoAliasDumper, allow_unicode=True,
                  sort_keys=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ---------------- 集群 ----------------

def install_kk(host, key):
    log("==> 安装 Kubekey（国内镜像）")
    scp_file(BASE / "scripts" / "install-kk.sh", host,
             "/root/install-kk.sh", key)
    ssh_run(host, key, "bash /root/install-kk.sh", timeout=600)


def init_registry(host, key, kk_cfg):
    log("==> 初始化 master 本地镜像仓库（registry:443/TLS，节点从master拉取）")
    scp_file(kk_cfg, host, "/root/kubekey-cluster.yaml", key)
    ssh_run(host, key,
            "systemctl stop kk-install 2>/dev/null || true; "
            "pkill -x kk || true; sleep 2; "
            "cd /root && ./kk init registry -f /root/kubekey-cluster.yaml -y",
            timeout=900)


def ensure_docker(host, key):
    r = ssh_run(host, key,
                "command -v docker >/dev/null 2>&1 && docker info >/dev/null "
                "2>&1 && echo DOCKER_OK || echo NO_DOCKER",
                capture=True, check=False, quiet=True, timeout=60,
                allow_timeout=True)
    if "DOCKER_OK" in (r.stdout or ""):
        return
    log("==> master 安装 Docker（用于推送镜像到本地 registry）")
    ssh_run(host, key,
            "apt-get update -y >/dev/null 2>&1; "
            "DEBIAN_FRONTEND=noninteractive apt-get "
            "-o DPkg::Lock::Timeout=300 install -y docker.io "
            ">/dev/null 2>&1; "
            "systemctl enable --now docker >/dev/null 2>&1 || true; "
            "docker info >/dev/null 2>&1 && echo DOCKER_READY "
            "|| echo DOCKER_FAILED",
            timeout=1200)


def registry_has_images(host, key, addr):
    r = ssh_run(host, key, "curl -ks https://%s/v2/_catalog || true" % addr,
                capture=True, check=False, quiet=True, timeout=30,
                allow_timeout=True)
    return "kubesphereio/pause" in (r.stdout or "")


def drop_incompatible_docker(host, key):
    r = ssh_run(host, key,
                "v=$(docker version --format '{{.Server.Version}}' "
                "2>/dev/null || echo none); echo VER=$v",
                capture=True, check=False, quiet=True, timeout=60,
                allow_timeout=True)
    m = re.search(r"VER=(\d+)\.", r.stdout or "")
    if not m or int(m.group(1)) < 25:
        return
    log("==> 移除 Docker %s（与 cri-dockerd 不兼容），交由 Kubekey 安装 24.0.9"
        % m.group(1))
    ssh_run(host, key,
            "systemctl stop docker docker.socket cri-docker "
            "cri-docker.socket 2>/dev/null || true; "
            "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 "
            "purge -y docker.io containerd >/dev/null 2>&1 || true; "
            "rm -f /usr/bin/docker /usr/bin/dockerd; echo DOCKER_REMOVED",
            timeout=900)


def push_k8s_images(host, key, registry_addr):
    log("==> 推送 K8s 基础镜像到 master registry（节点从master拉取）")
    scp_file(BASE / "scripts" / "push-k8s-images.sh", host,
             "/root/push-k8s-images.sh", key)
    ssh_run(host, key,
            "REG=%s bash /root/push-k8s-images.sh" % shq(registry_addr),
            timeout=2400)


def start_kk(host, key, kk_cfg, workdir):
    scp_file(kk_cfg, host, "/root/kubekey-cluster.yaml", key)
    ssh_run(host, key, "pkill -x kk || true", timeout=30, check=False)
    log("==> 后台启动集群安装（systemd托管，防SSH断开/连接挂起）")
    cmd = ("systemctl reset-failed kk-install 2>/dev/null || true; "
           "systemd-run --unit=kk-install --collect --setenv=KKZONE=cn "
           "bash -c '/root/kk create cluster -f /root/kubekey-cluster.yaml -y "
           "> /root/kk-run.log 2>&1' >/dev/null && echo KK_STARTED")
    ssh_run(host, key, cmd, timeout=60)


def wait_cluster(host, key, total_nodes, timeout):
    log("==> 等待集群就绪（首次约20-40分钟，镜像下载取决于带宽）")
    deadline = time.time() + timeout
    last_log = 0
    stopped = 0
    while time.time() < deadline:
        r = ssh_run(
            host, key,
            "if [ -f /root/.kube/config ]; then kubectl get nodes --no-headers "
            "2>/dev/null; else pgrep -x kk >/dev/null && echo __RUNNING__ || "
            "echo __STOPPED__; fi",
            timeout=60, capture=True, check=False, quiet=True,
            allow_timeout=True)
        out = (r.stdout or "").strip()
        if "__RUNNING__" in out:
            stopped = 0
            status = "安装中（下载镜像/初始化控制面）"
        elif "__STOPPED__" in out:
            stopped += 1
            status = "安装进程未运行（确认中 %d/3）" % stopped
            if stopped >= 3:
                tail = ssh_run(host, key, "tail -c 1500 /root/kk-run.log",
                               timeout=60, capture=True, check=False,
                               quiet=True, allow_timeout=True).stdout
                die("Kubekey 异常退出，请检查日志：\n%s" % tail)
        else:
            stopped = 0
            lines = [l for l in out.splitlines() if l.strip()]
            ready = [l for l in lines if re.search(r"\sReady\s", l)]
            status = "节点 Ready %d/%d" % (len(ready), total_nodes)
            if len(lines) >= total_nodes and len(ready) >= total_nodes:
                log("[OK] 集群就绪:")
                for l in lines:
                    log("   " + l)
                return
        if time.time() - last_log > 30:
            log("   " + status)
            last_log = time.time()
        time.sleep(15)
    die("等待集群超时（%ds）。可稍后用 status 查看；资源仍在计费。"
        % timeout)


def bootstrap_cluster(host, key, password):
    log("==> 集群引导（镜像加速 / CNI检查 / local-path存储类）")
    scp_file(BASE / "scripts" / "bootstrap-cluster.sh", host,
             "/root/bootstrap-cluster.sh", key)
    ssh_run(host, key,
            "NODE_PASSWORD=%s bash /root/bootstrap-cluster.sh" % shq(password),
            timeout=1500)


# ---------------- 应用部署 ----------------

def parse_compose(path, yaml):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def compose_has_build(compose):
    return any("build" in s for s in (compose.get("services") or {}).values())


def docker_ok():
    if not which("docker"):
        return False
    r = run(["docker", "info"], capture=True, check=False, quiet=True,
            timeout=30)
    return r.returncode == 0


def resolve_image_mode(args, compose):
    if args.image_mode != "auto":
        return args.image_mode
    if args.bundle:
        return "save"
    if compose_has_build(compose):
        if docker_ok():
            return "build"
        die("项目包含 build 段，但本机没有可用 Docker。\n"
            "  方案A：安装并启动 Docker Desktop 后重试\n"
            "  方案B：在有 Docker 的机器执行 docker compose build 并 "
            "docker save -o images.tar.gz <镜像...>，然后用 --bundle images.tar.gz")
    return "none"


def compute_urls(compose, host, nodeport_start):
    urls = []
    np = nodeport_start
    for name, svc in (compose.get("services") or {}).items():
        for p in svc.get("ports", []) or []:
            try:
                if isinstance(p, dict):
                    target = int(p.get("target"))
                else:
                    last = str(p).split(":")[-1].split("/")[0]
                    target = int(last)
            except (TypeError, ValueError):
                continue
            if target in HTTP_PORTS:
                urls.append((name, "http://%s:%d" % (host, np)))
            np += 1
    return urls


def deploy_app(args, compose, workdir, master1, key, password):
    mode = resolve_image_mode(args, compose)
    ns = args.namespace
    cmd = [
        sys.executable, str(workdir / "course-selection" / "vm2k8s.py"),
        "ship",
        "--compose", str(args.compose),
        "--namespace", ns,
        "--out", str(workdir / "app-k8s"),
        "--image-mode", mode,
        "--remote", "root@%s" % master1,
        "--ssh-key", str(key),
        "--node-password", password,
        "--nodeport-start", str(args.nodeport_start),
        "--rollout-timeout", str(args.rollout_timeout),
    ]
    if args.ephemeral:
        cmd.append("--ephemeral")
    if args.ingress:
        cmd += ["--ingress", "--ingress-host", args.ingress_host,
                "--ingress-class", args.ingress_class]
        if args.ingress_map:
            cmd += ["--ingress-map", args.ingress_map]
        if args.ingress_tls_secret:
            cmd += ["--ingress-tls-secret", args.ingress_tls_secret]
    if args.verify:
        cmd += ["--verify", args.verify]
    if args.strict_bind:
        cmd.append("--strict-bind")
    if args.depends_init:
        cmd += ["--depends-init", "--init-image", args.init_image]
    if mode == "save":
        cmd += ["--bundle", str(Path(args.bundle).resolve())]
    if mode == "registry":
        cmd += ["--registry", args.registry]
    env = os.environ.copy()
    env["DOCKER_DEFAULT_PLATFORM"] = "linux/amd64"
    log("==> 迁移并部署（镜像模式: %s）" % mode)
    run(cmd, cwd=str(workdir), env=env, timeout=args.deploy_timeout)


# ---------------- run 元数据 ----------------

def save_run(workdir, meta):
    with open(workdir / "run.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def load_run(run):
    if run:
        p = Path(run)
        if p.is_dir():
            rj = p / "run.json"
            if rj.is_file():
                with open(rj, "r", encoding="utf-8") as f:
                    return json.load(f)
            if (p / "terraform.tfstate").is_file():
                return {"workdir": str(p), "project": p.name}
            die("目录里既没有 run.json 也没有 terraform.tfstate: %s" % p)
        if not p.is_file():
            die("找不到运行记录: %s" % p)
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    runs = CACHE / "runs"
    cands = sorted(runs.glob("*/run.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True) if runs.is_dir() else []
    if not cands:
        die("没有找到任何运行记录，请用 --run 指定 workdir")
    with open(cands[0], "r", encoding="utf-8") as f:
        return json.load(f)


def get_creds(args):
    ak = getattr(args, "ak", None) or os.environ.get("ALICLOUD_ACCESS_KEY")
    sk = getattr(args, "sk", None) or os.environ.get("ALICLOUD_SECRET_KEY")
    if not ak or not sk:
        die("需要阿里云凭据：--ak/--sk 或环境变量 "
            "ALICLOUD_ACCESS_KEY / ALICLOUD_SECRET_KEY")
    return ak, sk


# ---------------- 命令 ----------------

def cmd_doctor(args):
    log("== k8slab doctor ==")
    ok = True

    def check(name, good, detail=""):
        nonlocal ok
        log("[%s] %s%s" % ("OK" if good else "MISS", name,
                           (" - " + detail) if detail else ""))
        if not good:
            ok = False

    check("Python >= 3.8", sys.version_info >= (3, 8),
          sys.version.split()[0])
    try:
        import yaml  # noqa: F401
        check("pyyaml", True)
    except ImportError:
        check("pyyaml", False, "python -m pip install pyyaml（up 会自动安装）")
    for tool in ("ssh", "scp", "ssh-keygen"):
        check(tool, bool(which(tool)))
    tf = None
    tf_found = False
    for c in (os.environ.get("TERRAFORM_PATH"), which("terraform"),
              str(CACHE / "bin" / ("terraform.exe" if os.name == "nt"
                                   else "terraform"))):
        if c and Path(c).is_file():
            tf = c
            tf_found = True
            break
    if not tf_found and args.download:
        tf = find_terraform()
        tf_found = True
    if tf_found:
        check("terraform", True, tf)
    else:
        log("[INFO] terraform - 未安装（up 时自动下载，或设置 TERRAFORM_PATH）")
    docker_bin = which("docker")
    if docker_bin:
        log("[OK] docker（可选用，build/registry 模式需要） - %s" % docker_bin)
    else:
        log("[INFO] docker - 未安装（仅 build/registry 镜像模式需要）")
    ak = args.ak or os.environ.get("ALICLOUD_ACCESS_KEY")
    sk = args.sk or os.environ.get("ALICLOUD_SECRET_KEY")
    if ak and sk:
        log("[OK] 阿里云 AK/SK - 已提供")
    else:
        log("[INFO] 阿里云 AK/SK - up 时用 --ak/--sk 或环境变量传入")
    log("")
    log("下一步:")
    log("  python k8slab.py up --compose examples/todo-app/docker-compose.yml "
        "--ak <AK> --sk <SK>")
    return 0


def cmd_up(args):
    yaml = ensure_yaml()
    ak, sk = get_creds(args)
    for tool in ("ssh", "scp", "ssh-keygen"):
        if not which(tool):
            die("缺少 %s（Windows 安装 OpenSSH 客户端后重试）" % tool)

    compose_path = Path(args.compose).resolve()
    if not compose_path.is_file():
        die("compose 文件不存在: %s" % compose_path)
    compose = parse_compose(compose_path, yaml)

    project = sanitize_name(args.project or compose_path.parent.name)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = "k8slab-%s" % project
    workdir = Path(args.workdir).resolve() if args.workdir else \
        CACHE / "runs" / ("%s-%s" % (project, ts))
    workdir.mkdir(parents=True, exist_ok=True)

    password = args.password or ("K8sLab@" + secrets.token_hex(4))
    pw_file = workdir / "node_password.txt"
    if not args.password and pw_file.is_file():
        saved = pw_file.read_text(encoding="utf-8").strip()
        if saved:
            password = saved
    pw_file.write_text(password, encoding="utf-8")
    try:
        os.chmod(pw_file, 0o600)
    except OSError:
        pass

    admin_cidr = (args.admin_cidr or "").strip()
    if not admin_cidr:
        ip = detect_public_ip()
        if not ip:
            die("无法自动检测公网 IP；请用 --admin-cidr <你的IP>/32")
        admin_cidr = ip + "/32"
        log("自动检测公网 IP: %s" % admin_cidr)
    if "/" not in admin_cidr:
        admin_cidr += "/32"
    nodeport_cidr = (args.nodeport_cidr or "").strip() or admin_cidr
    if "/" not in nodeport_cidr:
        nodeport_cidr += "/32"
    web_cidr = (args.web_cidr or "").strip()

    params_file = workdir / "k8slab-params.json"
    params = {}
    if params_file.is_file():
        try:
            params = json.loads(params_file.read_text(encoding="utf-8"))
        except Exception:
            params = {}
    key_pair_name = (params.get("key_pair_name")
                     or existing_key_pair_name(workdir)
                     or "%s-key-%s" % (prefix, ts))
    params.update({"project": project, "region": args.region,
                   "key_pair_name": key_pair_name})
    params_file.write_text(json.dumps(params, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    log("== [1/7] 准备工具与环境 (%s)" % workdir)
    prepare_workdir(workdir)
    key = gen_key(workdir)
    tf = find_terraform()

    namespace = sanitize_name(args.namespace or project)
    nodeport_start = args.nodeport_start
    urls = compute_urls(compose, "MASTER_IP", nodeport_start)

    tfvars = [
        "-var", "region=%s" % args.region,
        "-var", "worker_count=%d" % args.workers,
        "-var", "node_password=%s" % password,
        "-var", "ssh_public_key_path=%s" % Path(str(key) + ".pub").as_posix(),
        "-var", "name_prefix=%s" % prefix,
        "-var", "key_pair_name=%s" % key_pair_name,
        "-var", "bandwidth_out=%d" % args.bandwidth,
        "-var", "admin_cidr=%s" % admin_cidr,
        "-var", "nodeport_cidr=%s" % nodeport_cidr,
        "-var", "web_cidr=%s" % web_cidr,
        "-var", "enable_ha_slb=%s" % ("false" if args.no_ha else "true"),
    ]
    env = tf_env(ak, sk)

    if args.dry_run:
        log("[dry-run] 工作目录已准备完成: %s" % workdir)
        log("[dry-run] 将执行:")
        log("  %s init" % tf)
        log("  %s apply -auto-approve %s" % (tf, " ".join(tfvars)))
        log("  上传脚本 → 安装Kubekey → 等待集群 → bootstrap → ship")
        return 0

    applied = False
    try:
        log("== [2/7] 创建阿里云资源（VPC/ECS/安全组/密钥）")
        run([tf, "init", "-input=false", "-no-color"], cwd=str(workdir),
            env=env, timeout=1800)
        run([tf, "apply", "-auto-approve", "-input=false", "-no-color"]
            + tfvars, cwd=str(workdir), env=env, timeout=1800)
        applied = True
        outputs = tf_outputs(tf, workdir, env)
        master1 = outputs["master1_public_ip"]
        master_priv = outputs["master_private_ips"]
        worker_priv = outputs["worker_private_ips"]
        total_nodes = len(master_priv) + len(worker_priv)
        log("[OK] 资源创建完成")
        log("  master1 公网: %s" % master1)
        log("  API 入口: %s" % (outputs.get("api_endpoint") or master_priv[0]))
        log("  SSH/NodePort 来源: %s" % admin_cidr)
        log("  masters 私网: %s" % ", ".join(master_priv))
        log("  workers 私网: %s" % ", ".join(worker_priv))
        if args.ingress and not args.ingress_host:
            args.ingress_host = "%s.nip.io" % master1
            log("  Ingress 基础域名: %s" % args.ingress_host)

        host = "root@%s" % master1
        wait_ssh(host, str(key))
        log("== [3/7] 安装 Kubekey")
        install_kk(host, str(key))

        kk_cfg = render_kubekey(
            workdir, outputs, password,
            private_registry=master_priv[0] if args.private_registry else None)
        if args.private_registry:
            log("== [4/7] 初始化 master 本地镜像仓库")
            init_registry(host, str(key), kk_cfg)
            if registry_has_images(host, str(key), master_priv[0]):
                log("[OK] registry 已有K8s镜像，跳过推送")
            else:
                ensure_docker(host, str(key))
                push_k8s_images(host, str(key), master_priv[0])
            drop_incompatible_docker(host, str(key))
        log("== [4/7] 创建集群（3控制面 HA + %d worker）" % args.workers)
        start_kk(host, str(key), kk_cfg, workdir)
        wait_cluster(host, str(key), total_nodes, args.cluster_timeout)

        log("== [5/7] 集群引导")
        bootstrap_cluster(host, str(key), password)

        log("== [6/7] 部署应用")
        deploy_app(args, compose, workdir, master1, key, password)
        if args.ingress:
            log("Ingress 已生成；请确保集群已安装 ingress-nginx 且 80/443 已放行")

        urls = [(n, u.replace("MASTER_IP", master1)) for n, u in urls]
        meta = {
            "project": project,
            "namespace": namespace,
            "compose": str(compose_path),
            "workdir": str(workdir),
            "created": ts,
            "region": args.region,
            "master1": master1,
            "master_public_ips": outputs["master_public_ips"],
            "worker_public_ips": outputs["worker_public_ips"],
            "nodeport_start": nodeport_start,
            "urls": [{"service": n, "url": u} for n, u in urls],
        }
        save_run(workdir, meta)

        log("== [7/7] 完成")
        if urls:
            log("访问地址:")
            for n, u in urls:
                log("  %-12s %s" % (n, u))
        log("节点密码: %s" % (workdir / "node_password.txt"))
        log("环境仍在计费，验证完请执行:")
        log('  python "%s" destroy --run "%s"'
            % (Path(__file__).name, workdir))
        return 0

    except SystemExit:
        if applied and args.destroy_on_error:
            log("[WARN] 部署失败，按 --destroy-on-error 销毁资源 ...")
            run([tf, "destroy", "-auto-approve", "-input=false", "-no-color"],
                cwd=str(workdir), env=env, timeout=1800, check=False)
        elif applied:
            log("\n[WARN] 资源已创建并保留在: %s" % workdir)
            log("  查看: python %s status --run \"%s\""
                % (Path(__file__).name, workdir))
            log("  销毁: python %s destroy --run \"%s\""
                % (Path(__file__).name, workdir))
        raise


def ensure_tf_init(tf, workdir, env):
    if not (Path(workdir) / ".terraform").is_dir():
        run([tf, "init", "-input=false", "-no-color"], cwd=str(workdir),
            env=env, capture=True, quiet=True, timeout=900)


def cmd_status(args):
    meta = load_run(args.run)
    workdir = Path(meta["workdir"])
    ak, sk = get_creds(args)
    tf = find_terraform()
    env = tf_env(ak, sk)
    ensure_tf_init(tf, workdir, env)
    outputs = tf_outputs(tf, workdir, env)
    log("== 环境: %s (%s)" % (meta["project"], workdir))
    for k in ("master_public_ips", "worker_public_ips"):
        log("%s: %s" % (k, ", ".join(outputs.get(k, []))))
    key = workdir / "id_ed25519"
    host = "root@%s" % meta["master1"]
    try:
        r = ssh_run(host, str(key), "kubectl get nodes; echo ---; kubectl -n %s "
                    "get pods,svc" % meta["namespace"],
                    capture=True, check=False, timeout=90, quiet=True)
        log((r.stdout or "").strip())
    except Exception as e:
        log("[WARN] 集群不可达: %s" % e)
    if meta.get("urls"):
        log("访问地址:")
        for x in meta["urls"]:
            log("  %-12s %s" % (x["service"], x["url"]))
    return 0


def cmd_destroy(args):
    meta = load_run(args.run)
    workdir = Path(meta["workdir"])
    ak, sk = get_creds(args)
    tf = find_terraform()
    env = tf_env(ak, sk)
    log("== 销毁环境: %s" % workdir)
    ensure_tf_init(tf, workdir, env)
    extra = []
    pub = Path(workdir) / "id_ed25519.pub"
    if pub.is_file():
        extra += ["-var", "ssh_public_key_path=%s" % pub.as_posix()]
    pw_file = Path(workdir) / "node_password.txt"
    if pw_file.is_file():
        extra += ["-var", "node_password=%s"
                  % pw_file.read_text(encoding="utf-8").strip()]
    params_file = Path(workdir) / "k8slab-params.json"
    if params_file.is_file():
        try:
            params = json.loads(params_file.read_text(encoding="utf-8"))
        except Exception:
            params = {}
        if params.get("key_pair_name"):
            extra += ["-var", "key_pair_name=%s" % params["key_pair_name"]]
        project = params.get("project") or meta.get("project")
        if project:
            extra += ["-var", "name_prefix=k8slab-%s" % project]
    if meta.get("region"):
        extra += ["-var", "region=%s" % meta["region"]]
    run([tf, "destroy", "-auto-approve", "-input=false", "-no-color"] + extra,
        cwd=str(workdir), env=env, timeout=1800)
    log("[OK] 资源已销毁，计费停止")
    if args.purge:
        shutil.rmtree(workdir, ignore_errors=True)
        log("[OK] 已删除本地运行记录: %s" % workdir)
    return 0


def cmd_export(args):
    out = Path(args.out or (Path.cwd() / "k8slab-kit.zip")).resolve()
    includes = ["k8slab.py", "main.tf", "variables.tf", "outputs.tf",
                "README.md", ".terraform.lock.hcl", "terraform.tfvars.example",
                "backend.tf.example"]
    inc_dirs = ["cloud-init", "scripts", "examples"]
    skips = {"__pycache__", "node_modules", ".venv", "venv", ".git",
             ".terraform", "keys", "data-migration", "library-k8s",
             "learning"}
    sensitive = {"node_password.txt", "tfplan", "course-selection.tar.gz",
                 "kubekey-cluster.yaml", "id_ed25519", "id_ed25519.pub",
                 "backend.tf"}
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in includes:
            p = BASE / f
            if p.is_file():
                z.write(p, f)
                count += 1
        for name in ("vm2k8s.py", "datamigrate.py"):
            vs = BASE / "course-selection" / name
            if vs.is_file():
                z.write(vs, "course-selection/" + name)
                count += 1
        for d in inc_dirs:
            root = BASE / d
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if p.name in sensitive or p.name.startswith("terraform.tfstate"):
                    continue
                if p.is_file() and not any(s in p.parts for s in skips):
                    z.write(p, str(p.relative_to(BASE)))
                    count += 1
        if args.with_provider:
            tfdir = BASE / ".terraform"
            if tfdir.is_dir():
                for p in tfdir.rglob("*"):
                    if p.is_file():
                        z.write(p, str(p.relative_to(BASE)))
                        count += 1
                log("[OK] 已附带 .terraform provider（离线 init）")
    log("[OK] 工具包已生成: %s (%d 个文件)" % (out, count))
    log("全新电脑使用:")
    log("  1) 安装 Python 3.8+ 与 OpenSSH")
    log("  2) python -m pip install pyyaml")
    log("  3) python k8slab.py doctor")
    log("  4) python k8slab.py up --compose examples/todo-app/docker-compose.yml "
        "--ak <AK> --sk <SK>")
    return 0


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(
        description="阿里云 K8s 迁移实验室一体化工具（全新电脑可用）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="检查本机依赖")
    d.add_argument("--ak", default="")
    d.add_argument("--sk", default="")
    d.add_argument("--download", action="store_true",
                   help="尝试自动下载 terraform")

    u = sub.add_parser("up", help="一条命令创建集群并部署应用")
    u.add_argument("--compose", required=True)
    u.add_argument("--project", default="")
    u.add_argument("--namespace", default="")
    u.add_argument("--ak", default="")
    u.add_argument("--sk", default="")
    u.add_argument("--region", default="cn-guangzhou")
    u.add_argument("--workers", type=int, default=3)
    u.add_argument("--password", default="")
    u.add_argument("--bandwidth", type=int, default=5)
    u.add_argument("--admin-cidr", default=os.environ.get("ADMIN_CIDR", ""),
                   help="允许 SSH/NodePort 的来源 CIDR；默认自动检测本机公网 IP")
    u.add_argument("--nodeport-cidr", default=os.environ.get("NODEPORT_CIDR", ""),
                   help="允许 NodePort 的来源 CIDR；默认复用 admin-cidr")
    u.add_argument("--web-cidr", default=os.environ.get("WEB_CIDR", "0.0.0.0/0"),
                   help="允许 80/443 的来源 CIDR；留空则关闭")
    u.add_argument("--no-ha", action="store_true",
                   help="不创建内网 SLB，API 回退到 master1（单点，省费用）")
    u.add_argument("--nodeport-start", type=int, default=30080)
    u.add_argument("--image-mode", default="auto",
                   choices=["auto", "none", "build", "save", "registry"])
    u.add_argument("--bundle", default="")
    u.add_argument("--registry", default="")
    u.add_argument("--ephemeral", action="store_true")
    u.add_argument("--ingress", action="store_true",
                   help="生成 nginx Ingress（需集群已有 ingress-controller）")
    u.add_argument("--ingress-host", default="",
                   help="基础域名；默认 <master1>.nip.io")
    u.add_argument("--ingress-map", default="",
                   help="服务=域名[/路径],服务=域名[/路径]")
    u.add_argument("--ingress-class", default="nginx")
    u.add_argument("--ingress-tls-secret", default="")
    u.add_argument("--private-registry", action="store_true",
                   help="在master1跑本地registry，节点镜像均从master拉取")
    u.add_argument("--verify", default="", help="svc=/path,svc2=/health")
    u.add_argument("--strict-bind", action="store_true",
                   help="bind 目录/缺失源直接失败，而不是跳过并告警")
    u.add_argument("--depends-init", action="store_true",
                   help="把 depends_on 近似转为 TCP 等待 initContainer")
    u.add_argument("--init-image", default="busybox:1.36",
                   help="depends_on initContainer 使用的镜像")
    u.add_argument("--cluster-timeout", type=int, default=3600)
    u.add_argument("--rollout-timeout", type=int, default=300)
    u.add_argument("--deploy-timeout", type=int, default=3600)
    u.add_argument("--workdir", default="")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--destroy-on-error", action="store_true")
    u.add_argument("--yes", action="store_true", help="保留参数（非交互）")

    s = sub.add_parser("status", help="查看环境状态")
    s.add_argument("--run", default="")
    s.add_argument("--ak", default="")
    s.add_argument("--sk", default="")

    x = sub.add_parser("destroy", help="销毁环境（停止计费）")
    x.add_argument("--run", default="")
    x.add_argument("--ak", default="")
    x.add_argument("--sk", default="")
    x.add_argument("--purge", action="store_true", help="同时删除本地记录")

    e = sub.add_parser("export", help="打包成全新电脑可用的 zip 工具包")
    e.add_argument("--out", default="")
    e.add_argument("--with-provider", action="store_true",
                   help="附带本地 .terraform provider（离线init，约+308MB）")

    args = ap.parse_args()
    handlers = {
        "doctor": cmd_doctor, "up": cmd_up, "status": cmd_status,
        "destroy": cmd_destroy, "export": cmd_export,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
