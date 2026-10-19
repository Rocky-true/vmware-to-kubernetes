#!/usr/bin/env python3
"""
vm2k8s —— 通用 Docker Compose → Kubernetes 迁移工具

用法:
  python vm2k8s.py assess  --compose docker-compose.yml
  python vm2k8s.py convert --compose docker-compose.yml --out k8s --namespace demo \
      [--ephemeral] [--image-map svc=img,...] [--nodeport-start 30080]

支持特性:
  image/build(单/多) · ports(短/长语法, TCP/UDP) · expose · environment/env_file
  command/entrypoint(字符串/列表) · named volume/anonymous volume · bind挂载单文件(转ConfigMap)
  healthcheck → readiness/liveness探针 · deploy.resources/replicas → resources/副本数
  compose level secrets/configs → K8s Secret/ConfigMap · labels · working_dir · user
  command/entrypoint 中的 --requirepass/--password 等参数提取到 Secret
忽略并告警:
  depends_on/networks/restart/logging/privileged/cap_add/devices/network_mode/ulimits 等
输出:
  Namespace/ConfigMap/Secret/Deployment/Service/PVC + deploy.sh + MIGRATION.md
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

STATEFUL_HINTS = ("mysql", "mariadb", "postgres", "redis", "mongo",
                  "rabbitmq", "kafka", "elasticsearch", "zookeeper")
SECRET_HINTS = ("PASS", "SECRET", "TOKEN", "PASSWORD", "APIKEY",
                "API_KEY", "CREDENTIAL", "PRIVATE_KEY", "_KEY")
IGNORED_KEYS = ("depends_on", "networks", "restart", "logging", "privileged",
                "cap_add", "cap_drop", "devices", "network_mode", "ulimits",
                "profiles", "extra_hosts", "dns", "dns_search", "init",
                "sysctls", "security_opt", "platform")


# ---------------- 基础工具 ----------------

def load_compose(path):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "services" not in data:
        sys.exit("compose 文件格式不正确：缺少 services")
    return data


def parse_duration(v, default=0):
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", s):
        n = float(num)
        total += {"ms": n / 1000.0, "s": n, "m": n * 60, "h": n * 3600}[unit]
    return int(total) if total else default


def memory_k8s(v):
    s = str(v).strip().lower().replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([kmg]?)i?b?$", s)
    if not m:
        return str(v)
    n = float(m.group(1))
    mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]
    b = int(n * mult)
    if b % (1024 ** 3) == 0:
        return "%dGi" % (b // 1024 ** 3)
    if b % (1024 ** 2) == 0:
        return "%dMi" % (b // 1024 ** 2)
    if b % 1024 == 0:
        return "%dKi" % (b // 1024)
    return str(b)


def cpu_k8s(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f < 1:
        return "%dm" % int(f * 1000)
    return str(int(f)) if f == int(f) else str(f)


def norm_exec(value):
    if value is None:
        return None
    if isinstance(value, str):
        return ["/bin/sh", "-c", value]
    if isinstance(value, list):
        return [str(c) for c in value]
    return None


COMMAND_SECRET_FLAGS = (
    "requirepass", "password", "auth", "auth-token", "token",
    "api-key", "apikey", "secret",
)
COMMAND_SECRET_RE = re.compile(
    r"(--(?:%s))(=|\s+)(['\"]?)([^\s'\";|&]+)\3"
    % "|".join(re.escape(x) for x in COMMAND_SECRET_FLAGS), re.I)
COMMAND_SECRET_FLAG_RE = re.compile(
    r"^(--(?:%s))(?:=(.*))?$"
    % "|".join(re.escape(x) for x in COMMAND_SECRET_FLAGS), re.I)


def store_command_secret(name, value, secret_data):
    base = "CMD_%s_SECRET" % re.sub(r"[^A-Z0-9]", "_", name.upper())
    key = base
    n = 2
    while key in secret_data and secret_data[key] != value:
        key = "%s_%d" % (base, n)
        n += 1
    secret_data[key] = value
    return key


def scrub_shell_command(name, script, secret_data):
    keys = []

    def repl(match):
        key = store_command_secret(name, match.group(4), secret_data)
        keys.append(key)
        return '%s%s"$%s"' % (match.group(1), match.group(2), key)

    new = COMMAND_SECRET_RE.sub(repl, script)
    return new, keys


def scrub_command_secrets(name, parts, secret_data):
    if not parts:
        return parts, []
    if (len(parts) >= 3 and parts[0] in ("/bin/sh", "sh", "bash")
            and parts[1] == "-c"):
        script, keys = scrub_shell_command(name, parts[2], secret_data)
        if keys:
            return [parts[0], parts[1], script], keys
        return parts, []
    rendered = []
    keys = []
    i = 0
    found = False
    while i < len(parts):
        part = str(parts[i])
        match = COMMAND_SECRET_FLAG_RE.match(part)
        if not match:
            rendered.append(shlex.quote(part))
            i += 1
            continue
        flag, inline = match.group(1), match.group(2)
        if inline is not None:
            value = inline
            rendered.append('%s="$%s"' % (flag, "PLACEHOLDER"))
        elif i + 1 < len(parts):
            value = str(parts[i + 1])
            rendered.append('%s "$%s"' % (flag, "PLACEHOLDER"))
            i += 1
        else:
            rendered.append(shlex.quote(part))
            i += 1
            continue
        key = store_command_secret(name, value, secret_data)
        keys.append(key)
        rendered[-1] = rendered[-1].replace("PLACEHOLDER", key)
        found = True
        i += 1
    if not found:
        return parts, []
    return ["/bin/sh", "-c", "exec " + " ".join(rendered)], keys


def read_env_file(path, base_dir):
    env = {}
    p = path if os.path.isabs(path) else os.path.join(base_dir, path)
    if not os.path.exists(p):
        return env
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def env_dict(svc, base_dir="."):
    env = {}
    env_files = svc.get("env_file") or []
    if isinstance(env_files, str):
        env_files = [env_files]
    for ef in env_files:
        env.update(read_env_file(ef, base_dir))
    raw = svc.get("environment") or {}
    if isinstance(raw, list):
        raw = dict(item.split("=", 1) for item in raw if "=" in item)
    env.update({str(k): str(v) for k, v in raw.items()})
    return env


def port_entries(svc):
    """返回 [{target, published, protocol}]，ports=发布, expose=仅内网"""
    out = []
    for p in svc.get("ports", []):
        proto = "TCP"
        try:
            if isinstance(p, dict):
                t = int(p.get("target"))
                proto = str(p.get("protocol") or "tcp").upper()
            else:
                parts = str(p).split(":")
                last = parts[-1]
                if "/" in last:
                    last, proto = last.split("/", 1)
                    proto = proto.upper()
                t = int(last)
            out.append({"target": t, "published": True, "protocol": proto})
        except (ValueError, TypeError):
            continue
    for e in svc.get("expose", []) or []:
        try:
            t = int(str(e).split("/")[0])
            proto = str(e).split("/")[1].upper() if "/" in str(e) else "TCP"
            out.append({"target": t, "published": False, "protocol": proto})
        except (ValueError, TypeError):
            continue
    # 去重
    seen = set()
    uniq = []
    for p in out:
        key = (p["target"], p["protocol"])
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def depends_init_containers(name, svc, services, init_image, warnings):
    """把 compose depends_on 近似转为 TCP 等待 initContainer。"""
    deps = svc.get("depends_on")
    if not deps:
        return []
    if isinstance(deps, dict):
        items = deps.items()
    else:
        items = [(d, None) for d in deps]
    out = []
    for dep, opts in items:
        dep = str(dep)
        condition = "service_started"
        if isinstance(opts, dict):
            condition = str(opts.get("condition") or condition)
        if condition == "service_completed_successfully":
            warnings.append("[%s] depends_on %s 为一次性任务，未生成 initContainer"
                            % (name, dep))
            continue
        target = services.get(dep)
        if not target:
            warnings.append("[%s] depends_on 引用的服务不存在: %s" % (name, dep))
            continue
        ports = port_entries(target)
        if not ports:
            warnings.append("[%s] depends_on %s 未声明端口，无法生成 TCP 等待"
                            % (name, dep))
            continue
        port = ports[0]["target"]
        out.append({
            "name": "wait-%s" % re.sub(r"[^a-z0-9-]", "-", dep.lower())[:40],
            "image": init_image,
            "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c",
                        "until nc -z -w 2 %s %d; do echo waiting for %s:%d; sleep 2; done"
                        % (dep, port, dep, port)],
        })
    return out


def healthcheck_probe(svc, ports):
    hc = svc.get("healthcheck") or {}
    test = hc.get("test")
    if not test:
        return None, None
    if isinstance(test, str):
        if test.strip().lower() == "none":
            return None, "disabled"
        cmd = test
    else:
        parts = [str(x) for x in test]
        if parts and parts[0].upper() == "NONE":
            return None, "disabled"
        if parts and parts[0].upper() == "CMD-SHELL":
            cmd = " ".join(parts[1:])
        elif parts and parts[0].upper() == "CMD":
            cmd = " ".join(parts[1:])
        else:
            cmd = " ".join(parts)

    base = {
        "periodSeconds": max(1, parse_duration(hc.get("interval"), 10)),
        "timeoutSeconds": max(1, parse_duration(hc.get("timeout"), 5)),
        "failureThreshold": max(1, int(hc.get("retries") or 3)),
        "initialDelaySeconds": max(1, parse_duration(hc.get("start_period"), 5)),
    }
    m = re.search(r"https?://([^/\s'\"]+)(/[^\s'\"]*)?", cmd)
    if m:
        host, path = m.group(1), m.group(2) or "/"
        port = 443 if cmd.strip().startswith("https") else 80
        if ":" in host:
            try:
                port = int(host.split(":")[-1])
            except ValueError:
                pass
        probe = dict(base)
        probe["httpGet"] = {"path": path, "port": port}
        return probe, "http"
    if ports:
        probe = dict(base)
        probe["tcpSocket"] = {"port": ports[0]["target"]}
        return probe, "tcp"
    return None, "unknown"


def resources_for(svc):
    res = {"requests": {"cpu": "100m", "memory": "128Mi"},
           "limits": {"cpu": "1", "memory": "1Gi"}}
    dep = svc.get("deploy") or {}
    r = dep.get("resources") or {}
    lim = r.get("limits") or {}
    req = r.get("reservations") or {}
    if lim.get("cpus") is not None:
        res["limits"]["cpu"] = cpu_k8s(lim["cpus"])
    if lim.get("memory") is not None:
        res["limits"]["memory"] = memory_k8s(lim["memory"])
    if req.get("cpus") is not None:
        res["requests"]["cpu"] = cpu_k8s(req["cpus"])
    if req.get("memory") is not None:
        res["requests"]["memory"] = memory_k8s(req["memory"])
    return res


def deploy_mounts(svc):
    """解析 volumes: named volume/anonymous/bind文件(返回文件信息)/bind目录(警告)"""
    mounts, named, empty, bind_files, warnings = [], [], [], [], []
    for v in svc.get("volumes", []):
        src = mpath = rw = None
        if isinstance(v, dict):
            vtype = str(v.get("type") or "volume")
            if vtype == "bind":
                src, mpath = v.get("source"), v.get("target")
                bind_files.append({
                    "source": src, "target": mpath,
                    "ro": bool(v.get("read_only")),
                })
            elif vtype == "volume":
                src, mpath = v.get("source"), v.get("target")
            else:
                warnings.append("bind/tmpfs 挂载未支持: %s" % v)
                continue
        else:
            parts = str(v).split(":")
            if len(parts) == 1:
                mpath = parts[0]
                empty.append(mpath)
                continue
            src, mpath = parts[0], parts[1]
            if len(parts) > 2:
                rw = parts[2]

        if not mpath:
            continue
        if src and (src.startswith((".", "/"))):
            bind_files.append({"source": src, "target": mpath, "ro": rw == "ro"})
        elif src:
            mounts.append({"name": src, "mountPath": mpath})
            if src not in named:
                named.append(src)
    return mounts, named, empty, bind_files, warnings


# ---------------- assess ----------------

def assess(compose):
    print("=" * 84)
    print("vm2k8s 迁移评估报告")
    print("=" * 84)
    print(f"{'服务':<12}{'镜像/构建':<30}{'策略':<18}{'批次':<8}理由")
    print("-" * 84)
    for name, svc in compose["services"].items():
        img = "build(本地构建)" if "build" in svc else str(svc.get("image", "-"))
        lname = name.lower()
        limg = str(svc.get("image", "")).lower()
        stateful = any(h in lname or h in limg for h in STATEFUL_HINTS)
        if "build" in svc:
            strat, batch, reason = "容器化重建", "第1批", "无状态应用，直接容器化"
        elif lname in ("nginx", "gateway"):
            strat, batch, reason = "容器化(网关)", "第1批", "入口网关，生产可替换为Ingress"
        elif stateful:
            strat, batch, reason = "容器化+数据迁移", "第3批", "有状态服务，需PVC/主从迁移"
        else:
            strat, batch, reason = "容器化重建", "第2批", "一般服务"
        print(f"{name:<12}{img:<30}{strat:<18}{batch:<8}{reason}")
    print("-" * 84)
    print("建议：第1批验证流程 → 第2批业务服务 → 第3批有状态中间件（低谷窗口切换）")


# ---------------- convert ----------------

def convert(compose, outdir, namespace, ephemeral=False, image_map=None,
            nodeport_start=30080, compose_dir=".", ingress=False,
            ingress_host="", ingress_map=None, ingress_class="nginx",
            ingress_tls_secret="", strict_bind=False, depends_init=False,
            init_image="busybox:1.36"):
    services = compose["services"]
    os.makedirs(outdir, exist_ok=True)
    image_map = image_map or {}

    warnings = []
    config_data, secret_data = {}, {}
    file_configs = {}       # configmap key -> content (bind 单文件)
    top_secrets, top_configs = {}, {}
    deployments, svcs, pvcs = [], [], []
    published = []          # 记录需要 NodePort 的服务与端口
    images = []

    # 顶层 secrets/configs
    for name, spec in (compose.get("secrets") or {}).items():
        path = (spec or {}).get("file")
        if path:
            p = path if os.path.isabs(path) else os.path.join(compose_dir, path)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    top_secrets[name] = f.read()
            else:
                warnings.append("secret 文件不存在: %s" % path)
    for name, spec in (compose.get("configs") or {}).items():
        path = (spec or {}).get("file")
        if path:
            p = path if os.path.isabs(path) else os.path.join(compose_dir, path)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    top_configs[name] = f.read()
            else:
                warnings.append("config 文件不存在: %s" % path)

    for name, svc in services.items():
        for key in IGNORED_KEYS:
            if key in svc:
                if key == "depends_on":
                    if depends_init:
                        warnings.append("[%s] depends_on 已近似转为 TCP initContainer"
                                        % name)
                    else:
                        warnings.append("[%s] depends_on 不建模启动顺序，应用需自行重试连接"
                                        % name)
                else:
                    warnings.append("[%s] 忽略 compose 字段: %s" % (name, key))

        env = env_dict(svc, compose_dir)
        for k, v in env.items():
            if any(t in k.upper() for t in SECRET_HINTS):
                secret_data[k] = v
            else:
                config_data[k] = v

        if "build" in svc:
            image = image_map.get(name) or svc.get("image") or "local/%s:latest" % name
        else:
            image = image_map.get(name) or svc.get("image")
        if not image:
            warnings.append("[%s] 无 image/build，跳过" % name)
            continue
        images.append(image)

        ports = port_entries(svc)

        mounts, named, empty, bind_files, vwarn = deploy_mounts(svc)
        warnings.extend("[%s] %s" % (name, w) for w in vwarn)

        vols = []
        for vname in named:
            if ephemeral:
                vols.append({"name": vname, "emptyDir": {}})
            else:
                vols.append({"name": vname,
                             "persistentVolumeClaim": {"claimName": vname}})
                if vname not in pvcs:
                    pvcs.append(vname)
        for idx, mpath in enumerate(empty):
            vname = "anon-%d" % idx
            vols.append({"name": vname, "emptyDir": {}})
            mounts.append({"name": vname, "mountPath": mpath})

        # bind 挂载：文件→ConfigMap，目录→告警跳过
        for bf in bind_files:
            src = bf.get("source") or ""
            p = src if os.path.isabs(src) else os.path.join(compose_dir, src)
            if os.path.isfile(p):
                key = "%s-%s" % (name, os.path.basename(src))
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    file_configs[key] = f.read()
                vname = "file-" + key.replace(".", "-")
                vols.append({"name": vname, "configMap": {"name": "migration-files"}})
                mounts.append({"name": vname, "mountPath": bf["target"],
                               "subPath": key})
            else:
                msg = "[%s] bind挂载未支持(目录或不存在): %s" % (name, src)
                if strict_bind:
                    raise SystemExit(msg + "；--strict-bind 拒绝静默跳过")
                warnings.append(msg)

        # 顶层 secrets / configs 挂载
        for ref in svc.get("secrets", []) or []:
            if isinstance(ref, dict):
                sname = ref.get("source")
                target = ref.get("target") or "/run/secrets/%s" % sname
            else:
                sname, target = str(ref), "/run/secrets/%s" % ref
            if sname in top_secrets:
                vname = "compose-secret-" + sname
                vols.append({"name": vname,
                             "secret": {"secretName": vname}})
                mounts.append({"name": vname, "mountPath": target,
                               "subPath": sname, "readOnly": True})
            else:
                warnings.append("[%s] 引用的secret未定义: %s" % (name, sname))
        for ref in svc.get("configs", []) or []:
            if isinstance(ref, dict):
                cname = ref.get("source")
                target = ref.get("target") or "/%s" % cname
            else:
                cname, target = str(ref), "/%s" % ref
            if cname in top_configs:
                vname = "compose-config-" + cname
                vols.append({"name": vname,
                             "configMap": {"name": vname}})
                mounts.append({"name": vname, "mountPath": target,
                               "subPath": cname, "readOnly": True})
            else:
                warnings.append("[%s] 引用的config未定义: %s" % (name, cname))

        container = {
            "name": name,
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "resources": resources_for(svc),
            "envFrom": [
                {"configMapRef": {"name": "migration-config"}},
                {"secretRef": {"name": "migration-secret"}},
            ],
        }

        entry = norm_exec(svc.get("entrypoint"))
        cmd = norm_exec(svc.get("command"))
        entry, entry_keys = scrub_command_secrets(name, entry, secret_data)
        cmd, cmd_keys = scrub_command_secrets(name, cmd, secret_data)
        for key in entry_keys + cmd_keys:
            warnings.append("[%s] 已从 command/entrypoint 提取敏感参数到 Secret (%s)"
                            % (name, key))
        if entry:
            container["command"] = entry
            if cmd:
                container["args"] = cmd
        elif cmd:
            container["command"] = cmd
        if svc.get("working_dir"):
            container["workingDir"] = str(svc["working_dir"])
        if svc.get("user"):
            u = str(svc["user"])
            ug = u.split(":", 1)
            sec = {}
            try:
                sec["runAsUser"] = int(ug[0])
                if len(ug) > 1:
                    sec["runAsGroup"] = int(ug[1])
                container["securityContext"] = sec
            except ValueError:
                warnings.append("[%s] 用户名形式user未支持: %s" % (name, u))

        if ports:
            container["ports"] = [{"containerPort": p["target"],
                                   "protocol": p["protocol"]} for p in ports]

        probe, probe_kind = healthcheck_probe(svc, ports)
        if probe:
            container["readinessProbe"] = probe
            container["livenessProbe"] = dict(probe)
            if probe_kind == "tcp":
                warnings.append("[%s] healthcheck 无法解析为HTTP，使用TCP探针" % name)
        elif probe_kind == "disabled":
            warnings.append("[%s] healthcheck: NONE，未生成探针" % name)
        elif ports:
            container["readinessProbe"] = {
                "tcpSocket": {"port": ports[0]["target"]},
                "initialDelaySeconds": 10,
                "periodSeconds": 5,
            }

        if mounts:
            container["volumeMounts"] = mounts

        replicas = int((svc.get("deploy") or {}).get("replicas") or 1)
        labels = {"app": name}
        for k, v in (svc.get("labels") or {}).items():
            labels[str(k)] = str(v)
        inits = (depends_init_containers(name, svc, services, init_image,
                                         warnings)
                 if depends_init else [])

        deployments.append({
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [container],
                        **({"initContainers": inits} if inits else {}),
                        **({"volumes": vols} if vols else {}),
                    },
                },
            },
        })

        if ports:
            svc_ports = [{
                "name": "%s-%d" % (p["protocol"].lower(), p["target"]),
                "port": p["target"],
                "targetPort": p["target"],
                "protocol": p["protocol"],
            } for p in ports]
            svcs.append({
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": namespace, "labels": labels},
                "spec": {"selector": {"app": name}, "ports": svc_ports},
            })
            pub = [p for p in ports if p["published"]]
            if pub:
                published.append({"service": name, "ports": ports})

    docs = {
        "00-namespace.yaml": [{"apiVersion": "v1", "kind": "Namespace",
                               "metadata": {"name": namespace}}],
        "01-configmap.yaml": [{"apiVersion": "v1", "kind": "ConfigMap",
                               "metadata": {"name": "migration-config",
                                            "namespace": namespace},
                               "data": config_data or {}}],
        "02-secret.yaml": [{"apiVersion": "v1", "kind": "Secret",
                            "metadata": {"name": "migration-secret",
                                         "namespace": namespace},
                            "type": "Opaque", "stringData": secret_data or {}}],
        "03-deployments.yaml": deployments,
        "04-services.yaml": svcs,
    }
    if file_configs:
        docs["01-configmap.yaml"].append({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "migration-files", "namespace": namespace},
            "data": file_configs,
        })
    for sname, content in top_secrets.items():
        docs["02-secret.yaml"].append({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "compose-secret-" + sname, "namespace": namespace},
            "type": "Opaque", "stringData": {sname: content},
        })
    for cname, content in top_configs.items():
        docs["01-configmap.yaml"].append({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "compose-config-" + cname, "namespace": namespace},
            "data": {cname: content},
        })
    if pvcs:
        docs["05-pvcs.yaml"] = [{
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": p, "namespace": namespace},
            "spec": {"accessModes": ["ReadWriteOnce"],
                     "resources": {"requests": {"storage": "1Gi"}}},
        } for p in pvcs]

    ingress_docs = []
    if ingress:
        imap = ingress_map or {}
        for item in published:
            svc_name = item["service"]
            http_ports = [p for p in item["ports"]
                          if p["published"] and p["target"] in HTTP_PORTS]
            if not http_ports:
                continue
            target = http_ports[0]["target"]
            if svc_name in imap:
                hp = imap[svc_name]
                if "/" in hp:
                    host, path = hp.split("/", 1)
                    path = "/" + path.lstrip("/")
                else:
                    host, path = hp, "/"
            elif ingress_host:
                host = "%s.%s" % (svc_name, ingress_host.strip("."))
                path = "/"
            else:
                host, path = "", "/" + svc_name
            rule = {"http": {"paths": [{
                "path": path,
                "pathType": "Prefix",
                "backend": {"service": {"name": svc_name,
                                        "port": {"number": target}}},
            }]}}
            if host:
                rule["host"] = host
            meta = {"name": "%s-ingress" % svc_name, "namespace": namespace}
            if not host:
                meta["annotations"] = {
                    "nginx.ingress.kubernetes.io/rewrite-target": "/"}
            spec = {"ingressClassName": ingress_class, "rules": [rule]}
            if ingress_tls_secret and host:
                spec["tls"] = [{"hosts": [host],
                                "secretName": ingress_tls_secret}]
            ingress_docs.append({
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": meta,
                "spec": spec,
            })
        if ingress_docs:
            docs["07-ingress.yaml"] = ingress_docs

    for fname, items in docs.items():
        path = os.path.join(outdir, fname)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump_all(items, f, allow_unicode=True, sort_keys=False)

    # deploy.sh：一键部署 + NodePort 暴露
    np = nodeport_start
    for item in published:
        for p in item["ports"]:
            if p["published"]:
                p["nodePort"] = np
                np += 1

    lines = ["#!/bin/bash", "set -e",
             'DIR="$(cd "$(dirname "$0")" && pwd)"',
             'kubectl apply -f "$DIR"/']
    for item in published:
        ports = []
        for p in item["ports"]:
            entry = {"port": p["target"], "targetPort": p["target"],
                     "protocol": p["protocol"]}
            if p.get("nodePort"):
                entry["nodePort"] = p["nodePort"]
            ports.append(entry)
        patch = json.dumps({"spec": {"type": "NodePort", "ports": ports}},
                           separators=(",", ":"))
        lines.append("kubectl -n %s patch svc %s -p '%s' || echo "
                     "'[WARN] patch svc %s failed (nodePort conflict?)'"
                     % (namespace, item["service"], patch, item["service"]))
    lines.append("sleep 5")
    lines.append("kubectl -n %s get pods,svc" % namespace)
    with open(os.path.join(outdir, "06-deploy.sh"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    # MIGRATION.md
    md = ["# 迁移说明（vm2k8s 自动生成）", "",
          "- 目标命名空间: `%s`" % namespace,
          "- 生成清单: Namespace/ConfigMap/Secret/Deployment/Service/PVC",
          "- 一键部署: `bash 06-deploy.sh`（需 kubectl 指向目标集群）", "",
          "## 服务", "", "| 服务 | 镜像 | 端口 |", "|---|---|---|"]
    for d in deployments:
        c = d["spec"]["template"]["spec"]["containers"][0]
        ps = ",".join(str(p["containerPort"]) for p in c.get("ports", []))
        md.append("| %s | %s | %s |" % (d["metadata"]["name"], c["image"], ps or "-"))
    md += ["", "## 告警（需人工确认）", ""]
    md += ["- " + w for w in warnings] if warnings else ["- 无"]
    depends_limit = ("- depends_on 已近似为 TCP initContainer；非 TCP/无端口依赖仍需人工处理"
                     if depends_init else
                     "- depends_on 启动顺序未建模（可用 --depends-init）")
    md += ["", "## 已知限制", "",
           depends_limit,
           "- networks 视为同一平面网络",
           "- 目录型 bind mount 未自动转换（建议手工 ConfigMap/PVC，或用 --strict-bind 拒绝）",
           "- compose profiles 未展开"]
    with open(os.path.join(outdir, "MIGRATION.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    print("生成完成: %s" % outdir)
    for fname in sorted(os.listdir(outdir)):
        print("  -", fname)
    if warnings:
        print("\n告警:")
        for w in warnings:
            print("  [WARN]", w)
    print("\n部署: bash %s/06-deploy.sh" % outdir)
    if ingress_docs:
        print("\nIngress:")
        for d in ingress_docs:
            rule = d["spec"]["rules"][0]
            host = rule.get("host", "<node-ip>")
            path = rule["http"]["paths"][0]["path"]
            print("  http://%s%s" % (host, path))
    return {
        "outdir": outdir,
        "namespace": namespace,
        "deployments": [d["metadata"]["name"] for d in deployments],
        "published": published,
        "images": images,
        "warnings": warnings,
    }


# ---------------- ship：转换 + 镜像 + 部署 + 验证 ----------------

HTTP_PORTS = (80, 3000, 5000, 8000, 8080, 8081, 8888)


def shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def run_cmd(cmd, cwd=None, timeout=None, capture=False, check=True,
            allow_timeout=False):
    printable = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    printable = re.sub(r"(PASSWORD=)'[^']*'", r"\1'***'", printable)
    printable = re.sub(r"(--node-password\s+)\S+", r"\1***", printable)
    printable = re.sub(r"(--password\s+)\S+", r"\1***", printable)
    print("+ " + printable)
    try:
        r = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError:
        sys.exit("找不到命令: %s" % cmd[0])
    except subprocess.TimeoutExpired:
        if allow_timeout:
            return subprocess.CompletedProcess(cmd, 1, "", "timeout")
        sys.exit("命令超时: %s" % printable)
    if check and r.returncode != 0:
        if capture:
            print(r.stdout or "", r.stderr or "")
        sys.exit("命令失败(exit=%d): %s" % (r.returncode, printable))
    return r


def ssh_argv(remote, key):
    argv = ["ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15"]
    if key:
        argv += ["-i", key]
    return argv + [remote]


def scp_argv(key):
    argv = ["scp", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes"]
    if key:
        argv += ["-i", key]
    return argv


def ssh_run(remote, key, cmd, timeout=None, capture=False, check=True,
            retries=3):
    last = None
    for i in range(retries):
        last = run_cmd(ssh_argv(remote, key) + [cmd], timeout=timeout,
                       capture=capture, check=False, allow_timeout=True)
        if last.returncode == 0:
            return last
        transient = last.returncode == 255 or \
            (last.stderr or "").strip() == "timeout"
        if transient and i < retries - 1:
            print("  SSH 瞬断，重试 %d/%d ..." % (i + 1, retries), flush=True)
            time.sleep(3)
            continue
        break
    if check:
        if capture:
            print(last.stdout or "", last.stderr or "")
        sys.exit("SSH命令失败(exit=%d): %s" % (last.returncode, cmd))
    return last


def scp_upload(local, remote, dest, key, timeout=1800):
    run_cmd(scp_argv(key) + [os.path.basename(local), "%s:%s" % (remote, dest)],
            cwd=os.path.dirname(os.path.abspath(local)), timeout=timeout)


def scp_upload_dir(local_dir, remote, dest, key, timeout=600):
    run_cmd(scp_argv(key) + ["-r", os.path.basename(local_dir),
                             "%s:%s" % (remote, dest)],
            cwd=os.path.dirname(os.path.abspath(local_dir)), timeout=timeout)


def wait_remote_cluster(args):
    print("== 等待集群就绪（kubectl 可用 + 全部节点 Ready）")
    deadline = time.time() + args.cluster_timeout
    last = "unknown"
    while time.time() < deadline:
        r = ssh_run(args.remote, args.ssh_key,
                    "kubectl get nodes --no-headers 2>/dev/null",
                    timeout=60, capture=True, check=False)
        lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
        ready = [l for l in lines if re.search(r"\sReady\s", l)]
        last = "nodes=%d ready=%d" % (len(lines), len(ready))
        if lines:
            print("  " + last)
            if len(ready) == len(lines) and \
                    (not args.expect_nodes or len(lines) >= args.expect_nodes):
                for l in lines:
                    print("   ", l)
                return
        else:
            print("  kubectl/集群未就绪，等待...")
        time.sleep(15)
    sys.exit("集群在 %ds 内未就绪 (%s)" % (args.cluster_timeout, last))


def deploy_local(args, result):
    ns = result["namespace"]
    print("== 本地 kubectl 部署")
    run_cmd([args.kubectl, "apply", "-f", result["outdir"]])
    for item in result["published"]:
        ports = []
        for p in item["ports"]:
            entry = {"port": p["target"], "targetPort": p["target"],
                     "protocol": p["protocol"]}
            if p.get("nodePort"):
                entry["nodePort"] = p["nodePort"]
            ports.append(entry)
        patch = json.dumps({"spec": {"type": "NodePort", "ports": ports}},
                           separators=(",", ":"))
        run_cmd([args.kubectl, "-n", ns, "patch", "svc", item["service"],
                 "-p", patch], check=False)
    for d in result["deployments"]:
        run_cmd([args.kubectl, "-n", ns, "rollout", "status",
                 "deployment/%s" % d, "--timeout=%ds" % args.rollout_timeout],
                timeout=args.rollout_timeout + 30)


def to_ssh_path(p):
    if p.startswith("~"):
        return p.replace("~", "$HOME", 1)
    return p


def deploy_remote(args, result, bundle):
    ns = result["namespace"]
    remote = args.remote
    key = args.ssh_key
    scp_base = args.remote_dir
    ssh_base = to_ssh_path(scp_base)
    print("== 远端部署到 %s" % remote)
    ssh_run(remote, key, "mkdir -p %s" % ssh_base, timeout=30)

    if bundle:
        if not os.path.isfile(bundle):
            sys.exit("镜像包不存在: %s" % bundle)
        print("== 上传镜像包 (%d MB)" % (os.path.getsize(bundle) // 1024 // 1024))
        scp_upload(bundle, remote, "%s/images.tar.gz" % scp_base, key)
        script = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "scripts",
            "distribute-images.sh"))
        if os.path.isfile(script):
            scp_upload(script, remote, "%s/distribute-images.sh" % scp_base, key)
            prefix = "TARBALL=%s/images.tar.gz " % ssh_base
            if args.node_password:
                prefix += "NODE_PASSWORD=%s " % shq(args.node_password)
            ssh_run(remote, key, prefix + 'bash "%s/distribute-images.sh"'
                    % ssh_base, timeout=3600)
        else:
            print("[WARN] 未找到 scripts/distribute-images.sh，跳过离线分发")

    ssh_run(remote, key, "rm -rf %s/%s" % (ssh_base, ns), timeout=30)
    scp_upload_dir(result["outdir"], remote, "%s/%s" % (scp_base, ns), key)
    print("== 应用 K8s 清单")
    ssh_run(remote, key, 'bash "%s/%s/06-deploy.sh"' % (ssh_base, ns),
            timeout=300)
    print("== 等待滚动更新")
    for d in result["deployments"]:
        ssh_run(remote, key,
                "kubectl -n %s rollout status deployment/%s --timeout=%ds"
                % (ns, d, args.rollout_timeout),
                timeout=args.rollout_timeout + 30)


def parse_verify(spec):
    out = {}
    for part in (spec or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip() or "/"
    return out


def verify(result, host, spec, timeout):
    wanted = parse_verify(spec)
    checks = []
    for item in result["published"]:
        for p in item["ports"]:
            if not p.get("nodePort"):
                continue
            svc = item["service"]
            if wanted:
                if svc not in wanted:
                    continue
                path = wanted[svc]
            elif p["target"] in HTTP_PORTS:
                path = "/"
            else:
                continue
            url = "http://%s:%d%s" % (host, p["nodePort"], path)
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    checks.append((url, "OK(%d)" % r.getcode(), True))
            except urllib.error.HTTPError as e:
                checks.append((url, "HTTP %d (可达)" % e.code, True))
            except Exception as e:
                checks.append((url, "FAIL: %s" % e, False))
    if not checks:
        print("\n[WARN] 没有可验证的 HTTP 服务（可用 --verify svc=/path 指定）")
        return 0
    print("\n== 验证")
    ok = True
    for url, st, good in checks:
        print("  %-58s %s" % (url, st))
        ok = ok and good
    return 0 if ok else 1


def compose_images(compose, image_map):
    out = {}
    for name, svc in compose["services"].items():
        if "build" in svc:
            img = image_map.get(name) or svc.get("image") or "local/%s:latest" % name
        else:
            img = image_map.get(name) or svc.get("image")
        if img:
            out[name] = img
    return out


def ship(args):
    compose = load_compose(args.compose)
    compose_dir = os.path.dirname(os.path.abspath(args.compose))
    image_map = parse_image_map(args.image_map)

    mode = args.image_mode
    if mode == "auto":
        if any("build" in s for s in compose["services"].values()):
            mode = "build"
        elif args.bundle:
            mode = "save"
        else:
            mode = "none"
    print("== 镜像模式: %s" % mode)

    src_images = compose_images(compose, image_map)
    if mode == "registry":
        if not args.registry:
            sys.exit("registry 模式需要 --registry <仓库前缀>")
        registry = args.registry.rstrip("/")
        image_map = {}
        for name, img in src_images.items():
            image_map[name] = "%s/%s" % (registry, img.split("/")[-1])
        if any("build" in s for s in compose["services"].values()):
            run_cmd(["docker", "compose", "-f", args.compose, "build"],
                    cwd=compose_dir, timeout=1800)
        for name, img in src_images.items():
            run_cmd(["docker", "tag", img, image_map[name]], timeout=120)
            run_cmd(["docker", "push", image_map[name]], timeout=600)

    result = convert(compose, args.out, args.namespace, args.ephemeral,
                     image_map, args.nodeport_start, compose_dir,
                     ingress=args.ingress,
                     ingress_host=args.ingress_host,
                     ingress_map=parse_ingress_map(args.ingress_map),
                     ingress_class=args.ingress_class,
                     ingress_tls_secret=args.ingress_tls_secret,
                     strict_bind=args.strict_bind,
                     depends_init=args.depends_init,
                     init_image=args.init_image)

    bundle = os.path.abspath(args.bundle) if args.bundle else None
    if mode == "build":
        run_cmd(["docker", "compose", "-f", args.compose, "build"],
                cwd=compose_dir, timeout=1800)
        bundle = bundle or os.path.join(os.path.abspath(args.out),
                                        "images.tar.gz")
        run_cmd(["docker", "save", "-o", bundle] + list(src_images.values()),
                timeout=1200)
        print("== 镜像已导出: %s" % bundle)
    elif mode == "save" and not bundle:
        sys.exit("save 模式需要 --bundle <镜像tar.gz>")

    if args.wait_cluster and args.remote:
        wait_remote_cluster(args)

    if args.remote:
        deploy_remote(args, result, bundle)
        verify_host = args.verify_host or \
            args.remote.split("@")[-1].split(":")[0]
    else:
        deploy_local(args, result)
        verify_host = args.verify_host or "127.0.0.1"

    if args.no_verify:
        print("\n[SKIP] 跳过验证")
        return 0
    return verify(result, verify_host, args.verify, args.verify_timeout)


def parse_image_map(spec):
    image_map = {}
    for pair in (spec or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            image_map[k.strip()] = v.strip()
    return image_map


def parse_ingress_map(spec):
    out = {}
    for pair in (spec or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser(description="vm2k8s 迁移工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("assess", help="迁移评估")
    a.add_argument("--compose", required=True)

    c = sub.add_parser("convert", help="转换为K8s清单")
    c.add_argument("--compose", required=True)
    c.add_argument("--out", default="k8s")
    c.add_argument("--namespace", default="app")
    c.add_argument("--ephemeral", action="store_true", help="用emptyDir代替PVC")
    c.add_argument("--image-map", default="", help="服务=镜像,服务=镜像")
    c.add_argument("--nodeport-start", type=int, default=30080)
    c.add_argument("--ingress", action="store_true",
                   help="生成 07-ingress.yaml（需要 nginx ingress-controller）")
    c.add_argument("--ingress-host", default="",
                   help="基础域名，服务会生成 <service>.<host>")
    c.add_argument("--ingress-map", default="",
                   help="服务=域名[/路径],服务=域名[/路径]")
    c.add_argument("--ingress-class", default="nginx")
    c.add_argument("--ingress-tls-secret", default="")
    c.add_argument("--strict-bind", action="store_true",
                   help="bind 目录/缺失源直接失败，而不是跳过并告警")
    c.add_argument("--depends-init", action="store_true",
                   help="把 depends_on 近似转为 TCP 等待 initContainer")
    c.add_argument("--init-image", default="busybox:1.36",
                   help="depends_on initContainer 使用的镜像")

    s = sub.add_parser("ship", help="一键转换+镜像+部署+验证")
    s.add_argument("--compose", required=True)
    s.add_argument("--namespace", default="",
                   help="默认取 compose 所在目录名")
    s.add_argument("--out", default="", help="默认 ./<namespace>-k8s")
    s.add_argument("--ephemeral", action="store_true")
    s.add_argument("--image-map", default="")
    s.add_argument("--nodeport-start", type=int, default=30080)
    s.add_argument("--ingress", action="store_true",
                   help="生成 07-ingress.yaml（需要 nginx ingress-controller）")
    s.add_argument("--ingress-host", default="",
                   help="基础域名，服务会生成 <service>.<host>")
    s.add_argument("--ingress-map", default="",
                   help="服务=域名[/路径],服务=域名[/路径]")
    s.add_argument("--ingress-class", default="nginx")
    s.add_argument("--ingress-tls-secret", default="")
    s.add_argument("--strict-bind", action="store_true",
                   help="bind 目录/缺失源直接失败，而不是跳过并告警")
    s.add_argument("--depends-init", action="store_true",
                   help="把 depends_on 近似转为 TCP 等待 initContainer")
    s.add_argument("--init-image", default="busybox:1.36",
                   help="depends_on initContainer 使用的镜像")
    s.add_argument("--image-mode", default="auto",
                   choices=["auto", "none", "build", "save", "registry"])
    s.add_argument("--registry", default="", help="registry 模式仓库前缀")
    s.add_argument("--bundle", default="", help="预导出镜像包 images.tar.gz")
    s.add_argument("--remote", default="", help="root@master1（远端部署）")
    s.add_argument("--remote-dir", default="~/vm2k8s",
                   help="远端工作目录（默认 ~/vm2k8s）")
    s.add_argument("--ssh-key", default="")
    s.add_argument("--node-password",
                   default=os.environ.get("K8S_NODE_PASSWORD", ""))
    s.add_argument("--kubectl", default="kubectl")
    s.add_argument("--wait-cluster", action="store_true",
                   help="等待 kubeconfig 与节点 Ready")
    s.add_argument("--expect-nodes", type=int, default=0)
    s.add_argument("--cluster-timeout", type=int, default=600)
    s.add_argument("--rollout-timeout", type=int, default=300)
    s.add_argument("--verify", default="", help="svc=/path,svc2=/health")
    s.add_argument("--verify-host", default="")
    s.add_argument("--verify-timeout", type=int, default=8)
    s.add_argument("--no-verify", action="store_true")

    args = ap.parse_args()

    if args.cmd == "ship":
        if not args.namespace:
            d = os.path.basename(os.path.dirname(os.path.abspath(args.compose)))
            args.namespace = re.sub(r"[^a-z0-9-]", "-", d.lower()) or "app"
        if not args.out:
            args.out = os.path.join(os.getcwd(), args.namespace + "-k8s")
        sys.exit(ship(args))

    compose = load_compose(args.compose)
    compose_dir = os.path.dirname(os.path.abspath(args.compose))

    if args.cmd == "assess":
        assess(compose)
        return

    image_map = parse_image_map(args.image_map)
    convert(compose, args.out, args.namespace, args.ephemeral,
            image_map, args.nodeport_start, compose_dir,
            ingress=args.ingress,
            ingress_host=args.ingress_host,
            ingress_map=parse_ingress_map(args.ingress_map),
            ingress_class=args.ingress_class,
            ingress_tls_secret=args.ingress_tls_secret,
            strict_bind=args.strict_bind,
            depends_init=args.depends_init,
            init_image=args.init_image)


if __name__ == "__main__":
    main()
