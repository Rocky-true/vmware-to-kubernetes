"""vm2k8s 普遍性测试：对多个不同compose项目运行 convert 并校验产物"""
import json
import os
import re
import subprocess
import sys
import tempfile

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
VM2K8S = os.path.join(ROOT, "vm2k8s.py")
FIXTURES = os.path.join(BASE, "fixtures")
FILES = ["01-configmap.yaml", "02-secret.yaml", "03-deployments.yaml",
         "04-services.yaml", "05-pvcs.yaml", "07-ingress.yaml"]


def convert_path(src, extra=None):
    out = tempfile.mkdtemp(prefix="vm2k8s-")
    cmd = [sys.executable, VM2K8S, "convert", "--compose", src,
           "--out", out, "--namespace", "test"]
    cmd += extra or []
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, "convert failed: %s" % r.stderr
    docs = {}
    for f in FILES:
        p = os.path.join(out, f)
        docs[f] = list(yaml.safe_load_all(open(p, encoding="utf-8"))) if os.path.exists(p) else []
    return docs, out


def convert(name, extra=None):
    return convert_path(os.path.join(FIXTURES, name, "docker-compose.yml"),
                        extra)


def deployments(docs):
    return {d["metadata"]["name"]: d for d in docs["03-deployments.yaml"]}


def container(dep):
    return dep["spec"]["template"]["spec"]["containers"][0]


def check(name, fn):
    try:
        fn()
        print("[PASS] %s" % name)
        return True
    except AssertionError as e:
        print("[FAIL] %s: %s" % (name, e))
        return False


def t_strict_bind():
    tmp = tempfile.mkdtemp(prefix="vm2k8s-strict-")
    os.makedirs(os.path.join(tmp, "data"))
    compose = os.path.join(tmp, "docker-compose.yml")
    with open(compose, "w", encoding="utf-8") as f:
        f.write("services:\n  app:\n    image: nginx\n"
                "    volumes:\n      - ./data:/data\n")
    r = subprocess.run([sys.executable, VM2K8S, "convert", "--compose", compose,
                        "--out", os.path.join(tmp, "out"), "--strict-bind"],
                       capture_output=True, text=True)
    assert r.returncode != 0, "strict-bind should fail on directory bind"
    assert "strict-bind" in (r.stdout + r.stderr)


def t_depends_init():
    tmp = tempfile.mkdtemp(prefix="vm2k8s-depends-")
    compose = os.path.join(tmp, "docker-compose.yml")
    with open(compose, "w", encoding="utf-8") as f:
        f.write("services:\n  db:\n    image: postgres:15\n    expose:\n"
                "      - '5432'\n  app:\n    image: nginx\n    depends_on:\n"
                "      db:\n        condition: service_healthy\n")
    docs, out = convert_path(compose, ["--depends-init"])
    app = deployments(docs)["app"]["spec"]["template"]["spec"]
    inits = app.get("initContainers") or []
    assert len(inits) == 1, inits
    assert inits[0]["image"] == "busybox:1.36"
    assert "db 5432" in inits[0]["command"][-1], inits
    migration = open(os.path.join(out, "MIGRATION.md"), encoding="utf-8").read()
    assert "depends_on 已近似" in migration


def t_blog():
    docs, _ = convert("blog")
    deps = deployments(docs)
    assert set(deps) == {"db", "wordpress"}, "deployments=%s" % list(deps)
    db = container(deps["db"])
    assert db["volumeMounts"][0]["mountPath"] == "/var/lib/mysql"
    assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "db-data"
               for v in deps["db"]["spec"]["template"]["spec"]["volumes"])
    wp_ports = [p["containerPort"] for p in container(deps["wordpress"]).get("ports", [])]
    assert 80 in wp_ports, "wordpress ports=%s" % wp_ports
    secret = {s["metadata"]["name"]: s for s in docs["02-secret.yaml"]}
    data = secret["migration-secret"]["stringData"]
    assert "MYSQL_ROOT_PASSWORD" in data and "WORDPRESS_DB_PASSWORD" in data
    cm = {c["metadata"]["name"]: c for c in docs["01-configmap.yaml"]}
    assert "WORDPRESS_DB_HOST" in cm["migration-config"]["data"]
    assert "MYSQL_DATABASE" in cm["migration-config"]["data"]


