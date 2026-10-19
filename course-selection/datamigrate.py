#!/usr/bin/env python3
"""
datamigrate.py —— Docker Compose → Kubernetes 数据迁移工具

用法:
  python datamigrate.py plan --compose docker-compose.yml --out data-migration/app \
      --namespace app [--source-ssh user@vm --source-key id_ed25519] \
      [--target-ssh root@master --target-key id_ed25519]

  python datamigrate.py run --plan data-migration/app/migration-plan.json \
      --phase all [--dry-run] [--yes] [--resume]

阶段:
  preflight → dump → transfer → restore → verify → start

支持:
  MySQL / MariaDB / PostgreSQL / Redis / MongoDB 逻辑备份
  SQLite 与命名文件卷归档
  源端 Docker（本机或 SSH）、目标端 kubectl（本机或 SSH）
  生成可审阅的 shell/PowerShell 脚本、RUNBOOK 与 JSON 计划
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time

try:
    import yaml
except ImportError:
    sys.exit("需要 pyyaml：python -m pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import vm2k8s

PHASES = ["preflight", "dump", "transfer", "restore", "verify", "start"]
DB_KINDS = ("mysql", "postgres", "redis", "mongo")
DB_DATA_MOUNTS = {
    "mysql": ("/var/lib/mysql",),
    "postgres": ("/var/lib/postgresql/data", "/var/lib/postgresql"),
    "redis": ("/data",),
    "mongo": ("/data/db", "/data", "/var/lib/mongodb"),
}
HTTP_PORTS = vm2k8s.HTTP_PORTS


class MigrationError(Exception):
    pass


def log(msg=""):
    print(msg, flush=True)


def die(msg, code=1):
    raise SystemExit("错误: %s" % msg) if code else None


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def text(result):
    out = result.stdout or b""
    if isinstance(out, str):
        return out
    return out.decode("utf-8", "replace")


def err_text(result):
    out = result.stderr or b""
    if isinstance(out, str):
        return out
    return out.decode("utf-8", "replace")


def split_csv(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def parse_kv(value):
    out = {}
    for part in split_csv(value):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def sanitize_name(value, default="app"):
    name = re.sub(r"[^a-z0-9-]", "-", str(value or "").lower()).strip("-")
    name = re.sub(r"-+", "-", name)
    return name or default


def resolve_path(base, path):
    path = str(path)
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base, path))


def sql_lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def ident_mysql(value):
    return "`" + str(value).replace("`", "``") + "`"


def ident_pg(value):
    return '"' + str(value).replace('"', '""') + '"'


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def format_size(num):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024 or unit == "TiB":
            return "%.1f %s" % (num, unit)
        num /= 1024.0


def remote_join(argv):
    return " ".join(shlex.quote(str(x)) for x in argv)


def ssh_argv(remote, key=""):
    argv = ["ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if key:
        argv += ["-i", key]
    return argv + [remote]


def scp_argv(key=""):
    argv = ["scp", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if key:
        argv += ["-i", key]
    return argv


def run(argv, stdin_path=None, stdout_path=None, input_text=None,
        capture=False, check=True, dry_run=False, timeout=None):
    argv = [str(x) for x in argv]
    printable = " ".join(shlex.quote(x) for x in argv)
    log("+ " + printable)
    if dry_run:
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    stdin = stdout = None
    try:
        if stdin_path:
            stdin = open(stdin_path, "rb")
        if stdout_path:
            stdout = open(stdout_path, "wb")
        elif capture:
            stdout = subprocess.PIPE
        stderr = subprocess.PIPE if (capture or stdout_path) else None
        r = subprocess.run(
            argv, stdin=stdin, stdout=stdout, stderr=stderr,
            input=input_text.encode("utf-8") if input_text is not None else None,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise MigrationError("找不到命令: %s" % argv[0])
    except subprocess.TimeoutExpired:
        raise MigrationError("命令超时: %s" % printable)
    finally:
        if stdin:
            stdin.close()
        if stdout and stdout_path:
            stdout.close()
    if check and r.returncode != 0:
        raise MigrationError("命令失败(exit=%d): %s\n%s"
                             % (r.returncode, printable, err_text(r)))
    return r


class Source:
    def __init__(self, ssh="", key="", project=""):
        self.ssh = ssh
        self.key = key
        self.project = project

    def host_argv(self, argv):
        if self.ssh:
            return ssh_argv(self.ssh, self.key) + [remote_join(argv)]
        return list(argv)

    def run(self, argv, **kwargs):
        return run(self.host_argv(argv), **kwargs)


class Target:
    def __init__(self, ssh="", key="", kubectl="kubectl"):
        self.ssh = ssh
        self.key = key
        self.kubectl = kubectl

    def host_run(self, argv, **kwargs):
        if self.ssh:
            return run(ssh_argv(self.ssh, self.key) + [remote_join(argv)], **kwargs)
        return run(argv, **kwargs)

    def kubectl_run(self, argv, **kwargs):
        cmd = [self.kubectl] + [str(x) for x in argv]
        if self.ssh:
            return run(ssh_argv(self.ssh, self.key) + [remote_join(cmd)], **kwargs)
        return run(cmd, **kwargs)


class Ctx:
    def __init__(self, plan, args):
        self.plan = plan
        self.args = args
        self.outdir = os.path.dirname(os.path.abspath(args.plan))
        self.artifacts = os.path.join(self.outdir, "artifacts")
        self.dry_run = args.dry_run
        self.timeout = args.timeout
        self.state = load_state(self.outdir)
        source = plan.get("source") or {}
        target = plan.get("target") or {}
        self.source = Source(source.get("ssh", ""), source.get("key", ""),
                             source.get("project", ""))
        self.target = Target(target.get("ssh", ""), target.get("key", ""),
                             target.get("kubectl", "kubectl"))
        only = set(split_csv(args.only))
        self.jobs = [j for j in plan.get("jobs", [])
                     if not only or j.get("id") in only
                     or j.get("service") in only
                     or any(s in only for s in j.get("services", []))]
        self._inspect = {}

    @property
    def namespace(self):
        return self.plan["target"]["namespace"]

    def artifact_path(self, job):
        return os.path.join(self.outdir, job["artifact"])


def load_state(outdir):
    path = os.path.join(outdir, ".state.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {"version": 1, "phases": {}, "stopped": {}, "verify": {}}


def save_state(ctx):
    ctx.state["updated"] = now()
    path = os.path.join(ctx.outdir, ".state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ctx.state, f, ensure_ascii=False, indent=2)


def state_job(ctx, phase, job_id, **values):
    phase_state = ctx.state.setdefault("phases", {}).setdefault(phase, {})
    jobs = phase_state.setdefault("jobs", {})
    item = jobs.setdefault(job_id, {})
    item.update(values)
    item["at"] = now()
    save_state(ctx)


def state_job_done(ctx, phase, job_id):
    return bool(ctx.state.get("phases", {}).get(phase, {})
                .get("jobs", {}).get(job_id, {}).get("done"))


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def detect_db_kind(name, svc):
    image = str(svc.get("image") or "").lower()
    text_value = (str(name) + " " + image).lower()
    if any(x in text_value for x in ("exporter", "commander", "adminer",
                                     "phpmyadmin", "backup")):
        return ""
    if "mariadb" in text_value:
        return "mysql"
    if "mysql" in text_value:
        return "mysql"
    if "postgres" in text_value or "postgis" in text_value:
        return "postgres"
    if "redis" in text_value or "valkey" in text_value:
        return "redis"
    if "mongo" in text_value:
        return "mongo"
    return ""


def find_mount_for_path(mounts, path):
    best = None
    target = str(path).rstrip("/") or "/"
    for mount in mounts:
        mp = str(mount.get("mountPath") or "").rstrip("/") or "/"
        if not mp:
            continue
        if (target == mp or target.startswith(mp + "/")
                or mp.startswith(target + "/")):
            if best is None or len(mp) > len(str(best.get("mountPath"))):
                best = mount
    return best


def find_db_mount(kind, mounts, bind_files):
    for candidate in DB_DATA_MOUNTS.get(kind, ()):
        mount = find_mount_for_path(mounts, candidate)
        if mount:
            return {"type": "volume", "name": mount["name"],
                    "mount": mount["mountPath"], "claim": mount["name"]}
        for bf in bind_files:
            target = str(bf.get("target") or "")
            if target.rstrip("/") == candidate.rstrip("/") or \
                    target.startswith(candidate.rstrip("/") + "/"):
                return {"type": "bind", "source": bf.get("source"),
                        "mount": target, "claim": None}
    return None


def detect_sqlite_paths(env, svc):
    values = list(env.values())
    for key in ("command", "entrypoint"):
        raw = svc.get(key)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(str(x) for x in raw)
    paths = set()
    for value in values:
        for match in re.finditer(r"/[^\s\"'=,;]+\.(?:db|sqlite|sqlite3)", str(value)):
            paths.add(match.group(0))
    return sorted(paths)


def make_db_job(name, kind, svc, env, mounts, bind_files, options, db_map):
    image = str(svc.get("image") or "")
    warnings = []
    if not image:
        return None, ["[%s] 无 image，跳过数据迁移" % name]
    volume = find_db_mount(kind, mounts, bind_files)
    if volume is None:
        warnings.append("[%s] 未发现 %s 数据卷，数据可能未持久化" % (name, kind))
    elif volume["type"] == "bind":
        warnings.append("[%s] %s 数据使用 bind 挂载，目标端未自动创建；只做逻辑备份"
                        % (name, kind))
    if options.get("ephemeral") and volume:
        warnings.append("[%s] --ephemeral 下目标使用 emptyDir，原始卷不会恢复"
                        % name)
    skip_restore = False
    if kind == "redis" and (volume is None or
                            volume.get("type") != "volume" or
                            options.get("ephemeral")):
        skip_restore = True
        if volume is not None:
            warnings.append("[%s] Redis 数据无法通过 PVC 恢复，跳过恢复" % name)

    if kind == "mysql":
        flavor = "mariadb" if "mariadb" in image.lower() else "mysql"
        db_name = db_map.get(name) or env.get("MYSQL_DATABASE") or \
            env.get("MARIADB_DATABASE") or ""
        user = "root" if env.get("MYSQL_ROOT_PASSWORD") else \
            (env.get("MYSQL_USER") or "root")
        password_env = "MYSQL_ROOT_PASSWORD" if env.get("MYSQL_ROOT_PASSWORD") else \
            ("MYSQL_PASSWORD" if env.get("MYSQL_PASSWORD") else "")
        database = {"name": db_name, "user": user,
                    "password_env": password_env, "flavor": flavor}
        ext = ".sql"
    elif kind == "postgres":
        db_name = db_map.get(name) or env.get("POSTGRES_DB") or \
            env.get("POSTGRES_USER") or "postgres"
        user = env.get("POSTGRES_USER") or "postgres"
        database = {"name": db_name, "user": user,
                    "password_env": "POSTGRES_PASSWORD"
                    if env.get("POSTGRES_PASSWORD") else ""}
        ext = ".sql"
    elif kind == "redis":
        auth_env = ""
        for key in ("REDIS_PASSWORD", "REDIS_PASS", "REDISCLI_AUTH"):
            if env.get(key):
                auth_env = key
                break
        command_text = " ".join(str(x) for x in (
            [svc.get("command")] if isinstance(svc.get("command"), str)
            else (svc.get("command") or [])))
        appendonly = bool(re.search(r"appendonly\s+yes", command_text, re.I)
                          or re.search(r"appendonly\s+yes",
                                       str(env.get("REDIS_ARGS") or ""), re.I))
        database = {"name": env.get("REDIS_DB") or "0",
                    "password_env": auth_env, "cmd_flag": "--requirepass",
                    "appendonly": appendonly}
        if appendonly:
            warnings.append("[%s] Redis 启用了 AOF，恢复 RDB 后可能被 AOF 覆盖"
                            % name)
        ext = ".rdb"
    else:
        database = {"user": env.get("MONGO_INITDB_ROOT_USERNAME") or "",
                    "password_env": "MONGO_INITDB_ROOT_PASSWORD"
                    if env.get("MONGO_INITDB_ROOT_PASSWORD") else "",
                    "auth_db": "admin",
                    "name": db_map.get(name) or env.get("MONGO_INITDB_DATABASE") or ""}
        ext = ".archive.gz"

    job = {
        "id": sanitize_name(name),
        "service": name,
        "services": [name],
        "kind": kind,
        "image": image,
        "volume": volume,
        "database": database,
        "artifact": "artifacts/%s%s" % (sanitize_name(name), ext),
        "counts": "artifacts/%s.counts.json" % sanitize_name(name),
        "warnings": warnings,
        "skip_restore": skip_restore,
    }
    return job, warnings


def make_sqlite_jobs(name, svc, env, mounts, bind_files, options):
    jobs = []
    warnings = []
    paths = detect_sqlite_paths(env, svc)
    if not paths:
        return jobs, warnings
    grouped = {}
    for path in paths:
        mount = find_mount_for_path(mounts, path)
        if mount:
            mp = mount["mountPath"].rstrip("/") or "/"
            rel = path[len(mp):].lstrip("/") if path.startswith(mp) else path.lstrip("/")
            key = ("volume", mount["name"], mount["mountPath"])
            grouped.setdefault(key, []).append(rel)
            continue
        bind = None
        for bf in bind_files:
            target = str(bf.get("target") or "").rstrip("/")
            if target and (path == target or path.startswith(target + "/")):
                bind = bf
                break
        if bind:
            warnings.append("[%s] SQLite 位于 bind 目录 %s，目标端未自动转换，需手工迁移"
                            % (name, bind.get("source")))
        else:
            warnings.append("[%s] 检测到 SQLite 路径 %s，但不在命名卷中，跳过"
                            % (name, path))
    for idx, (key, files) in enumerate(sorted(grouped.items()), 1):
        _, volume_name, mount_path = key
        files = sorted(set(files))
        extras = []
        for rel in files:
            for suffix in ("-wal", "-shm", "-journal"):
                if rel + suffix not in files:
                    extras.append(rel + suffix)
        job_id = sanitize_name(name) if idx == 1 else \
            "%s-%d" % (sanitize_name(name), idx)
        jobs.append({
            "id": job_id,
            "service": name,
            "services": [name],
            "kind": "sqlite",
            "image": str(svc.get("image") or ""),
            "volume": {"type": "volume", "name": volume_name,
                       "mount": mount_path, "claim": volume_name},
            "files": files + extras,
            "artifact": "artifacts/%s.tar.gz" % job_id,
            "skip_restore": bool(options.get("ephemeral")),
            "quiesce": True,
            "warnings": (["[%s] --ephemeral 下 SQLite 数据卷不会恢复" % name]
                         if options.get("ephemeral") else []),
        })
    return jobs, warnings


def default_target_dir(ssh, namespace):
    user = (ssh or "").split("@", 1)[0]
    home = "/root" if user in ("", "root") else "/home/%s" % user
    return posixpath.join(home, "datamigrate", namespace)


def build_plan(compose, compose_dir, args):
    services = compose.get("services") or {}
    include = set(split_csv(args.include))
    exclude = set(split_csv(args.exclude))
    selected = [n for n in services
                if (not include or n in include) and n not in exclude]
    source_dir = args.source_dir or ""

    def bind_path(raw):
        raw = str(raw or "")
        if source_dir and not os.path.isabs(raw):
            if re.match(r"^[A-Za-z]:[\\/]", source_dir):
                return os.path.normpath(os.path.join(source_dir, raw))
            return posixpath.normpath(posixpath.join(source_dir, raw))
        return resolve_path(compose_dir, raw)

    db_map = parse_kv(args.database_map)
    container_map = parse_kv(args.container_map)
    options = {
        "ephemeral": bool(args.ephemeral),
        "include_bind_dirs": bool(args.include_bind_dirs),
        "verify_rows": bool(args.verify_rows),
        "quiesce": bool(args.quiesce),
        "stop_apps": True,
        "helper_image": args.helper_image or "",
        "container_map": container_map,
        "volume_map": parse_kv(args.volume_map),
    }
    warnings = []
    jobs = []
    covered_volumes = set()
    db_services = set()
    all_mounts = {}
    has_bind = False

    for name in selected:
        svc = services[name]
        env = vm2k8s.env_dict(svc, compose_dir)
        mounts, named, empty, bind_files, vwarn = vm2k8s.deploy_mounts(svc)
        all_mounts[name] = mounts
        warnings.extend("[%s] %s" % (name, w) for w in vwarn)
        for path in empty:
            warnings.append("[%s] 匿名卷 %s 转为 emptyDir，不迁移" % (name, path))
        for bf in bind_files:
            raw = bf.get("source") or ""
            src = bind_path(raw)
            local_src = raw if os.path.isabs(raw) else \
                os.path.join(compose_dir, raw)
            if os.path.isfile(local_src) or os.path.isfile(src):
                continue
            has_bind = True
            is_dir = os.path.isdir(local_src) or os.path.isdir(src)
            if not is_dir and args.source_ssh and source_dir:
                is_dir = True
            if is_dir:
                if options["include_bind_dirs"]:
                    job_id = sanitize_name("bind-%s" % name)
                    jobs.append({
                        "id": job_id,
                        "service": name,
                        "services": [name],
                        "kind": "bind",
                        "image": str(svc.get("image") or ""),
                        "volume": {"type": "bind", "source": src,
                                   "mount": bf.get("target"), "claim": None},
                        "artifact": "artifacts/%s.tar.gz" % job_id,
                        "skip_restore": True,
                        "warnings": ["[%s] bind 目录未由转换器创建，只归档不恢复"
                                     % name],
                    })
                else:
                    warnings.append("[%s] bind 目录 %s 未转换，不迁移数据"
                                    % (name, bf.get("source")))
        kind = detect_db_kind(name, svc)
        if kind:
            db_services.add(name)
            job, jwarn = make_db_job(name, kind, svc, env, mounts,
                                     bind_files, options, db_map)
            warnings.extend(jwarn)
            if job:
                job["container_override"] = container_map.get(name, "")
                jobs.append(job)
                if job.get("volume") and job["volume"].get("type") == "volume":
                    covered_volumes.add(job["volume"]["name"])
        else:
            sjobs, swarn = make_sqlite_jobs(name, svc, env, mounts,
                                            bind_files, options)
            warnings.extend(swarn)
            for job in sjobs:
                job["container_override"] = container_map.get(name, "")
                jobs.append(job)
                covered_volumes.add(job["volume"]["name"])

    volume_users = {}
    for name in selected:
        for mount in all_mounts.get(name, []):
            volume_users.setdefault(mount["name"], {
                "services": set(), "mount": mount["mountPath"],
            })["services"].add(name)

    for volume_name, info in sorted(volume_users.items()):
        if volume_name in covered_volumes:
            continue
        users = sorted(info["services"])
        job_id = sanitize_name("volume-%s" % volume_name)
        skip = bool(options.get("ephemeral"))
        vwarn = []
        if skip:
            vwarn.append("--ephemeral 下命名卷 %s 不恢复" % volume_name)
        if any(s in db_services for s in users):
            skip = True
            vwarn.append("卷 %s 被数据库服务使用，跳过原始卷恢复" % volume_name)
        jobs.append({
            "id": job_id,
            "service": None,
            "services": users,
            "kind": "volume",
            "image": volume_image(services, users),
            "volume": {"type": "volume", "name": volume_name,
                       "mount": info["mount"], "claim": volume_name},
            "artifact": "artifacts/%s.tar.gz" % job_id,
            "skip_restore": skip,
            "warnings": vwarn,
        })

    if not jobs:
        warnings.append("未发现可迁移的数据库或命名卷")
    if not args.source_ssh:
        warnings.append("源端未指定 --source-ssh，默认使用本机 Docker")
    elif not source_dir and has_bind:
        warnings.append("源端为 SSH 但未指定 --source-dir，bind 相对路径按本机目录解析")
    if not args.target_ssh:
        warnings.append("目标端未指定 --target-ssh，默认使用本机 kubectl")

    project = compose_project_name(compose, compose_dir, args.source_project)
    add_volume_candidates(jobs, project, options.get("volume_map"))
    namespace = sanitize_name(args.namespace or os.path.basename(compose_dir))
    outdir = os.path.abspath(args.out or os.path.join("data-migration", namespace))
    target_dir = args.target_dir or default_target_dir(args.target_ssh, namespace)
    source_key = os.path.abspath(os.path.expanduser(args.source_key)) \
        if args.source_key else ""
    target_key = os.path.abspath(os.path.expanduser(args.target_key)) \
        if args.target_key else ""
    plan = {
        "version": 1,
        "tool": os.path.abspath(__file__),
        "created": now(),
        "compose": os.path.abspath(args.compose),
        "compose_dir": os.path.abspath(compose_dir),
        "out": outdir,
        "source": {
            "ssh": args.source_ssh or "",
            "key": source_key,
            "project": args.source_project or project,
            "dir": source_dir,
        },
        "target": {
            "ssh": args.target_ssh or "",
            "key": target_key,
            "kubectl": args.kubectl or "kubectl",
            "namespace": namespace,
            "remote_dir": target_dir,
        },
        "services": sorted(selected),
        "options": options,
        "jobs": jobs,
        "warnings": warnings,
    }
    return plan


def compose_project_name(compose, compose_dir, override=""):
    raw = str(override or compose.get("name") or
              os.path.basename(compose_dir) or "app")
    name = re.sub(r"[^a-z0-9_-]", "", raw.lower()).strip("_-")
    return name or "app"


def add_volume_candidates(jobs, project, volume_map=None):
    volume_map = volume_map or {}
    for job in jobs:
        volume = job.get("volume") or {}
        if volume.get("type") != "volume" or not volume.get("name"):
            continue
        candidates = [volume_map.get(volume["name"], ""),
                      project + "_" + volume["name"], volume["name"]]
        seen = []
        for value in candidates:
            if value and value not in seen:
                seen.append(value)
        volume["candidates"] = seen
    return jobs


def volume_image(services, users):
    for name in users:
        image = str((services.get(name) or {}).get("image") or "")
        if image:
            return image
    return ""


def generate_plan(args):
    compose = vm2k8s.load_compose(args.compose)
    compose_dir = os.path.dirname(os.path.abspath(args.compose))
    plan = build_plan(compose, compose_dir, args)
    outdir = plan["out"]
    os.makedirs(outdir, exist_ok=True)
    write_json(os.path.join(outdir, "migration-plan.json"), plan)
    write_wrappers(plan, outdir)
    write_runbook(plan, outdir)
    print_plan(plan)
    return plan


def write_wrappers(plan, outdir):
    scripts = os.path.join(outdir, "scripts")
    os.makedirs(scripts, exist_ok=True)
    tool = plan["tool"].replace('"', '\\"')
    tool_sh = shlex.quote(plan["tool"].replace("\\", "/"))
    phases = [(name, name) for name in PHASES] + [("all", "all")]
    for filename, phase in phases:
        sh_path = os.path.join(scripts, "%s.sh" % filename)
        with open(sh_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    'PLAN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/migration-plan.json"\n'
                    'PY="${PYTHON:-python3}"\n'
                    'command -v "$PY" >/dev/null 2>&1 || PY=python\n'
                    'exec "$PY" %s run --plan "$PLAN" --phase %s "$@"\n'
                    % (tool_sh, phase))
        try:
            os.chmod(sh_path, 0o755)
        except OSError:
            pass
        ps_path = os.path.join(scripts, "%s.ps1" % filename)
        with open(ps_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write('$ErrorActionPreference = "Stop"\n'
                    '$Plan = Join-Path (Split-Path -Parent $PSScriptRoot) "migration-plan.json"\n'
                    '& python "%s" run --plan $Plan --phase %s @args\n'
                    % (tool, phase))
        with open(os.path.join(scripts, "README.txt"), "w",
                  encoding="utf-8", newline="\r\n") as f:
            f.write("脚本是 datamigrate.py 的薄封装，可在执行前审阅 RUNBOOK.md。\n"
                    "Linux/macOS: bash scripts/01-dump.sh\n"
                    "Windows:     powershell -File scripts/01-dump.ps1\n"
                    "所有脚本都支持追加参数，例如 --dry-run、--yes、--resume。\n")


def write_runbook(plan, outdir):
    lines = ["# 数据迁移执行手册（datamigrate 自动生成）", "",
             "- 生成时间: %s" % plan["created"],
             "- compose: `%s`" % plan["compose"],
             "- 命名空间: `%s`" % plan["target"]["namespace"],
             "- 源端: `%s`" % (plan["source"]["ssh"] or "本机 Docker"),
             "- 目标端: `%s`" % (plan["target"]["ssh"] or "本机 kubectl"),
             "- 计划文件: `migration-plan.json`", "",
             "## 迁移任务", "",
             "| 任务 | 类型 | 服务 | 数据卷 | 产物 | 恢复方式 |",
             "|---|---|---|---|---|---|"]
    for job in plan["jobs"]:
        volume = job.get("volume") or {}
        volume_name = volume.get("name") or volume.get("source") or "-"
        mode = "跳过" if job.get("skip_restore") else \
            ("逻辑恢复" if job["kind"] in DB_KINDS else "卷恢复")
        lines.append("| %s | %s | %s | %s | `%s` | %s |"
                     % (job["id"], job["kind"],
                        job.get("service") or ",".join(job.get("services") or []),
                        volume_name, job["artifact"], mode))
    tool = plan["tool"].replace("\\", "/")
    lines += ["", "## 执行顺序", "",
              "先在目标集群完成应用部署（`vm2k8s.py ship` 或 `06-deploy.sh`）。",
              "以下命令在计划目录执行（也可把 `--plan` 换成绝对路径）：", "",
              "```bash",
              'python "%s" run \\' % tool,
              "  --plan migration-plan.json --phase preflight",
              'python "%s" run \\' % tool,
              "  --plan migration-plan.json --phase dump",
              'python "%s" run \\' % tool,
              "  --plan migration-plan.json --phase transfer",
              'python "%s" run \\' % tool,
              "  --plan migration-plan.json --phase restore --yes",
              'python "%s" run \\' % tool,
              "  --plan migration-plan.json --phase verify",
              'python "%s" run \\' % tool,
              "  --plan migration-plan.json --phase start",
              "```", "",
              "也可以直接运行 `scripts/all.sh`（Windows PowerShell 用 `scripts/all.ps1`）。",
              "`--phase all` 会按顺序执行全部阶段。`--dry-run` 只打印命令。", "",
              "## 安全与回滚", "",
              "- `restore` 会先缩容非数据库服务，恢复完成前保持停止；`start` 恢复副本数。",
              "- 恢复失败时应用保持停止，修复后先执行 `--phase verify`，再执行 `--phase start`。",
              "- Redis 使用 RDB 文件恢复，要求目标端 Redis 数据目录可写且未启用 AOF。",
              "- SQLite 与文件卷要求目标 Deployment 已停止；工具会通过临时 Pod 解包。",
              "- 产物包含数据库内容，默认位于 `artifacts/`，请按敏感数据管理。",
              "- 数据库逻辑恢复会覆盖同名对象；生产环境先在预发环境演练。", "",
              "## 告警", ""]
    lines += ["- " + w for w in plan.get("warnings", [])] or ["- 无"]
    lines += ["", "## 各任务命令摘要", ""]
    for job in plan["jobs"]:
        lines += ["### %s（%s）" % (job["id"], job["kind"]), ""]
        lines.append("- 产物: `%s`" % job["artifact"])
        if job["kind"] in DB_KINDS:
            lines.append("- 源端容器需运行，备份通过容器内客户端完成")
        if job.get("volume"):
            lines.append("- 数据卷: `%s`，挂载点 `%s`"
                         % (job["volume"].get("name") or job["volume"].get("source"),
                            job["volume"].get("mount")))
        if job.get("files"):
            lines.append("- 文件: %s" % ", ".join("`%s`" % x for x in job["files"]))
        for warning in job.get("warnings", []):
            lines.append("- 告警: %s" % warning)
        lines.append("")
    with open(os.path.join(outdir, "RUNBOOK.md"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def print_plan(plan):
    print("=" * 78)
    print("数据迁移计划")
    print("=" * 78)
    print("命名空间: %s" % plan["target"]["namespace"])
    print("输出目录: %s" % plan["out"])
    print("")
    print("%-20s %-10s %-18s %s" % ("任务", "类型", "服务", "产物"))
    print("-" * 78)
    for job in plan["jobs"]:
        print("%-20s %-10s %-18s %s"
              % (job["id"], job["kind"],
                 job.get("service") or ",".join(job.get("services") or []),
                 job["artifact"]))
    if not plan["jobs"]:
        print("(无可迁移任务)")
    print("")
    if plan.get("warnings"):
        print("告警:")
        for warning in plan["warnings"]:
            print("  [WARN] %s" % warning)
        print("")
    print("下一步: python %s run --plan %s --phase preflight"
          % (plan["tool"], os.path.join(plan["out"], "migration-plan.json")))


def apply_run_overrides(plan, args):
    if args.source_ssh:
        plan["source"]["ssh"] = args.source_ssh
    if args.source_key:
        plan["source"]["key"] = os.path.abspath(os.path.expanduser(args.source_key))
    if args.source_project:
        plan["source"]["project"] = args.source_project
        project = compose_project_name({}, "", args.source_project)
        for job in plan.get("jobs", []):
            volume = job.get("volume") or {}
            if volume.get("type") == "volume" and volume.get("name"):
                volume["candidates"] = [project + "_" + volume["name"],
                                        volume["name"]]
    if args.source_dir:
        plan["source"]["dir"] = args.source_dir
    if args.target_ssh:
        plan["target"]["ssh"] = args.target_ssh
    if args.target_key:
        plan["target"]["key"] = os.path.abspath(os.path.expanduser(args.target_key))
    if args.target_dir:
        plan["target"]["remote_dir"] = args.target_dir
    if args.namespace:
        plan["target"]["namespace"] = sanitize_name(args.namespace)
    if args.kubectl:
        plan["target"]["kubectl"] = args.kubectl
    if args.helper_image:
        plan["options"]["helper_image"] = args.helper_image
    if args.volume_map:
        volume_map = parse_kv(args.volume_map)
        plan["options"]["volume_map"] = volume_map
        for job in plan.get("jobs", []):
            volume = job.get("volume") or {}
            name = volume.get("name")
            if volume.get("type") == "volume" and name in volume_map:
                volume["candidates"] = [volume_map[name]] + [
                    x for x in (volume.get("candidates") or [name])
                    if x != volume_map[name]]
    if args.verify_rows is not None:
        plan["options"]["verify_rows"] = args.verify_rows
    if args.stop_apps is not None:
        plan["options"]["stop_apps"] = args.stop_apps
    if args.clean_volume is not None:
        plan["options"]["clean_volume"] = args.clean_volume
    if args.quiesce is not None:
        plan["options"]["quiesce"] = args.quiesce


def resolve_container(ctx, job):
    override = job.get("container_override") or \
        (ctx.plan.get("options", {}).get("container_map") or {}).get(job.get("service"))
    if override:
        return override
    service = job.get("service") or (job.get("services") or [None])[0]
    if not service:
        return ""
    filters = ["--filter", "label=com.docker.compose.service=%s" % service]
    project = ctx.source.project
    if project:
        filters += ["--filter", "label=com.docker.compose.project=%s" % project]
    r = ctx.source.run(["docker", "ps", "-q"] + filters,
                       capture=True, check=False, timeout=30)
    ids = [x.strip() for x in text(r).splitlines() if x.strip()]
    if not ids and not project:
        r = ctx.source.run(["docker", "ps", "--format",
                            "{{.ID}}\t{{.Names}}", "--filter",
                            "name=%s" % service],
                           capture=True, check=False, timeout=30)
        for line in text(r).splitlines():
            cid, _, name = line.partition("\t")
            if not cid:
                continue
            if (name == service or name.startswith(service + "-")
                    or name.endswith("-" + service)
                    or ("-%s-" % service) in name):
                ids.append(cid)
    if len(ids) > 1:
        log("  [WARN] 服务 %s 匹配到多个容器，使用 %s" % (service, ids[0]))
    return ids[0] if ids else ""


def service_container(ctx, service):
    fake = {"service": service, "services": [service], "container_override": ""}
    return resolve_container(ctx, fake)


def ensure_container(ctx, job):
    cid = job.get("_container") or resolve_container(ctx, job)
    if not cid:
        raise MigrationError("找不到运行中的容器: %s"
                             % (job.get("service") or job.get("id")))
    job["_container"] = cid
    return cid


def inspect_container(ctx, cid):
    if cid in ctx._inspect:
        return ctx._inspect[cid]
    r = ctx.source.run(["docker", "inspect", cid],
                       capture=True, check=False, timeout=30)
    if r.returncode != 0:
        raise MigrationError("docker inspect 失败: %s" % cid)
    try:
        info = json.loads(text(r))[0]
    except (ValueError, IndexError):
        raise MigrationError("docker inspect 返回格式异常: %s" % cid)
    ctx._inspect[cid] = info
    return info


def get_deployment(ctx, name):
    r = ctx.target.kubectl_run(["-n", ctx.namespace, "get", "deploy", name,
                                "-o", "json"],
                               capture=True, check=False, timeout=60)
    if r.returncode != 0:
        return None
    try:
        return json.loads(text(r))
    except ValueError:
        return None


def get_pods(ctx, service):
    r = ctx.target.kubectl_run(["-n", ctx.namespace, "get", "pods",
                                "-l", "app=%s" % service, "-o", "json"],
                               capture=True, check=False, timeout=60)
    if r.returncode != 0:
        return []
    try:
        return json.loads(text(r)).get("items", [])
    except ValueError:
        return []


def pod_ready(pod):
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return bool(statuses) and all(x.get("ready") for x in statuses)


def wait_for_pod(ctx, service, timeout=None):
    timeout = timeout or ctx.timeout
    deadline = time.time() + timeout
    while time.time() < deadline:
        pods = get_pods(ctx, service)
        ready = [p for p in pods if pod_ready(p)]
        chosen = ready or pods
        if chosen:
            name = chosen[0]["metadata"]["name"]
            if ready:
                return name
        time.sleep(3)
    raise MigrationError("等待 Pod 就绪超时: app=%s（命名空间 %s）"
                         % (service, ctx.namespace))


def wait_pods_gone(ctx, service, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not get_pods(ctx, service):
            return
        time.sleep(2)
    raise MigrationError("等待 Pod 退出超时: app=%s" % service)


def scale_deployment(ctx, name, replicas):
    ctx.target.kubectl_run(["-n", ctx.namespace, "scale", "deploy/%s" % name,
                            "--replicas=%d" % replicas],
                           timeout=120)


def wait_rollout(ctx, name):
    ctx.target.kubectl_run(["-n", ctx.namespace, "rollout", "status",
                            "deploy/%s" % name, "--timeout=%ds" % ctx.timeout],
                           timeout=ctx.timeout + 30)


def source_service_stop(ctx, service):
    cid = service_container(ctx, service)
    if not cid:
        log("  [WARN] 源端服务 %s 未运行，跳过停止" % service)
        return ""
    ctx.source.run(["docker", "stop", cid], timeout=120)
    return cid


def source_service_start(ctx, cid, service):
    if not cid:
        return
    r = ctx.source.run(["docker", "start", cid], check=False, timeout=120)
    if r.returncode != 0:
        log("  [WARN] 源端服务 %s 启动失败，请手工检查" % service)


def tar_paths_local(src, files, dest, all_files=False):
    if not os.path.isdir(src):
        raise MigrationError("bind 目录不存在: %s" % src)
    added = 0
    with tarfile.open(dest, "w:gz") as tf:
        if all_files:
            for name in sorted(os.listdir(src)):
                tf.add(os.path.join(src, name), arcname=name)
                added += 1
        else:
            for rel in files:
                path = os.path.join(src, rel)
                if os.path.exists(path):
                    tf.add(path, arcname=rel)
                    added += 1
    if added == 0:
        raise MigrationError("没有可归档的文件: %s" % src)


def tar_manifest(path):
    out = {}
    with tarfile.open(path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            name = re.sub(r"^\./", "", member.name)
            fh = tf.extractfile(member)
            h = hashlib.sha256()
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
            out[name] = {"size": member.size, "sha256": h.hexdigest()}
    return out


def source_volume_name(ctx, job):
    volume = job["volume"]
    candidates = volume.get("candidates") or [volume.get("name")]
    for name in candidates:
        r = ctx.source.run(["docker", "volume", "inspect", "--format",
                            "{{.Name}}", name],
                           capture=True, check=False, timeout=30)
        if r.returncode == 0:
            return name
    log("  [WARN] 未找到源端卷 %s，尝试 %s"
        % (", ".join(candidates), candidates[0]))
    return candidates[0]


def archive_named_volume(ctx, job, dest, files=None):
    if not job.get("volume") or job["volume"].get("type") != "volume":
        raise MigrationError("任务 %s 没有命名卷" % job["id"])
    volume = source_volume_name(ctx, job)
    image = job.get("image") or ctx.plan["options"].get("helper_image") or \
        "busybox:1.36"
    if files:
        file_list = " ".join(shlex.quote(x) for x in files)
        script = ("set --; for f in %s; do [ -e \"/data/$f\" ] && set -- \"$@\" \"$f\"; done; "
                  "[ $# -gt 0 ] && exec tar czf - -C /data \"$@\"; exit 4"
                  % file_list)
    else:
        script = "exec tar czf - -C /data ."
    argv = ["docker", "run", "--rm", "--entrypoint", "sh",
            "-v", "%s:/data:ro" % volume, image, "-c", script]
    r = ctx.source.run(argv, stdout_path=dest, check=False,
                       timeout=ctx.timeout)
    if r.returncode != 0:
        helper = ctx.plan["options"].get("helper_image") or "busybox:1.36"
        if helper != image:
            if os.path.exists(dest):
                os.remove(dest)
            log("  [WARN] 镜像 %s 归档失败，尝试 helper 镜像 %s"
                % (image, helper))
            argv = ["docker", "run", "--rm", "--entrypoint", "sh",
                    "-v", "%s:/data:ro" % volume, helper, "-c", script]
            r = ctx.source.run(argv, stdout_path=dest, check=False,
                               timeout=ctx.timeout)
    if r.returncode != 0:
        if os.path.exists(dest):
            os.remove(dest)
        raise MigrationError("卷归档失败: %s\n%s" % (volume, err_text(r)))


def dump_files_job(ctx, job):
    artifact = ctx.artifact_path(job)
    tmp = artifact + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    services = job.get("services") or []
    stopped = []
    quiesce = ctx.plan["options"].get("quiesce") or job.get("quiesce")
    if ctx.dry_run:
        log("[dry-run] 归档 %s -> %s" % (job["id"], job["artifact"]))
        if quiesce:
            for service in services:
                log("[dry-run] 停止源端服务 %s" % service)
        return
    try:
        if quiesce:
            for service in services:
                cid = source_service_stop(ctx, service)
                if cid:
                    stopped.append((service, cid))
        if job["kind"] == "bind":
            src = job["volume"]["source"]
            all_files = not job.get("files")
            if ctx.source.ssh:
                argv = ["tar", "czf", "-", "-C", src]
                argv += (["."] if all_files else list(job.get("files") or []))
                r = ctx.source.run(argv, stdout_path=tmp, check=False,
                                   timeout=ctx.timeout)
                if r.returncode != 0:
                    raise MigrationError("bind 目录归档失败: %s\n%s"
                                         % (src, err_text(r)))
            else:
                tar_paths_local(src, job.get("files") or [], tmp,
                                all_files=all_files)
        else:
            archive_named_volume(ctx, job, tmp, job.get("files"))
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            raise MigrationError("归档为空: %s" % job["id"])
        os.replace(tmp, artifact)
        manifest = tar_manifest(artifact)
        manifest_path = artifact + ".manifest.json"
        write_json(manifest_path, {"files": manifest, "at": now()})
        job["_manifest"] = manifest
        if not manifest:
            log("  [WARN] %s 归档中没有普通文件" % job["id"])
        state_job(ctx, "dump", job["id"], done=True,
                  artifact=job["artifact"], size=os.path.getsize(artifact),
                  sha256=sha256_file(artifact),
                  files=len(manifest))
        log("  完成 %s (%s, %d 个文件)"
            % (job["id"], format_size(os.path.getsize(artifact)), len(manifest)))
    finally:
        for service, cid in stopped:
            source_service_start(ctx, cid, service)
        if os.path.exists(tmp):
            os.remove(tmp)


def mysql_dump_script(job):
    db = (job.get("database") or {}).get("name") or ""
    flavor = (job.get("database") or {}).get("flavor") or "mysql"
    dump_bin = "mariadb-dump" if flavor == "mariadb" else "mysqldump"
    options = ("--single-transaction --quick --triggers "
               "--hex-blob --default-character-set=utf8mb4")
    user = (job.get("database") or {}).get("user") or "root"
    if user == "root":
        options += " --routines --events"
    if flavor == "mysql":
        options += " --set-gtid-purged=OFF --no-tablespaces"
    lines = [
        'if printenv MYSQL_ROOT_PASSWORD >/dev/null 2>&1; then '
        'U=root; P=$(printenv MYSQL_ROOT_PASSWORD); '
        'elif printenv MYSQL_PASSWORD >/dev/null 2>&1; then '
        'U=$(printenv MYSQL_USER || echo root); P=$(printenv MYSQL_PASSWORD); '
        'else U=root; P=; fi',
        '[ -n "$P" ] && export MYSQL_PWD="$P"',
        'CLI=%s; command -v %s >/dev/null 2>&1 || CLI=%s'
        % (dump_bin, dump_bin, "mysqldump" if flavor == "mariadb" else "mariadb-dump"),
    ]
    if db:
        lines.append('exec "$CLI" -h 127.0.0.1 -u "$U" %s --databases %s'
                     % (options, shlex.quote(db)))
    else:
        lines += [
            'DBS=$(%s -h 127.0.0.1 -u "$U" -N -B -e '
            '"SELECT schema_name FROM information_schema.schemata '
            "WHERE schema_name NOT IN ('information_schema',"
            "'performance_schema','mysql','sys')\")"
            % ("mysql" if flavor == "mysql" else "mariadb"),
            '[ -n "$DBS" ] || { echo "no user database" >&2; exit 4; }',
            'exec "$CLI" -h 127.0.0.1 -u "$U" %s --databases $DBS' % options,
        ]
    return "\n".join(lines)


def postgres_dump_script(job):
    db = (job.get("database") or {}).get("name") or "postgres"
    return "\n".join([
        'U=$(printenv POSTGRES_USER || echo postgres)',
        'P=$(printenv POSTGRES_PASSWORD || true)',
        '[ -n "$P" ] && export PGPASSWORD="$P"',
        'exec pg_dump -h 127.0.0.1 -U "$U" -d %s --no-owner '
        '--no-privileges --clean --if-exists --format=plain '
        '--encoding=UTF8' % shlex.quote(db),
    ])


def redis_shell_auth():
    return ("PW=$(printenv REDIS_PASSWORD || printenv REDIS_PASS || "
            "printenv REDISCLI_AUTH || true)\n"
            "if [ -z \"$PW\" ] && [ -r /proc/1/cmdline ]; then\n"
            "  PW=$(tr '\\0' '\\n' < /proc/1/cmdline | awk "
            "'p{print;exit} /^--requirepass$/{p=1} "
            "/--requirepass[ =]/{sub(/.*--requirepass[ =]/,\"\"); print; exit}' "
            "| head -n1)\n"
            "fi\n"
            "[ -n \"$PW\" ] && export REDISCLI_AUTH=\"$PW\"\n"
            "CLI=redis-cli; command -v redis-cli >/dev/null 2>&1 || "
            "CLI=valkey-cli\n")


def redis_dump_script():
    return (redis_shell_auth() +
            "rm -f /tmp/datamigrate.rdb\n"
            "exec \"$CLI\" --rdb /tmp/datamigrate.rdb\n")


def mongo_dump_script():
    return "\n".join([
        'U=$(printenv MONGO_INITDB_ROOT_USERNAME || true)',
        'P=$(printenv MONGO_INITDB_ROOT_PASSWORD || true)',
        'if [ -n "$U" ]; then',
        '  exec mongodump --host 127.0.0.1 --port 27017 --archive --gzip '
        '-u "$U" -p "$P" --authenticationDatabase admin',
        'else',
        '  exec mongodump --host 127.0.0.1 --port 27017 --archive --gzip',
        'fi',
    ])


def dump_logical_job(ctx, job):
    artifact = ctx.artifact_path(job)
    tmp = artifact + ".part"
    service = job.get("service") or job["id"]
    if ctx.dry_run:
        log("[dry-run] 备份 %s（服务 %s） -> %s"
            % (job["kind"], service, job["artifact"]))
        return
    cid = ensure_container(ctx, job)
    if os.path.exists(tmp):
        os.remove(tmp)
    if job["kind"] == "mysql":
        script = mysql_dump_script(job)
    elif job["kind"] == "postgres":
        script = postgres_dump_script(job)
    elif job["kind"] == "redis":
        script = redis_dump_script()
    else:
        script = mongo_dump_script()
    r = ctx.source.run(["docker", "exec", "-i", cid, "sh", "-c", script],
                       stdout_path=tmp if job["kind"] != "redis" else None,
                       capture=job["kind"] == "redis",
                       check=False, timeout=ctx.timeout)
    if r.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise MigrationError("%s 备份失败: %s\n%s"
                             % (job["kind"], job["id"], err_text(r)))
    if job["kind"] == "redis":
        r = ctx.source.run(["docker", "exec", cid, "cat",
                            "/tmp/datamigrate.rdb"],
                           stdout_path=tmp, check=False, timeout=ctx.timeout)
        ctx.source.run(["docker", "exec", cid, "rm", "-f",
                        "/tmp/datamigrate.rdb"], check=False, timeout=30)
        if r.returncode != 0 or not os.path.exists(tmp) or \
                os.path.getsize(tmp) == 0:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise MigrationError("Redis RDB 读取失败: %s" % job["id"])
    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        raise MigrationError("备份产物为空: %s" % job["id"])
    os.replace(tmp, artifact)
    state_job(ctx, "dump", job["id"], done=True, artifact=job["artifact"],
              size=os.path.getsize(artifact), sha256=sha256_file(artifact))
    log("  完成 %s (%s)" % (job["id"], format_size(os.path.getsize(artifact))))
    if ctx.plan["options"].get("verify_rows"):
        try:
            counts = collect_counts(ctx, job, target=False)
            write_json(os.path.join(ctx.outdir, job["counts"]),
                       {"kind": job["kind"], "at": now(), "data": counts})
            state_job(ctx, "dump", job["id"], counts=job["counts"])
        except MigrationError as exc:
            log("  [WARN] 源端计数失败（不影响备份）: %s" % exc)


def dump_job(ctx, job):
    artifact = ctx.artifact_path(job)
    if ctx.args.resume and state_job_done(ctx, "dump", job["id"]) and \
            os.path.exists(artifact):
        log("  跳过已完成任务 %s（--resume）" % job["id"])
        return
    if job["kind"] in DB_KINDS:
        dump_logical_job(ctx, job)
    else:
        dump_files_job(ctx, job)


def artifact_files(ctx):
    needed = set()
    for job in ctx.jobs:
        needed.add(os.path.basename(job["artifact"]))
        needed.add(os.path.basename(job["artifact"]) + ".manifest.json")
        if job.get("counts"):
            needed.add(os.path.basename(job["counts"]))
    manifest_path = os.path.join(ctx.artifacts, "manifest.json")
    if os.path.isfile(manifest_path):
        manifest = load_json(manifest_path, {}) or {}
        return [x["path"] for x in manifest.get("files", [])
                if os.path.basename(x["path"]) in needed]
    if not os.path.isdir(ctx.artifacts):
        return []
    return sorted("artifacts/" + name for name in os.listdir(ctx.artifacts)
                  if name in needed and
                  os.path.isfile(os.path.join(ctx.artifacts, name)) and
                  not name.endswith(".part"))


def write_artifact_manifest(ctx):
    names = set()
    for job in ctx.jobs:
        names.add(os.path.basename(job["artifact"]))
        if job.get("counts"):
            names.add(os.path.basename(job["counts"]))
        names.add(os.path.basename(job["artifact"]) + ".manifest.json")
    files = []
    for name in sorted(names):
        path = os.path.join(ctx.artifacts, name)
        if not os.path.isfile(path):
            continue
        files.append({"path": "artifacts/" + name,
                      "size": os.path.getsize(path),
                      "sha256": sha256_file(path)})
    write_json(os.path.join(ctx.artifacts, "manifest.json"),
               {"at": now(), "files": files})


def phase_preflight(ctx):
    if ctx.dry_run:
        log("[dry-run] 检查源端 Docker、目标端 kubectl、Deployment 与 PVC")
        for job in ctx.jobs:
            log("[dry-run] 任务 %s: %s" % (job["id"], job["kind"]))
        return 0
    if not ctx.jobs:
        log("没有迁移任务，预检通过")
        return 0
    errors = []
    if ctx.source.ssh:
        r = ctx.source.run(["true"], capture=True, check=False, timeout=30)
        if r.returncode != 0:
            errors.append("源端 SSH 不可达: %s" % ctx.source.ssh)
    if any(j["kind"] != "bind" for j in ctx.jobs):
        r = ctx.source.run(["docker", "version", "--format",
                            "{{.Server.Version}}"],
                           capture=True, check=False, timeout=30)
        if r.returncode != 0:
            errors.append("源端 Docker 不可用: %s" % err_text(r).strip())
        else:
            log("  源端 Docker: %s" % text(r).strip())
    if ctx.target.ssh:
        r = ctx.target.host_run(["true"], capture=True, check=False, timeout=30)
        if r.returncode != 0:
            errors.append("目标端 SSH 不可达: %s" % ctx.target.ssh)
    r = ctx.target.kubectl_run(["version", "--client", "--output", "json"],
                               capture=True, check=False, timeout=30)
    if r.returncode != 0:
        errors.append("目标端 kubectl 不可用: %s" % err_text(r).strip())
    r = ctx.target.kubectl_run(["-n", ctx.namespace, "get", "namespace",
                                ctx.namespace],
                               capture=True, check=False, timeout=30)
    if r.returncode != 0:
        errors.append("命名空间不存在: %s" % ctx.namespace)

    for job in ctx.jobs:
        if job["kind"] == "bind":
            continue
        service = job.get("service") or (job.get("services") or [None])[0]
        cid = resolve_container(ctx, job)
        if cid:
            job["_container"] = cid
            log("  容器 %s -> %s" % (service or job["id"], cid[:12]))
            try:
                inspect_container(ctx, cid)
            except MigrationError as exc:
                errors.append(str(exc))
        elif job["kind"] in ("mysql", "postgres", "mongo", "redis"):
            errors.append("源端容器未运行: %s" % (service or job["id"]))
        else:
            log("  [WARN] 源端容器未运行，直接归档卷: %s"
                % (service or job["id"]))
        volume = job.get("volume") or {}
        if volume.get("type") == "volume":
            candidates = volume.get("candidates") or [volume["name"]]
            found = ""
            for candidate in candidates:
                r = ctx.source.run(["docker", "volume", "inspect", candidate],
                                   capture=True, check=False, timeout=30)
                if r.returncode == 0:
                    found = candidate
                    break
            if not found:
                errors.append("源端数据卷不存在: %s" % ", ".join(candidates))
        dep_name = job.get("service") or service
        if dep_name and get_deployment(ctx, dep_name) is None:
            errors.append("目标 Deployment/%s 不存在，请先部署应用" % dep_name)
        if volume.get("type") == "volume" and not ctx.plan["options"].get("ephemeral"):
            r = ctx.target.kubectl_run(["-n", ctx.namespace, "get", "pvc",
                                        volume["claim"]],
                                       capture=True, check=False, timeout=30)
            if r.returncode != 0:
                errors.append("目标 PVC/%s 不存在" % volume["claim"])
    if errors:
        log("")
        for error in errors:
            log("  [FAIL] %s" % error)
        return 1
    log("")
    log("预检通过：%d 个迁移任务" % len(ctx.jobs))
    return 0


def phase_dump(ctx):
    if not ctx.dry_run:
        os.makedirs(ctx.artifacts, exist_ok=True)
    if not ctx.jobs:
        log("没有需要备份的任务")
        return 0
    errors = []
    for job in ctx.jobs:
        try:
            dump_job(ctx, job)
        except MigrationError as exc:
            errors.append("%s: %s" % (job["id"], exc))
            state_job(ctx, "dump", job["id"], done=False, error=str(exc))
    if not ctx.dry_run:
        write_artifact_manifest(ctx)
    log("")
    if errors:
        for error in errors:
            log("  [FAIL] %s" % error)
        return 1
    log("备份完成，产物目录: %s" % ctx.artifacts)
    return 0


def remote_artifact_path(ctx, filename):
    return posixpath.join(ctx.plan["target"]["remote_dir"], "artifacts", filename)


def target_artifact_path(ctx, job):
    filename = os.path.basename(job["artifact"])
    if ctx.target.ssh:
        return remote_artifact_path(ctx, filename)
    return ctx.artifact_path(job)


def phase_transfer(ctx):
    if not ctx.jobs:
        log("没有需要传输的产物")
        return 0
    if ctx.dry_run:
        for job in ctx.jobs:
            log("[dry-run] 上传 %s -> %s"
                % (job["artifact"], target_artifact_path(ctx, job)))
        return 0
    files = artifact_files(ctx)
    if ctx.target.ssh:
        remote_dir = ctx.plan["target"]["remote_dir"]
        ctx.target.host_run(["mkdir", "-p", posixpath.join(remote_dir, "artifacts")],
                            timeout=60)
        for rel in files:
            local = os.path.join(ctx.outdir, rel)
            if not os.path.isfile(local):
                raise MigrationError("本地产物不存在: %s" % local)
            remote = remote_artifact_path(ctx, os.path.basename(rel))
            dest = "%s:%s" % (ctx.target.ssh, shlex.quote(remote))
            r = run(scp_argv(ctx.target.key) + [local, dest],
                    check=False, timeout=3600)
            if r.returncode != 0:
                raise MigrationError("上传失败: %s\n%s" % (rel, err_text(r)))
            local_hash = sha256_file(local)
            r = ctx.target.host_run(["sha256sum", remote],
                                    capture=True, check=False, timeout=120)
            remote_hash = text(r).split()[0] if text(r).strip() else ""
            if remote_hash != local_hash:
                raise MigrationError("上传校验失败: %s" % rel)
            state_job(ctx, "transfer", os.path.basename(rel), done=True,
                      sha256=local_hash)
            log("  已上传 %s" % rel)
    else:
        for rel in files:
            local = os.path.join(ctx.outdir, rel)
            if not os.path.isfile(local):
                raise MigrationError("本地产物不存在: %s" % local)
            state_job(ctx, "transfer", os.path.basename(rel), done=True,
                      sha256=sha256_file(local))
            log("  已就绪 %s" % rel)
    log("")
    log("传输完成")
    return 0


def keep_services(ctx):
    return {j["service"] for j in ctx.jobs
            if j["kind"] in ("mysql", "postgres", "mongo") and j.get("service")}


def stop_target_services(ctx):
    options = ctx.plan["options"]
    if not options.get("stop_apps"):
        raw = [j for j in ctx.jobs
               if not j.get("skip_restore") and
               j["kind"] in ("sqlite", "volume", "redis")]
        if raw:
            raise MigrationError("--no-stop-apps 不能用于 SQLite/文件卷/Redis 恢复")
        return
    keep = keep_services(ctx)
    stop = [s for s in ctx.plan.get("services", []) if s not in keep]
    stopped = dict(ctx.state.get("stopped") or {})
    for service in stop:
        dep = get_deployment(ctx, service)
        if dep is None:
            log("  [WARN] Deployment/%s 不存在，跳过" % service)
            continue
        replicas = int((dep.get("spec") or {}).get("replicas") or 0)
        if replicas == 0:
            continue
        stopped[service] = replicas
        scale_deployment(ctx, service, 0)
        log("  停止 Deployment/%s（原副本 %d）" % (service, replicas))
    ctx.state["stopped"] = stopped
    save_state(ctx)
    for service in stopped:
        wait_pods_gone(ctx, service)


def mysql_query_script(sql):
    return ("if printenv MYSQL_ROOT_PASSWORD >/dev/null 2>&1; then "
            "U=root; P=$(printenv MYSQL_ROOT_PASSWORD); "
            "elif printenv MYSQL_PASSWORD >/dev/null 2>&1; then "
            "U=$(printenv MYSQL_USER || echo root); P=$(printenv MYSQL_PASSWORD); "
            "else U=root; P=; fi\n"
            "[ -n \"$P\" ] && export MYSQL_PWD=\"$P\"\n"
            "CLI=mysql; command -v mysql >/dev/null 2>&1 || CLI=mariadb\n"
            "exec \"$CLI\" -h 127.0.0.1 -u \"$U\" -N -B -e %s"
            % shlex.quote(sql))


def postgres_query_script(sql, db):
    if db:
        db_line = "DB=%s" % shlex.quote(db)
    else:
        db_line = ("DB=$(printenv POSTGRES_DB || printenv POSTGRES_USER "
                   "|| echo postgres)")
    return ("U=$(printenv POSTGRES_USER || echo postgres)\n"
            "P=$(printenv POSTGRES_PASSWORD || true)\n"
            "[ -n \"$P\" ] && export PGPASSWORD=\"$P\"\n"
            "%s\n"
            "exec psql -h 127.0.0.1 -U \"$U\" -d \"$DB\" -t -A "
            "-F '|' -c %s"
            % (db_line, shlex.quote(sql)))


def mongo_eval_script(js):
    return "\n".join([
        'U=$(printenv MONGO_INITDB_ROOT_USERNAME || true)',
        'P=$(printenv MONGO_INITDB_ROOT_PASSWORD || true)',
        'CLI=mongosh; command -v mongosh >/dev/null 2>&1 || CLI=mongo',
        'if [ -n "$U" ]; then',
        '  exec "$CLI" --quiet -u "$U" -p "$P" '
        '--authenticationDatabase admin --eval %s' % shlex.quote(js),
        'else',
        '  exec "$CLI" --quiet --eval %s' % shlex.quote(js),
        'fi',
    ])


def mongo_stats_script():
    js = ("JSON.stringify(db.getMongo().getDBNames()"
          ".filter(n=>!['admin','local','config'].includes(n))"
          ".map(n=>({name:n,objects:db.getSiblingDB(n).stats().objects})))")
    return mongo_eval_script(js)


def redis_keyspace_script():
    return redis_shell_auth() + 'exec "$CLI" INFO keyspace'


def container_exec(ctx, cid, script, target=False, pod=""):
    if target:
        argv = ["-n", ctx.namespace, "exec", pod, "--", "sh", "-c", script]
        return ctx.target.kubectl_run(argv, capture=True, check=False,
                                      timeout=ctx.timeout)
    return ctx.source.run(["docker", "exec", cid, "sh", "-c", script],
                          capture=True, check=False, timeout=ctx.timeout)


def parse_mysql_counts(raw):
    data = {}
    for line in raw.splitlines():
        if "\t" not in line:
            continue
        name, count = line.split("\t", 1)
        try:
            data[name] = int(count.strip())
        except ValueError:
            continue
    return data


def parse_pg_counts(raw):
    data = {}
    for line in raw.splitlines():
        if "|" not in line:
            continue
        name, count = line.split("|", 1)
        try:
            data[name] = int(count.strip())
        except ValueError:
            continue
    return data


def parse_redis_keyspace(raw):
    data = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("db") or ":" not in line:
            continue
        db, rest = line.split(":", 1)
        fields = {}
        for part in rest.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                try:
                    fields[k] = int(v)
                except ValueError:
                    fields[k] = v
        data[db] = fields
    return data


def collect_counts(ctx, job, target=False):
    cid = ""
    pod = ""
    if target:
        pod = wait_for_pod(ctx, job["service"])
    else:
        cid = ensure_container(ctx, job)
    def query(script):
        r = container_exec(ctx, cid, script, target=target, pod=pod)
        if r.returncode != 0:
            raise MigrationError("%s 计数查询失败: %s"
                                 % (job["kind"], err_text(r)[:500]))
        return r

    if job["kind"] == "mysql":
        db = (job.get("database") or {}).get("name") or ""
        if not db and not target:
            r = query(mysql_query_script(
                "SELECT table_schema, table_name "
                "FROM information_schema.tables "
                "WHERE table_type='BASE TABLE' "
                "AND table_schema NOT IN "
                "('mysql','information_schema',"
                "'performance_schema','sys') "
                "ORDER BY 1,2"))
            tables = [tuple(x.split("\t", 1)) for x in text(r).splitlines()
                      if "\t" in x]
        elif db:
            r = query(mysql_query_script(
                "SELECT table_schema, table_name "
                "FROM information_schema.tables "
                "WHERE table_type='BASE TABLE' "
                "AND table_schema=%s "
                "ORDER BY 1,2" % sql_lit(db)))
            tables = [tuple(x.split("\t", 1)) for x in text(r).splitlines()
                      if "\t" in x]
        else:
            tables = []
        if not tables:
            return {}
        parts = []
        for db_name, table in tables:
            parts.append("SELECT %s, COUNT(*) FROM %s.%s"
                         % (sql_lit("%s.%s" % (db_name, table)),
                            ident_mysql(db_name), ident_mysql(table)))
        r = query(mysql_query_script(" UNION ALL ".join(parts)))
        return {"tables": parse_mysql_counts(text(r))}
    if job["kind"] == "postgres":
        r = query(postgres_query_script(
            "SELECT schemaname, relname "
            "FROM pg_stat_user_tables ORDER BY 1,2",
            (job.get("database") or {}).get("name")))
        tables = [tuple(x.split("|", 1)) for x in text(r).splitlines()
                  if "|" in x]
        if not tables:
            return {}
        parts = []
        for schema, table in tables:
            parts.append("SELECT %s, COUNT(*) FROM %s.%s"
                         % (sql_lit("%s.%s" % (schema, table)),
                            ident_pg(schema), ident_pg(table)))
        r = query(postgres_query_script(
            " UNION ALL ".join(parts),
            (job.get("database") or {}).get("name")))
        return {"tables": parse_pg_counts(text(r))}
    if job["kind"] == "mongo":
        r = query(mongo_stats_script())
        try:
            databases = json.loads(text(r).strip())
        except ValueError:
            return {}
        return {"databases": {x.get("name"): x.get("objects")
                              for x in databases}}
    if job["kind"] == "redis":
        r = query(redis_keyspace_script())
        return {"keyspace": parse_redis_keyspace(text(r))}
    return {}


def ping_db(ctx, job, target=False):
    if job["kind"] == "mysql":
        script = mysql_query_script("SELECT 1")
    elif job["kind"] == "postgres":
        script = postgres_query_script("SELECT 1",
                                       (job.get("database") or {}).get("name"))
    elif job["kind"] == "mongo":
        script = mongo_eval_script("db.runCommand({ping:1}).ok")
    else:
        script = redis_keyspace_script().replace("INFO keyspace", "PING")
    cid = "" if target else ensure_container(ctx, job)
    pod = wait_for_pod(ctx, job["service"]) if target else ""
    r = container_exec(ctx, cid, script, target=target, pod=pod)
    if r.returncode != 0:
        return False
    body = text(r).strip().lower()
    return "1" in body if job["kind"] != "redis" else "pong" in body


def mysql_restore_script():
    return ("if printenv MYSQL_ROOT_PASSWORD >/dev/null 2>&1; then "
            "U=root; P=$(printenv MYSQL_ROOT_PASSWORD); "
            "elif printenv MYSQL_PASSWORD >/dev/null 2>&1; then "
            "U=$(printenv MYSQL_USER || echo root); P=$(printenv MYSQL_PASSWORD); "
            "else U=root; P=; fi\n"
            "[ -n \"$P\" ] && export MYSQL_PWD=\"$P\"\n"
            "CLI=mysql; command -v mysql >/dev/null 2>&1 || CLI=mariadb\n"
            "exec \"$CLI\" -h 127.0.0.1 -u \"$U\" --default-character-set=utf8mb4")


def postgres_restore_script(job):
    db = (job.get("database") or {}).get("name")
    db_line = ("DB=%s" % shlex.quote(db)) if db else \
        "DB=$(printenv POSTGRES_DB || printenv POSTGRES_USER || echo postgres)"
    return ("U=$(printenv POSTGRES_USER || echo postgres)\n"
            "P=$(printenv POSTGRES_PASSWORD || true)\n"
            "[ -n \"$P\" ] && export PGPASSWORD=\"$P\"\n"
            "%s\n"
            "exec psql -h 127.0.0.1 -U \"$U\" -d \"$DB\" -v ON_ERROR_STOP=1"
            % db_line)


def mongo_restore_script():
    return ("U=$(printenv MONGO_INITDB_ROOT_USERNAME || true)\n"
            "P=$(printenv MONGO_INITDB_ROOT_PASSWORD || true)\n"
            "if [ -n \"$U\" ]; then\n"
            "  exec mongorestore --host 127.0.0.1 --archive --gzip --drop "
            "-u \"$U\" -p \"$P\" --authenticationDatabase admin\n"
            "else\n"
            "  exec mongorestore --host 127.0.0.1 --archive --gzip --drop\n"
            "fi")


def kubectl_stdin(ctx, argv, artifact, timeout=None):
    timeout = timeout or ctx.timeout
    if ctx.target.ssh:
        cmd = [ctx.target.kubectl] + [str(x) for x in argv]
        remote = (" ".join(shlex.quote(x) for x in cmd) +
                  " < " + shlex.quote(artifact))
        return ctx.target.host_run(["sh", "-c", remote], capture=True,
                                   check=False, timeout=timeout)
    return ctx.target.kubectl_run(argv, stdin_path=artifact, capture=True,
                                  check=False, timeout=timeout)


def restore_logical_job(ctx, job):
    pod = wait_for_pod(ctx, job["service"])
    artifact = target_artifact_path(ctx, job)
    if job["kind"] == "mysql":
        script = mysql_restore_script()
    elif job["kind"] == "postgres":
        script = postgres_restore_script(job)
    else:
        script = mongo_restore_script()
    if ctx.dry_run:
        log("[dry-run] kubectl exec -i %s -- sh -c '<restore %s>' < %s"
            % (pod, job["kind"], artifact))
        return
    r = kubectl_stdin(ctx, ["-n", ctx.namespace, "exec", "-i", pod,
                            "--", "sh", "-c", script], artifact)
    if r.returncode != 0:
        raise MigrationError("%s 恢复失败: %s\n%s"
                             % (job["kind"], job["id"], err_text(r)))
    state_job(ctx, "restore", job["id"], done=True, mode="logical")
    log("  已恢复 %s" % job["id"])


def helper_pod_name(job):
    return ("datamigrate-" + sanitize_name(job["id"]))[:55].rstrip("-")


def helper_pod_manifest(ctx, job, name):
    image = ctx.plan["options"].get("helper_image") or job.get("image") or \
        "busybox:1.36"
    claim = job["volume"]["claim"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": ctx.namespace,
                     "labels": {"app": "datamigrate"}},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "helper",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "sleep 3600"],
                "volumeMounts": [{"name": "data", "mountPath": "/data"}],
            }],
            "volumes": [{
                "name": "data",
                "persistentVolumeClaim": {"claimName": claim},
            }],
        },
    }


def helper_up(ctx, job):
    name = helper_pod_name(job)
    ctx.target.kubectl_run(["-n", ctx.namespace, "delete", "pod", name,
                            "--ignore-not-found", "--wait=true"],
                           check=False, timeout=120)
    manifest = helper_pod_manifest(ctx, job, name)
    service = job.get("service") or (job.get("services") or [None])[0]
    dep = get_deployment(ctx, service) if service else None
    pull_secrets = ((((dep or {}).get("spec") or {}).get("template") or {})
                    .get("spec") or {}).get("imagePullSecrets")
    if pull_secrets:
        manifest["spec"]["imagePullSecrets"] = pull_secrets
    ctx.target.kubectl_run(["-n", ctx.namespace, "apply", "-f", "-"],
                           input_text=yaml.safe_dump(manifest, sort_keys=False),
                           check=True, timeout=120)
    r = ctx.target.kubectl_run(["-n", ctx.namespace, "wait",
                                "--for=condition=Ready", "pod/%s" % name,
                                "--timeout=180s"],
                               check=False, timeout=200)
    if r.returncode != 0:
        raise MigrationError("helper Pod 未就绪（镜像可能缺少 sh/tar，可加 "
                             "--helper-image busybox:1.36）: %s\n%s"
                             % (name, err_text(r)))
    log("  helper Pod 就绪: %s" % name)
    return name


def helper_down(ctx, name):
    if ctx.args.keep_helper:
        log("  [KEEP] helper Pod 保留: %s" % name)
        return
    ctx.target.kubectl_run(["-n", ctx.namespace, "delete", "pod", name,
                            "--ignore-not-found", "--wait=true"],
                           check=False, timeout=120)


def kubectl_cp(ctx, local_or_remote, pod, dest):
    r = ctx.target.kubectl_run(["-n", ctx.namespace, "cp", local_or_remote,
                                "%s:%s" % (pod, dest)],
                               capture=True, check=False, timeout=ctx.timeout)
    if r.returncode != 0:
        raise MigrationError("kubectl cp 失败（容器缺少 tar 时可用 "
                             "--helper-image busybox:1.36）: %s\n%s"
                             % (local_or_remote, err_text(r)))


def verify_manifest_in_helper(ctx, pod, expected):
    r = ctx.target.kubectl_run(["-n", ctx.namespace, "exec", pod, "--",
                                "sh", "-c",
                                "cd /data && find . -type f -exec sha256sum {} +"],
                               capture=True, check=False, timeout=ctx.timeout)
    actual = {}
    for line in text(r).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = re.sub(r"^\./", "", parts[1])
            actual[name] = parts[0]
    missing = []
    mismatch = []
    for name, meta in expected.items():
        if name not in actual:
            missing.append(name)
        elif actual[name] != meta.get("sha256"):
            mismatch.append(name)
    return {"ok": not missing and not mismatch,
            "missing": missing[:20], "mismatch": mismatch[:20],
            "expected": len(expected), "actual": len(actual)}


def restore_raw_job(ctx, job):
    if job.get("skip_restore"):
        log("  跳过 %s（策略标记不恢复）" % job["id"])
        return
    if ctx.dry_run:
        log("[dry-run] 创建 helper Pod，解包 %s 到 PVC/%s"
            % (job["artifact"], job["volume"]["claim"]))
        return
    pod = helper_up(ctx, job)
    try:
        if job["kind"] == "redis":
            remote = target_artifact_path(ctx, job)
            kubectl_cp(ctx, remote, pod, "/tmp/dump.rdb")
            ctx.target.kubectl_run(["-n", ctx.namespace, "exec", pod, "--",
                                    "sh", "-c",
                                    "cp /tmp/dump.rdb /data/dump.rdb"],
                                   check=True, timeout=120)
            local_hash = sha256_file(ctx.artifact_path(job))
            r = ctx.target.kubectl_run(["-n", ctx.namespace, "exec", pod,
                                        "--", "sha256sum", "/data/dump.rdb"],
                                       capture=True, check=False, timeout=60)
            actual = text(r).split()[0] if text(r).strip() else ""
            result = {"ok": actual == local_hash, "expected": 1,
                      "actual": 1, "missing": [], "mismatch": []}
        else:
            remote = target_artifact_path(ctx, job)
            kubectl_cp(ctx, remote, pod, "/tmp/data.tar.gz")
            if ctx.plan["options"].get("clean_volume"):
                ctx.target.kubectl_run(["-n", ctx.namespace, "exec", pod, "--",
                                        "sh", "-c",
                                        "rm -rf /data/* /data/.[!.]* "
                                        "/data/..?* 2>/dev/null || true"],
                                       check=False, timeout=120)
            r = ctx.target.kubectl_run(["-n", ctx.namespace, "exec", pod, "--",
                                        "sh", "-c",
                                        "tar xzf /tmp/data.tar.gz -C /data"],
                                       check=False, timeout=ctx.timeout)
            if r.returncode != 0:
                raise MigrationError("解包失败: %s\n%s" % (job["id"], err_text(r)))
            manifest_path = ctx.artifact_path(job) + ".manifest.json"
            expected = (load_json(manifest_path, {}) or {}).get("files", {})
            result = verify_manifest_in_helper(ctx, pod, expected)
        ctx.state.setdefault("verify", {})[job["id"]] = result
        state_job(ctx, "restore", job["id"], done=True,
                  mode="raw", verify=result)
        log("  已恢复 %s（校验: %s）"
            % (job["id"], "OK" if result.get("ok") else "FAIL"))
    finally:
        helper_down(ctx, pod)


def phase_restore(ctx):
    jobs = [j for j in ctx.jobs if not j.get("skip_restore")]
    if not jobs:
        log("没有需要恢复的任务")
        return 0
    if not ctx.dry_run and not ctx.args.yes:
        raise MigrationError("restore 会修改目标数据，请确认后加 --yes")
    if ctx.dry_run:
        for job in jobs:
            log("[dry-run] 恢复 %s（%s）" % (job["id"], job["kind"]))
        stop_target_services_dry(ctx)
        return 0
    stop_target_services(ctx)
    errors = []
    for job in jobs:
        try:
            if job["kind"] in ("mysql", "postgres", "mongo"):
                restore_logical_job(ctx, job)
            else:
                restore_raw_job(ctx, job)
        except MigrationError as exc:
            errors.append("%s: %s" % (job["id"], exc))
            state_job(ctx, "restore", job["id"], done=False, error=str(exc))
    if errors:
        for error in errors:
            log("  [FAIL] %s" % error)
        log("")
        log("应用保持停止。修复后执行 verify，再执行 --phase start。")
        return 1
    log("")
    log("恢复完成；应用保持停止。下一步: --phase verify，然后 --phase start")
    return 0


def stop_target_services_dry(ctx):
    if not ctx.plan["options"].get("stop_apps"):
        raw = [j for j in ctx.jobs
               if not j.get("skip_restore") and
               j["kind"] in ("sqlite", "volume", "redis")]
        if raw:
            raise MigrationError("--no-stop-apps 不能用于 SQLite/文件卷/Redis 恢复")
        log("[dry-run] 保留应用运行（--no-stop-apps）")
        return
    keep = keep_services(ctx)
    for service in ctx.plan.get("services", []):
        if service not in keep:
            log("[dry-run] scale deploy/%s --replicas=0" % service)


def compare_counts(source, target):
    if source == target:
        return True
    if isinstance(source, dict) and isinstance(target, dict):
        return source == target
    return False


def verify_logical_job(ctx, job):
    result = {"ok": False, "detail": ""}
    counts_path = os.path.join(ctx.outdir, job["counts"])
    source = (load_json(counts_path, {}) or {}).get("data")
    try:
        if not ctx.plan["options"].get("verify_rows") or source is None:
            ok = ping_db(ctx, job, target=True)
            result["ok"] = ok
            result["detail"] = "连接成功（未比对行数）" if ok else "连接失败"
            return result
        target = collect_counts(ctx, job, target=True)
    except MigrationError as exc:
        result["detail"] = str(exc)
        return result
    ok = compare_counts(source, target)
    result["ok"] = ok
    result["detail"] = "计数一致" if ok else "计数不一致"
    if not ok:
        result["source"] = source
        result["target"] = target
    return result


def verify_redis_job(ctx, job):
    stored = ctx.state.get("verify", {}).get(job["id"])
    if stored:
        return stored
    stopped = ctx.state.get("stopped", {})
    service = job["service"]
    replicas = int(stopped.get(service) or 1)
    if not stopped.get(service):
        try:
            pod = wait_for_pod(ctx, service, timeout=60)
        except MigrationError as exc:
            return {"ok": False, "detail": str(exc)}
    else:
        scale_deployment(ctx, service, replicas)
        try:
            pod = wait_for_pod(ctx, service)
            ctx.state["stopped"].pop(service, None)
            save_state(ctx)
        except MigrationError as exc:
            return {"ok": False, "detail": str(exc)}
    try:
        target = collect_counts(ctx, job, target=True)
    except MigrationError as exc:
        return {"ok": False, "detail": str(exc)}
    source = (load_json(os.path.join(ctx.outdir, job["counts"]), {}) or {}).get("data")
    if source is None:
        return {"ok": True, "detail": "连接成功（未比对 keyspace）"}
    source_keys = {db: (info or {}).get("keys")
                   for db, info in (source.get("keyspace") or {}).items()}
    target_keys = {db: (info or {}).get("keys")
                   for db, info in (target.get("keyspace") or {}).items()}
    ok = source_keys == target_keys
    return {"ok": ok, "detail": "key 数一致" if ok else "key 数不一致",
            "source": source_keys, "target": target_keys}


def phase_verify(ctx):
    if ctx.dry_run:
        for job in ctx.jobs:
            if not job.get("skip_restore"):
                log("[dry-run] 校验 %s（%s）" % (job["id"], job["kind"]))
        return 0
    results = []
    for job in ctx.jobs:
        if job.get("skip_restore"):
            continue
        if job["kind"] in ("mysql", "postgres", "mongo"):
            result = verify_logical_job(ctx, job)
        elif job["kind"] == "redis":
            result = verify_redis_job(ctx, job)
        elif job["kind"] in ("sqlite", "volume"):
            result = ctx.state.get("verify", {}).get(job["id"]) or {
                "ok": False, "detail": "缺少恢复校验结果"}
        else:
            continue
        ctx.state.setdefault("verify", {})[job["id"]] = result
        results.append((job, result))
    if not results:
        log("没有需要校验的任务")
        return 0
    log("")
    log("%-20s %-8s %s" % ("任务", "结果", "说明"))
    log("-" * 70)
    ok = True
    for job, result in results:
        good = bool(result.get("ok"))
        ok = ok and good
        log("%-20s %-8s %s" % (job["id"], "PASS" if good else "FAIL",
                               result.get("detail", "")))
    save_state(ctx)
    return 0 if ok else 1


def phase_start(ctx):
    stopped = ctx.state.get("stopped", {})
    if not stopped:
        log("没有需要启动的服务")
        return 0
    if ctx.dry_run:
        for service, replicas in stopped.items():
            log("[dry-run] scale deploy/%s --replicas=%d" % (service, replicas))
        return 0
    for service, replicas in list(stopped.items()):
        dep = get_deployment(ctx, service)
        if dep is None:
            log("  [WARN] Deployment/%s 已不存在" % service)
            stopped.pop(service, None)
            continue
        scale_deployment(ctx, service, int(replicas))
        log("  启动 Deployment/%s（副本 %d）" % (service, replicas))
    for service in list(stopped.keys()):
        try:
            wait_rollout(ctx, service)
        except MigrationError as exc:
            log("  [WARN] 等待 Deployment/%s 滚动完成失败: %s" % (service, exc))
    ctx.state["stopped"] = {}
    save_state(ctx)
    log("")
    log("服务已恢复")
    return 0


def run_phase(ctx, phase):
    funcs = {
        "preflight": phase_preflight,
        "dump": phase_dump,
        "transfer": phase_transfer,
        "restore": phase_restore,
        "verify": phase_verify,
        "start": phase_start,
    }
    return funcs[phase](ctx)


def cmd_run(args):
    plan = load_json(args.plan)
    if not plan:
        raise MigrationError("无法读取计划文件: %s" % args.plan)
    apply_run_overrides(plan, args)
    ctx = Ctx(plan, args)
    phases = PHASES if args.phase == "all" else [args.phase]
    for phase in phases:
        log("")
        log("== 阶段: %s" % phase)
        rc = run_phase(ctx, phase)
        if rc != 0:
            log("")
            log("[FAIL] 阶段 %s 未通过" % phase)
            return rc
    return 0


def add_plan_args(parser):
    parser.add_argument("--compose", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--namespace", default="")
    parser.add_argument("--source-ssh", default="")
    parser.add_argument("--source-key", default="")
    parser.add_argument("--source-project", default="",
                        help="Compose 项目名（默认取 compose 目录名）")
    parser.add_argument("--source-dir", default="",
                        help="源端项目目录（SSH 源端 bind 路径解析）")
    parser.add_argument("--target-ssh", default="")
    parser.add_argument("--target-key", default="")
    parser.add_argument("--target-dir", default="")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--ephemeral", action="store_true")
    parser.add_argument("--include", default="")
    parser.add_argument("--exclude", default="")
    parser.add_argument("--database-map", default="")
    parser.add_argument("--volume-map", default="",
                        help="源端卷名映射，如 mysql-data=myproj_mysql-data")
    parser.add_argument("--container-map", default="")
    parser.add_argument("--include-bind-dirs", action="store_true")
    parser.add_argument("--verify-rows", dest="verify_rows",
                        action="store_true", default=True)
    parser.add_argument("--no-verify-rows", dest="verify_rows",
                        action="store_false")
    parser.add_argument("--quiesce", action="store_true", default=False)
    parser.add_argument("--helper-image", default="")


def add_run_args(parser):
    parser.add_argument("--plan", required=True)
    parser.add_argument("--phase", choices=PHASES + ["all"], default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--source-ssh", default="")
    parser.add_argument("--source-key", default="")
    parser.add_argument("--source-project", default="")
    parser.add_argument("--source-dir", default="")
    parser.add_argument("--target-ssh", default="")
    parser.add_argument("--target-key", default="")
    parser.add_argument("--target-dir", default="")
    parser.add_argument("--namespace", default="")
    parser.add_argument("--kubectl", default="")
    parser.add_argument("--helper-image", default="")
    parser.add_argument("--volume-map", default="")
    parser.add_argument("--verify-rows", dest="verify_rows",
                        action="store_true", default=None)
    parser.add_argument("--no-verify-rows", dest="verify_rows",
                        action="store_false")
    parser.add_argument("--stop-apps", dest="stop_apps",
                        action="store_true", default=None)
    parser.add_argument("--no-stop-apps", dest="stop_apps",
                        action="store_false")
    parser.add_argument("--clean-volume", dest="clean_volume",
                        action="store_true", default=None)
    parser.add_argument("--no-clean-volume", dest="clean_volume",
                        action="store_false")
    parser.add_argument("--quiesce", dest="quiesce",
                        action="store_true", default=None)
    parser.add_argument("--no-quiesce", dest="quiesce",
                        action="store_false")
    parser.add_argument("--keep-helper", action="store_true")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Docker Compose → Kubernetes 数据迁移工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="分析 compose 并生成迁移计划/脚本")
    add_plan_args(p)
    r = sub.add_parser("run", help="执行迁移阶段")
    add_run_args(r)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "plan":
            generate_plan(args)
            return 0
        return cmd_run(args)
    except MigrationError as exc:
        log("")
        log("[FAIL] %s" % exc)
        return 1
    except FileNotFoundError as exc:
        log("")
        log("[FAIL] 文件不存在: %s" % exc)
        return 1
    except yaml.YAMLError as exc:
        log("")
        log("[FAIL] compose 解析失败: %s" % exc)
        return 1
    except KeyboardInterrupt:
        log("")
        log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
