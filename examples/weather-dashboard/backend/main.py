import os
import random

from fastapi import FastAPI

CITY = os.getenv("CITY", "Guangzhou")
app = FastAPI()

CONDITIONS = ["Sunny", "Cloudy", "Light Rain", "Windy", "Humid"]


@app.get("/health")
def health():
    return {"status": "ok", "app": "weather-backend"}


@app.get("/api/weather")
def weather():
    random.seed(CITY + "2026")
    days = []
    for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        days.append({
            "day": d,
            "temp": 22 + (i * 3 % 9),
            "cond": CONDITIONS[i % len(CONDITIONS)],
        })
    return {"city": CITY, "updated": "2026-10-17", "days": days}