def t_monitoring():
    docs, _ = convert("monitoring")
    deps = deployments(docs)
    assert set(deps) == {"prometheus", "grafana"}
    cmd = container(deps["prometheus"]).get("command")
    assert cmd == ["--config.file=/etc/prometheus/prometheus.yml",
                   "--storage.tsdb.path=/prometheus"], "command=%s" % cmd
    mounts = container(deps["prometheus"]).get("volumeMounts", [])
    paths = [m["mountPath"] for m in mounts]
    assert "/prometheus" in paths
    assert "/etc/prometheus/prometheus.yml" not in paths, "missing bind file should be skipped"
    pvcs = {p["metadata"]["name"] for p in docs["05-pvcs.yaml"]}
    assert {"prom-data", "grafana-data"} <= pvcs, "pvcs=%s" % pvcs
    secret = {s["metadata"]["name"]: s for s in docs["02-secret.yaml"]}["migration-secret"]
    assert "GF_SECURITY_ADMIN_PASSWORD" in secret["stringData"]


def t_static_redis():
    docs, _ = convert("static-redis")
    deps = deployments(docs)
    assert set(deps) == {"web", "cache"}
    web = container(deps["web"])
    assert web["ports"][0]["containerPort"] == 80
    cache = deps["cache"]
    cmd = container(cache).get("command")
    assert cmd == ["/bin/sh", "-c",
                   'redis-server --requirepass "$CMD_CACHE_SECRET"'], \
        "command=%s" % cmd
    secret = {s["metadata"]["name"]: s for s in docs["02-secret.yaml"]}
    assert secret["migration-secret"]["stringData"]["CMD_CACHE_SECRET"] == \
        "secret123"
    assert "secret123" not in yaml.safe_dump(cache, allow_unicode=True)
    assert docs["05-pvcs.yaml"] == [], "should have no PVC"


def t_multi_build():
    docs, _ = convert("multi-build")
    deps = deployments(docs)
    assert set(deps) == {"api", "worker", "web"}
    assert container(deps["api"])["image"] == "local/api:latest"
    assert container(deps["worker"])["image"] == "local/worker:latest"
    assert container(deps["web"])["image"] == "nginx:1.25-alpine"
    assert container(deps["worker"])["command"] == ["/bin/sh", "-c", "python worker.py"]


def t_health_probe():
    docs, _ = convert("health-probe")
    dep = deployments(docs)["web"]
    c = container(dep)
    assert dep["spec"]["replicas"] == 2, "replicas=%s" % dep["spec"]["replicas"]
    probe = c["readinessProbe"]
    assert probe["httpGet"] == {"path": "/health", "port": 80}, "probe=%s" % probe
    assert probe["periodSeconds"] == 10 and probe["timeoutSeconds"] == 5
    assert probe["failureThreshold"] == 3 and probe["initialDelaySeconds"] == 15
    assert c["resources"]["limits"] == {"cpu": "500m", "memory": "256Mi"}
    assert c["resources"]["requests"] == {"cpu": "250m", "memory": "128Mi"}
    assert dep["metadata"]["labels"].get("tier") == "frontend"


