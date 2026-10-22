"""k8slab 纯函数测试（不需要云资源）"""
import os
import sys
import tempfile
from pathlib import Path

import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
sys.path.insert(0, ROOT)

import k8slab


def check(name, fn):
    try:
        fn()
        print("[PASS] %s" % name)
        return True
    except AssertionError as exc:
        print("[FAIL] %s: %s" % (name, exc))
        return False


def t_sanitize_name():
    assert k8slab.sanitize_name("My App!") == "my-app"
    assert k8slab.sanitize_name("") == "app"
    assert len(k8slab.sanitize_name("a" * 50)) == 20


def t_render_kubekey():
    out = tempfile.mkdtemp(prefix="k8slab-render-")
    outputs = {
        "master_private_ips": ["10.0.0.11", "10.0.0.12"],
        "worker_private_ips": ["10.0.0.21"],
        "api_endpoint": "10.0.0.100",
    }
    path = k8slab.render_kubekey(Path(out), outputs, "SecretOnlyForTest")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    hosts = data["spec"]["hosts"]
    assert [h["name"] for h in hosts] == ["master1", "master2", "worker1"]
    assert hosts[0]["password"] == "SecretOnlyForTest"
    assert data["spec"]["controlPlaneEndpoint"]["address"] == "10.0.0.100"

    outputs.pop("api_endpoint")
    path = k8slab.render_kubekey(Path(out), outputs, "SecretOnlyForTest")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["spec"]["controlPlaneEndpoint"]["address"] == "10.0.0.11"


def t_registry_config():
    out = tempfile.mkdtemp(prefix="k8slab-registry-")
    outputs = {
        "master_private_ips": ["10.0.0.11"],
        "worker_private_ips": ["10.0.0.21"],
    }
    path = k8slab.render_kubekey(
        Path(out), outputs, "pw",
        private_registry="harbor.example.com",
        local_registry=False,
        registry_namespace="k8s",
        registry_auth={"username": "user", "password": "pass"},
        insecure_registries=["harbor.example.com"])
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    registry = data["spec"]["registry"]
    assert registry["privateRegistry"] == "harbor.example.com"
    assert registry["namespaceOverride"] == "k8s"
    assert registry["insecureRegistries"] == ["harbor.example.com"]
    assert registry["auths"]["harbor.example.com"]["username"] == "user"
    assert registry["auths"]["harbor.example.com"]["password"] == "pass"
    assert registry["auths"]["harbor.example.com"]["skipTLSVerify"] is True
    assert "registry" not in data["spec"]["roleGroups"]

    path = k8slab.render_kubekey(
        Path(out), outputs, "pw", private_registry="10.0.0.11",
        local_registry=True)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["spec"]["roleGroups"]["registry"] == ["master1"]


def t_split_registry():
    assert k8slab.split_registry(
        "https://harbor.example.com/k8s/") == (
            "harbor.example.com", "k8s", "harbor.example.com/k8s")
    assert k8slab.split_registry("registry:5000") == (
        "registry:5000", "kubesphereio", "registry:5000")
    assert k8slab.split_registry("") == ("", "", "")


def main():
    cases = [
        ("sanitize_name", t_sanitize_name),
        ("render_kubekey", t_render_kubekey),
        ("registry_config", t_registry_config),
        ("split_registry", t_split_registry),
    ]
    ok = all([check(n, f) for n, f in cases])
    print("\n结果: %s" % ("全部通过" if ok else "存在失败"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
