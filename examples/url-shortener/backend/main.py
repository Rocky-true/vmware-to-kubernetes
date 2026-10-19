import os
import random
import string

import redis.asyncio as aioredis
from fastapi import FastAPI
from pydantic import BaseModel


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = env_int("REDIS_PORT", 6379)
REDIS_PASS = os.getenv("REDIS_PASS", "shortpass")

app = FastAPI()
r = aioredis.from_url(
    "redis://:%s@%s:%s/0" % (REDIS_PASS, REDIS_HOST, REDIS_PORT),
    decode_responses=True,
    socket_connect_timeout=3,
)


class LinkIn(BaseModel):
    url: str


@app.get("/health")
def health():
    return {"status": "ok", "app": "shortener-backend"}


@app.get("/api/links")
async def links():
    out = []
    for key in await r.keys("link:*"):
        out.append({"code": key.split(":", 1)[1], "url": await r.get(key)})
    out.sort(key=lambda x: x["code"])
    return out


@app.post("/api/shorten")
async def shorten(link: LinkIn):
    code = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    await r.set("link:%s" % code, link.url)
    return {"code": code, "url": link.url}