def t_bind_secrets():
    docs, _ = convert("bind-secrets")
    c = container(deployments(docs)["web"])
    cms = {x["metadata"]["name"]: x for x in docs["01-configmap.yaml"]}
    assert "web-default.conf" in cms["migration-files"]["data"]
    assert "listen 80" in cms["migration-files"]["data"]["web-default.conf"]
    assert cms["migration-config"]["data"]["APP_TITLE"] == "Demo"
    assert cms["migration-config"]["data"]["CACHE_HOST"] == "cache"
    assert cms["migration-config"]["data"]["APP_MODE"] == "production"
    secrets = {s["metadata"]["name"]: s for s in docs["02-secret.yaml"]}
    assert secrets["compose-secret-db_password"]["stringData"]["db_password"].strip() == "s3cret"
    mount = [m for m in c["volumeMounts"] if m["mountPath"] == "/etc/nginx/conf.d/default.conf"]
    assert mount and mount[0]["subPath"] == "web-default.conf", "mounts=%s" % c["volumeMounts"]
    smount = [m for m in c["volumeMounts"] if m["mountPath"] == "/run/secrets/db_password"]
    assert smount, "secret mount missing: %s" % c["volumeMounts"]


def t_mixed_expose():
    docs, out = convert("mixed-expose")
    svcs = {s["metadata"]["name"]: s for s in docs["04-services.yaml"]}
    assert set(svcs) == {"api", "internal"}
    api_ports = {p["port"] for p in svcs["api"]["spec"]["ports"]}
    assert api_ports == {8000, 9000}, "api ports=%s" % api_ports
    script = open(os.path.join(out, "06-deploy.sh"), encoding="utf-8").read()
    assert "patch svc internal" not in script, "expose-only svc must not get NodePort"
    m = re.search(r"patch svc api -p '([^']+)'", script)
    assert m, "api patch missing: %s" % script
    patch = json.loads(m.group(1))
    assert patch["spec"]["type"] == "NodePort"
    by_port = {p["port"]: p for p in patch["spec"]["ports"]}
    assert by_port[8000]["nodePort"] == 30080
    assert "nodePort" not in by_port[9000], "expose-only port must not get nodePort"


def t_ingress():
    docs, _ = convert("mixed-expose",
                      extra=["--ingress", "--ingress-host", "example.com"])
    ing = docs["07-ingress.yaml"]
    assert len(ing) == 1, "ingress docs=%s" % len(ing)
    rule = ing[0]["spec"]["rules"][0]
    assert rule["host"] == "api.example.com", "rule=%s" % rule
    backend = rule["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "api"
    assert backend["port"]["number"] == 8000


def t_command_secret():
    docs, _ = convert_path(os.path.join(ROOT, "docker-compose.yml"))
    deps = deployments(docs)
    redis = container(deps["redis"])
    command = " ".join(str(x) for x in redis.get("command") or [])
    assert "xhh123456" not in command, command
    assert "$CMD_REDIS_SECRET" in command, command
    secret = {s["metadata"]["name"]: s for s in docs["02-secret.yaml"]}
    assert secret["migration-secret"]["stringData"]["CMD_REDIS_SECRET"] == \
        "xhh123456"
    for doc in docs["03-deployments.yaml"]:
        assert "xhh123456" not in yaml.safe_dump(doc, allow_unicode=True)


def main():
    cases = [
        ("blog(wordpress+mysql)", t_blog),
        ("monitoring(prometheus+grafana)", t_monitoring),
        ("static-redis(字符串command)", t_static_redis),
        ("multi-build(多build服务)", t_multi_build),
        ("health-probe(HTTP探针+resources+replicas)", t_health_probe),
        ("bind-secrets(bind文件→ConfigMap+compose secret+env_file)", t_bind_secrets),
        ("mixed-expose(expose-only不暴露NodePort)", t_mixed_expose),
        ("ingress(为发布HTTP服务生成Ingress)", t_ingress),
        ("command密码提取到Secret", t_command_secret),
        ("strict-bind拒绝目录bind静默跳过", t_strict_bind),
        ("depends-on生成TCP initContainer", t_depends_init),
    ]
    ok = all([check(n, f) for n, f in cases])
    print("\n结果: %s" % ("全部通过" if ok else "存在失败"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
