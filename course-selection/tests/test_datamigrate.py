"""datamigrate 计划生成与 dry-run 测试"""
import json
import os
import subprocess
import sys
import tempfile
import types

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
REPO = os.path.dirname(ROOT)
DM = os.path.join(ROOT, "datamigrate.py")
FIXTURES = os.path.join(BASE, "fixtures")
ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def fixture(name):
    return os.path.join(FIXTURES, name, "docker-compose.yml")


def example(name):
    return os.path.join(REPO, "examples", name, "docker-compose.yml")


def run_dm(args):
    r = subprocess.run([sys.executable, DM] + args, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=ENV)
    assert r.returncode == 0, "datamigrate failed:\n%s\n%s" % (r.stdout, r.stderr)
    return r


def run_dm_fail(args):
    r = subprocess.run([sys.executable, DM] + args, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=ENV)
    assert r.returncode != 0, "datamigrate should fail:\n%s\n%s" % (
        r.stdout, r.stderr)
    return r


def plan(compose, extra=None):
    out = tempfile.mkdtemp(prefix="datamigrate-")
    args = ["plan", "--compose", compose, "--out", out,
            "--namespace", "test"] + (extra or [])
    run_dm(args)
    path = os.path.join(out, "migration-plan.json")
    assert os.path.isfile(path), "migration-plan.json missing"
    with open(path, encoding="utf-8") as f:
        return json.load(f), out


def jobs(plan_data):
    return {j["id"]: j for j in plan_data["jobs"]}


def check(name, fn):
    try:
        fn()
        print("[PASS] %s" % name)
        return True
    except AssertionError as exc:
        print("[FAIL] %s: %s" % (name, exc))
        return False


def t_mysql():
    data, out = plan(fixture("blog"))
    j = jobs(data)
    assert set(j) == {"db"}, "jobs=%s" % list(j)
    db = j["db"]
    assert db["kind"] == "mysql"
    assert db["service"] == "db"
    assert db["database"]["name"] == "wordpress"
    assert db["database"]["password_env"] == "MYSQL_ROOT_PASSWORD"
    assert db["volume"]["name"] == "db-data"
    assert db["artifact"] == "artifacts/db.sql"
    assert not db["skip_restore"]
    assert os.path.isfile(os.path.join(out, "RUNBOOK.md"))
    assert os.path.isfile(os.path.join(out, "scripts", "all.sh"))
    assert os.path.isfile(os.path.join(out, "scripts", "all.ps1"))


def t_postgres():
    data, _ = plan(example("library-app"))
    db = jobs(data)["db"]
    assert db["kind"] == "postgres"
    assert db["database"]["name"] == "library"
    assert db["database"]["password_env"] == "POSTGRES_PASSWORD"
    assert db["volume"]["name"] == "library-data"
    assert "library-app_library-data" in db["volume"]["candidates"]
    assert db["artifact"] == "artifacts/db.sql"


def t_sqlite():
    data, _ = plan(example("todo-app"))
    j = jobs(data)
    assert set(j) == {"backend"}, "jobs=%s" % list(j)
    b = j["backend"]
    assert b["kind"] == "sqlite"
    assert b["volume"]["name"] == "todo-data"
    assert "todo.db" in b["files"]
    assert "todo.db-wal" in b["files"]
    assert not b["skip_restore"]


def t_volumes():
    data, _ = plan(fixture("monitoring"))
    j = jobs(data)
    assert set(j) == {"volume-prom-data", "volume-grafana-data"}, "jobs=%s" % list(j)
    users = {job["volume"]["name"]: job["services"] for job in j.values()}
    assert users["prom-data"] == ["prometheus"], users
    assert users["grafana-data"] == ["grafana"], users
    for job in j.values():
        assert job["kind"] == "volume"


def t_no_data():
    data, _ = plan(fixture("bind-secrets"))
    assert data["jobs"] == [], "jobs=%s" % data["jobs"]


def t_redis_without_volume():
    data, _ = plan(example("url-shortener"))
    r = jobs(data)["redis"]
    assert r["kind"] == "redis"
    assert r["volume"] is None
    assert r["skip_restore"], "redis without volume must skip restore"
    assert any("redis" in w.lower() for w in r["warnings"])


def t_exporter_not_db():
    tmp = tempfile.mkdtemp(prefix="datamigrate-exporter-")
    compose = os.path.join(tmp, "docker-compose.yml")
    with open(compose, "w", encoding="utf-8") as f:
        f.write("services:\n"
                "  db:\n"
                "    image: mysql:8.0\n"
                "    environment:\n"
                "      MYSQL_ROOT_PASSWORD: pass\n"
                "      MYSQL_DATABASE: app\n"
                "    volumes: [db-data:/var/lib/mysql]\n"
                "  mysql-exporter:\n"
                "    image: prom/mysqld-exporter:v0.15.1\n"
                "volumes:\n"
                "  db-data:\n")
    data, _ = plan(compose)
    assert set(jobs(data)) == {"db"}, "jobs=%s" % list(jobs(data))


def t_volume_map():
    data, _ = plan(example("library-app"),
                   ["--volume-map", "library-data=custom_lib"])
    db = jobs(data)["db"]
    assert db["volume"]["candidates"][0] == "custom_lib", db["volume"]


