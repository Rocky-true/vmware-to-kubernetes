import os
import time
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

DB_HOST = os.getenv("DB_HOST", "db")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "library")
DB_PASSWORD = os.getenv("DB_PASSWORD", "library_pass_2026")
DB_NAME = os.getenv("DB_NAME", "library")
APP_NAME = os.getenv("APP_NAME", "Library Borrowing")


def connect():
    return psycopg.connect(host=DB_HOST, port=DB_PORT, user=DB_USER,
                           password=DB_PASSWORD, dbname=DB_NAME,
                           row_factory=dict_row)


def init_db():
    for _ in range(40):
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS books (
                        id SERIAL PRIMARY KEY,
                        title TEXT NOT NULL,
                        author TEXT NOT NULL,
                        reader TEXT,
                        borrowed_at TIMESTAMPTZ
                    )
                """)
                cur.execute("SELECT count(*) AS n FROM books")
                if cur.fetchone()["n"] == 0:
                    cur.executemany(
                        "INSERT INTO books(title, author) VALUES(%s, %s)",
                        [("三体", "刘慈欣"), ("活着", "余华"),
                         ("人类简史", "尤瓦尔·赫拉利")])
                conn.commit()
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("database not ready after 40s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=APP_NAME, lifespan=lifespan)


class BookIn(BaseModel):
    title: str
    author: str


class BorrowIn(BaseModel):
    reader: str


@app.get("/health")
def health():
    return {"status": "ok", "app": "library-backend"}


@app.get("/api/books")
def list_books():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, title, author, reader, "
                    "borrowed_at::text AS borrowed_at FROM books ORDER BY id")
        return cur.fetchall()


@app.get("/api/stats")
def stats():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total, "
                    "count(reader) AS borrowed FROM books")
        return cur.fetchone()


@app.post("/api/books")
def add_book(book: BookIn):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO books(title, author) VALUES(%s, %s) "
                    "RETURNING id", (book.title, book.author))
        book_id = cur.fetchone()["id"]
        conn.commit()
        return {"id": book_id, "title": book.title, "author": book.author}


@app.post("/api/books/{book_id}/borrow")
def borrow(book_id: int, req: BorrowIn):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE books SET reader=%s, borrowed_at=now() "
                    "WHERE id=%s AND reader IS NULL RETURNING id",
                    (req.reader, book_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=409,
                                detail="book not found or already borrowed")
        return {"ok": True, "id": book_id, "reader": req.reader}


@app.post("/api/books/{book_id}/return")
def return_book(book_id: int):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE books SET reader=NULL, borrowed_at=NULL "
                    "WHERE id=%s RETURNING id", (book_id,))
        row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="book not found")
        return {"ok": True, "id": book_id}
