import os
import sqlite3

from fastapi import FastAPI
from pydantic import BaseModel

DB = os.getenv("DB_PATH", "/data/todo.db")
app = FastAPI()


def conn():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS todos(id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " title TEXT NOT NULL, done INTEGER DEFAULT 0)")
    return c


@app.get("/health")
def health():
    return {"status": "ok", "app": "todo-backend"}


@app.get("/api/todos")
def list_todos():
    c = conn()
    rows = c.execute("SELECT id,title,done FROM todos ORDER BY id").fetchall()
    c.close()
    return [{"id": r[0], "title": r[1], "done": bool(r[2])} for r in rows]


class TodoIn(BaseModel):
    title: str


@app.post("/api/todos")
def add(t: TodoIn):
    c = conn()
    cur = c.execute("INSERT INTO todos(title) VALUES(?)", (t.title,))
    c.commit()
    i = cur.lastrowid
    c.close()
    return {"id": i, "title": t.title, "done": False}


@app.post("/api/todos/{i}/toggle")
def toggle(i: int):
    c = conn()
    c.execute("UPDATE todos SET done=1-done WHERE id=?", (i,))
    c.commit()
    c.close()
    return {"ok": True}


@app.delete("/api/todos/{i}")
def delete(i: int):
    c = conn()
    c.execute("DELETE FROM todos WHERE id=?", (i,))
    c.commit()
    c.close()
    return {"ok": True}
