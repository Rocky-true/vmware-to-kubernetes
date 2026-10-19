import asyncio
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---- 配置（环境变量可覆盖；Docker/K8s 中强制 mysql+redis） ----
DB_MODE = os.getenv("DB_MODE", "auto")          # auto | mysql | sqlite
STORE_MODE = os.getenv("STORE_MODE", "auto")    # auto | redis | memory

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


DB_PORT = env_int("DB_PORT", 3306)
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS", "xhh123456")
DB_NAME = os.getenv("DB_NAME", "course")
SQLITE_PATH = os.getenv("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "course.db"))

REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = env_int("REDIS_PORT", 6379)
REDIS_PASS = os.getenv("REDIS_PASS", "xhh123456")

STUDENTS = int(os.getenv("STUDENT_COUNT", "40000"))
COURSES = int(os.getenv("COURSE_COUNT", "300"))
CAPACITY = int(os.getenv("COURSE_CAPACITY", "120"))

LUA = """
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then return 2 end
local stock = tonumber(redis.call('GET', KEYS[1]) or '-1')
if stock < 0 then return -1 end
if stock <= 0 then return 0 end
redis.call('DECR', KEYS[1])
redis.call('SADD', KEYS[2], ARGV[1])
return 1
"""

_sqlite_lock = threading.Lock()


def db_conn():
    import pymysql
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
        database=DB_NAME, autocommit=True, charset="utf8mb4",
        connect_timeout=2,
    )


def sqlite_conn():
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def resolve_db_backend():
    if DB_MODE == "sqlite":
        return "sqlite"
    attempts = 60 if DB_MODE == "mysql" else 3
    last = None
    for _ in range(attempts):
        try:
            c = db_conn()
            c.ping()
            c.close()
            return "mysql"
        except Exception as e:  # noqa
            last = e
            time.sleep(2)
    if DB_MODE == "mysql":
        raise RuntimeError(f"MySQL not ready: {last}")
    return "sqlite"