def t_bind_dir_source_dir():
    tmp = tempfile.mkdtemp(prefix="datamigrate-bind-")
    compose = os.path.join(tmp, "docker-compose.yml")
    with open(compose, "w", encoding="utf-8") as f:
        f.write("services:\n"
                "  app:\n"
                "    image: nginx:1.25-alpine\n"
                "    volumes:\n"
                "      - ./data:/data\n")
    data, _ = plan(compose, ["--source-ssh", "deploy@example",
                            "--source-dir", "/srv/app",
                            "--include-bind-dirs"])
    assert data["source"]["dir"] == "/srv/app"
    j = jobs(data)
    assert set(j) == {"bind-app"}, "jobs=%s" % list(j)
    job = j["bind-app"]
    assert job["kind"] == "bind"
    assert job["volume"]["source"] == "/srv/app/data"
    assert job["skip_restore"]


def t_dry_run():
    data, out = plan(fixture("blog"))
    plan_path = os.path.join(out, "migration-plan.json")
    r = run_dm(["run", "--plan", plan_path, "--phase", "all", "--dry-run"])
    assert "[dry-run]" in r.stdout
    assert not os.path.isfile(os.path.join(out, "artifacts", "db.sql"))


def t_dry_run_no_stop_apps_volume():
    data, out = plan(fixture("monitoring"))
    plan_path = os.path.join(out, "migration-plan.json")
    run_dm_fail(["run", "--plan", plan_path, "--phase", "restore",
                 "--dry-run", "--no-stop-apps"])


def t_dry_run_no_jobs():
    data, out = plan(fixture("bind-secrets"))
    plan_path = os.path.join(out, "migration-plan.json")
    run_dm(["run", "--plan", plan_path, "--phase", "all", "--dry-run"])


def t_postgres_query_flags():
    sys.path.insert(0, ROOT)
    import datamigrate as dm
    script = dm.postgres_query_script("SELECT 1", "library")
    assert " -t -A " in script, script
    assert " -N " not in script and " -B " not in script, script


def t_target_dir_home():
    sys.path.insert(0, ROOT)
    import datamigrate as dm
    assert dm.default_target_dir("devops@host", "app") == \
        "/home/devops/datamigrate/app"
    assert dm.default_target_dir("root@host", "app") == \
        "/root/datamigrate/app"


def t_container_project_isolation():
    sys.path.insert(0, ROOT)
    import datamigrate as dm

    class FakeSource:
        project = "todo-app"

        def __init__(self):
            self.calls = []

        def run(self, argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

    fake = FakeSource()
    ctx = types.SimpleNamespace(source=fake, plan={"options": {}})
    job = {"service": "backend", "services": ["backend"]}
    assert dm.resolve_container(ctx, job) == ""
    assert len(fake.calls) == 1, fake.calls


def t_bind_file_no_warning():
    data, _ = plan(example("library-app"))
    assert not any("nginx.conf" in w for w in data["warnings"]), \
        data["warnings"]


def t_artifact_manifest():
    sys.path.insert(0, ROOT)
    import datamigrate as dm
    out = tempfile.mkdtemp(prefix="datamigrate-manifest-")
    artifacts = os.path.join(out, "artifacts")
    os.makedirs(artifacts)
    for name in ("db.sql", "db.counts.json", "db.sql.manifest.json",
                 "stale.sql"):
        with open(os.path.join(artifacts, name), "w", encoding="utf-8") as f:
            f.write("x")
    ctx = types.SimpleNamespace(
        artifacts=artifacts,
        plan={"jobs": [{"artifact": "artifacts/db.sql",
                        "counts": "artifacts/db.counts.json"}]})
    ctx.jobs = ctx.plan["jobs"]
    dm.write_artifact_manifest(ctx)
    with open(os.path.join(artifacts, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    names = {os.path.basename(x["path"]) for x in manifest["files"]}
    assert names == {"db.sql", "db.counts.json", "db.sql.manifest.json"}, names


def main():
    cases = [
        ("blog(mysql)", t_mysql),
        ("library-app(postgres)", t_postgres),
        ("todo-app(sqlite)", t_sqlite),
        ("monitoring(命名卷)", t_volumes),
        ("bind-secrets(无数据任务)", t_no_data),
        ("url-shortener(redis无卷)", t_redis_without_volume),
        ("exporter不误判为数据库", t_exporter_not_db),
        ("volume-map源卷名覆盖", t_volume_map),
        ("bind目录+source-dir", t_bind_dir_source_dir),
        ("dry-run(all)", t_dry_run),
        ("dry-run拒绝--no-stop-apps恢复卷", t_dry_run_no_stop_apps_volume),
        ("dry-run(无任务)", t_dry_run_no_jobs),
        ("postgres查询参数有效", t_postgres_query_flags),
        ("非root目标remote_dir", t_target_dir_home),
        ("容器解析不跨项目回退", t_container_project_isolation),
        ("bind文件不误报", t_bind_file_no_warning),
        ("artifact manifest只含计划产物", t_artifact_manifest),
    ]
    ok = all([check(n, f) for n, f in cases])
    print("\n结果: %s" % ("全部通过" if ok else "存在失败"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
