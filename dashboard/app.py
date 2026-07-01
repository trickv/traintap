"""traintap dashboard — FastAPI backend.

Reads the CSVs traintap writes (mounted read-only at DATA_DIR) and serves JSON
stats + the static dashboard. Read-only: never writes to the data.
"""

import os
import time

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import aggregate

DATA_DIR = os.environ.get("DATA_DIR", "data")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="traintap dashboard")


@app.middleware("http")
async def no_cache(request, call_next):
    """Revalidate every asset (etag-based 304s) so a redeploy never leaves the
    browser rendering fresh HTML/JS against a stale cached CSS."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/status")
def api_status():
    return aggregate.current_status(DATA_DIR, time.time())


@app.get("/api/stats")
def api_stats(range: str = Query("24h", pattern="^(24h|7d|all)$")):
    return aggregate.all_stats(DATA_DIR, time.time(), range)


@app.get("/api/health")
def api_health():
    return {"ok": True, "data_dir": DATA_DIR,
            "has_trains": os.path.exists(os.path.join(DATA_DIR, "trains.csv"))}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