def init_db_mysql():
    c = db_conn()
    cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS students(
        id INT PRIMARY KEY, name VARCHAR(32)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cur.execute("""CREATE TABLE IF NOT EXISTS courses(
        id INT PRIMARY KEY, name VARCHAR(64), teacher VARCHAR(32), capacity INT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cur.execute("""CREATE TABLE IF NOT EXISTS selections(
        id BIGINT AUTO_INCREMENT PRIMARY KEY, student_id INT NOT NULL,
        course_id INT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_student_course (student_id, course_id),
        KEY idx_course (course_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    cur.execute("SELECT COUNT(*) FROM students")
    have = cur.fetchone()[0]
    if have < STUDENTS:
        rows = [(i, f"学生{i:05d}") for i in range(have + 1, STUDENTS + 1)]
        for i in range(0, len(rows), 5000):
            cur.executemany("INSERT IGNORE INTO students(id,name) VALUES(%s,%s)", rows[i:i + 5000])
    cur.execute("SELECT COUNT(*) FROM courses")
    have = cur.fetchone()[0]
    if have < COURSES:
        rows = build_courses(have)
        for i in range(0, len(rows), 1000):
            cur.executemany("INSERT IGNORE INTO courses(id,name,teacher,capacity) VALUES(%s,%s,%s,%s)", rows[i:i + 1000])
    c.close()


def init_db_sqlite():
    with _sqlite_lock:
        c = sqlite_conn()
        cur = c.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, name TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS courses(id INTEGER PRIMARY KEY, name TEXT, teacher TEXT, capacity INTEGER)")
        cur.execute("""CREATE TABLE IF NOT EXISTS selections(
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(student_id, course_id))""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_course ON selections(course_id)")
        cur.execute("SELECT COUNT(*) FROM students")
        have = cur.fetchone()[0]
        if have < STUDENTS:
            rows = [(i, f"学生{i:05d}") for i in range(have + 1, STUDENTS + 1)]
            cur.executemany("INSERT OR IGNORE INTO students(id,name) VALUES(?,?)", rows)
        cur.execute("SELECT COUNT(*) FROM courses")
        have = cur.fetchone()[0]
        if have < COURSES:
            cur.executemany("INSERT OR IGNORE INTO courses(id,name,teacher,capacity) VALUES(?,?,?,?)", build_courses(have))
        c.commit()
        c.close()


BASE_NAMES = ["高等数学", "大学英语", "数据结构", "操作系统", "计算机网络", "线性代数",
              "概率论", "大学物理", "离散数学", "数据库原理", "软件工程", "人工智能导论",
              "机器学习", "深度学习", "云计算", "信息安全", "编译原理", "计算机组成",
              "数字电路", "模拟电路"]


def build_courses(have):
    rows = []
    for i in range(have + 1, COURSES + 1):
        n = BASE_NAMES[(i - 1) % len(BASE_NAMES)]
        suffix = f"（{(i - 1) // len(BASE_NAMES) + 1}班）" if i > len(BASE_NAMES) else ""
        rows.append((i, f"{n}{suffix}", f"教师{(i - 1) % 60 + 1:02d}", CAPACITY))
    return rows


def load_courses(backend):
    if backend == "mysql":
        c = db_conn()
        cur = c.cursor()
        cur.execute("SELECT id, name, teacher, capacity FROM courses ORDER BY id")
        rows = cur.fetchall()
        c.close()
    else:
        with _sqlite_lock:
            c = sqlite_conn()
            rows = c.execute("SELECT id, name, teacher, capacity FROM courses ORDER BY id").fetchall()
            c.close()
    return [{"id": r[0], "name": r[1], "teacher": r[2], "capacity": r[3]} for r in rows]


def insert_selection(backend, student_id, course_id):
    try:
        if backend == "mysql":
            c = db_conn()
            cur = c.cursor()
            cur.execute("INSERT IGNORE INTO selections(student_id, course_id) VALUES(%s,%s)",
                        (student_id, course_id))
            c.close()
        else:
            with _sqlite_lock:
                c = sqlite_conn()
                c.execute("INSERT OR IGNORE INTO selections(student_id, course_id) VALUES(?,?)",
                          (student_id, course_id))
                c.commit()
                c.close()
        return True
    except Exception:
        return False


def count_selections_db(backend):
    try:
        if backend == "mysql":
            c = db_conn()
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM selections")
            n = cur.fetchone()[0]
            c.close()
            return n
        with _sqlite_lock:
            c = sqlite_conn()
            n = c.execute("SELECT COUNT(*) FROM selections").fetchone()[0]
            c.close()
            return n
    except Exception:
        return -1


class MemoryStore:
    def __init__(self, courses):
        self.stocks = {c["id"]: c["capacity"] for c in courses}
        self.users = {c["id"]: set() for c in courses}
        self.lock = asyncio.Lock()

    async def select(self, cid, sid):
        async with self.lock:
            if cid not in self.stocks:
                return -1
            if sid in self.users[cid]:
                return 2
            if self.stocks[cid] <= 0:
                return 0
            self.stocks[cid] -= 1
            self.users[cid].add(sid)
            return 1

    async def remaining(self, cid):
        return self.stocks.get(cid, -1)

    async def remaining_many(self, ids):
        return [self.stocks.get(i, -1) for i in ids]

    async def selected_count(self, cid):
        return len(self.users.get(cid, ()))


class RedisStore:
    def __init__(self, client, sha):
        self.r = client
        self.sha = sha

    async def select(self, cid, sid):
        return await self.r.evalsha(self.sha, 2, f"course:stock:{cid}",
                                    f"course:users:{cid}", str(sid))

    async def remaining(self, cid):
        v = await self.r.get(f"course:stock:{cid}")
        return int(v) if v is not None else -1

    async def remaining_many(self, ids):
        vals = await self.r.mget([f"course:stock:{i}" for i in ids])
        return [int(v) if v is not None else -1 for v in vals]

    async def selected_count(self, cid):
        return await self.r.scard(f"course:users:{cid}")

    async def preload(self, courses):
        pipe = self.r.pipeline()
        for c in courses:
            pipe.set(f"course:stock:{c['id']}", c["capacity"], nx=True)
        await pipe.execute()


def resolve_store(courses):
    if STORE_MODE == "memory":
        return MemoryStore(courses), "memory"
    try:
        import redis.asyncio as aioredis
        r = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT,
                           password=REDIS_PASS or None, decode_responses=True,
                           socket_connect_timeout=2)
        return r, "redis"
    except Exception:
        if STORE_MODE == "redis":
            raise
        return MemoryStore(courses), "memory"


@asynccontextmanager
async def lifespan(app: FastAPI):
    backend = resolve_db_backend()
    if backend == "mysql":
        init_db_mysql()
    else:
        init_db_sqlite()
    app.state.db_backend = backend

    meta = load_courses(backend)
    app.state.courses = meta

    store_obj, store_mode = resolve_store(meta)
    if store_mode == "redis":
        r = store_obj
        ok = False
        attempts = 60 if STORE_MODE == "redis" else 3
        for _ in range(attempts):
            try:
                await r.ping()
                ok = True
                break
            except Exception:  # noqa
                await asyncio.sleep(2 if STORE_MODE == "redis" else 1)
        if not ok:
            if STORE_MODE == "redis":
                raise RuntimeError("Redis not ready")
            store = MemoryStore(meta)
            store_mode = "memory"
        else:
            sha = await r.script_load(LUA)
            store = RedisStore(r, sha)
            await store.preload(meta)
    else:
        store = store_obj
    app.state.store = store
    app.state.store_mode = store_mode
    app.state.ready = True
    yield
    if store_mode == "redis":
        await store.r.aclose()


app = FastAPI(title="校园选课系统", lifespan=lifespan)


class SelectReq(BaseModel):
    student_id: int
    course_id: int


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ready": getattr(app.state, "ready", False),
        "db": getattr(app.state, "db_backend", "?"),
        "store": getattr(app.state, "store_mode", "?"),
    }


@app.get("/api/courses")
async def list_courses():
    store = app.state.store
    meta = app.state.courses
    stocks = await store.remaining_many([c["id"] for c in meta])
    return {"total": len(meta), "items": [{**c, "remaining": s} for c, s in zip(meta, stocks)]}


@app.get("/api/courses/{course_id}")
async def get_course(course_id: int):
    meta = next((c for c in app.state.courses if c["id"] == course_id), None)
    if not meta:
        raise HTTPException(404, "课程不存在")
    return {**meta, "remaining": await app.state.store.remaining(course_id)}


@app.post("/api/select")
async def select(req: SelectReq):
    store = app.state.store
    res = await store.select(req.course_id, req.student_id)
    if res == 2:
        return {"code": 2, "msg": "该课程已选过", "remaining": await store.remaining(req.course_id)}
    if res == 0:
        return {"code": 1, "msg": "课程名额已满", "remaining": 0}
    if res == -1:
        raise HTTPException(404, "课程不存在")
    await asyncio.to_thread(insert_selection, app.state.db_backend, req.student_id, req.course_id)
    return {"code": 0, "msg": "选课成功", "remaining": await store.remaining(req.course_id)}


@app.get("/api/stats")
async def stats():
    store = app.state.store
    total = 0
    hot = []
    for c in app.state.courses:
        n = await store.selected_count(c["id"])
        total += n
        hot.append({**c, "selected": n})
    hot.sort(key=lambda x: x["selected"], reverse=True)
    return {
        "total_selections": total,
        "db_selections": count_selections_db(app.state.db_backend),
        "hot_courses": hot[:10],
    }


@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
