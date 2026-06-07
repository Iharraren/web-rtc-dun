"""
index.py — Starlette + Jinja2 signaling relay for WebRTC-over-CGNAT
Bundled ICE model: no separate /api/ice polling needed.
  Viewer  → POST /api/offer   { session_id, offer, ice: [...] }
  Server  → GET  /api/offer   → pending sessions with bundled viewer ICE
  Server  → POST /api/answer  { secret, session_id, answer, ice: [...] }
  Viewer  → GET  /api/answer  → { answer, ice: [...] }
  Server  → POST /api/register (heartbeat)
  All     → GET  /api/status
"""
import json
import os
import time

from jinja2 import Environment, BaseLoader
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

# ── Config ────────────────────────────────────────────────────────────────────
SECRET     = os.environ.get("SERVER_SECRET", "test123")
STORE_PATH = "/tmp/webrtc_store.json"

# ── Jinja2 ────────────────────────────────────────────────────────────────────
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
try:
    with open(_HTML_PATH) as _f:
        _HTML_SRC = _f.read()
except FileNotFoundError:
    _HTML_SRC = "<h1>index.html not found</h1>"

_template = Environment(loader=BaseLoader(), autoescape=False).from_string(_HTML_SRC)

# ── Store helpers ─────────────────────────────────────────────────────────────
def load_store() -> dict:
    if not os.path.exists(STORE_PATH):
        return {"server_ts": 0, "sessions": {}}
    try:
        with open(STORE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"server_ts": 0, "sessions": {}}

def save_store(data: dict) -> None:
    with open(STORE_PATH, "w") as f:
        json.dump(data, f)

def _ok(**kw):       return JSONResponse({"status": "ok", **kw})
def _err(msg, code): return JSONResponse({"error": msg}, status_code=code)

# ── Handlers ──────────────────────────────────────────────────────────────────

async def index(request: Request):
    return HTMLResponse(_template.render())


async def api_status(request: Request):
    store = load_store()
    ts = store.get("server_ts", 0)
    if ts:
        age = int(time.time() - ts)
        return JSONResponse({"online": age < 30, "seconds_ago": age, "last_seen": ts})
    return JSONResponse({"online": False, "seconds_ago": None, "last_seen": None})


async def api_register(request: Request):
    body = await request.json()
    if body.get("secret") != SECRET:
        return _err("forbidden", 403)
    store = load_store()
    store["server_ts"] = time.time()
    save_store(store)
    return _ok()


async def api_offer(request: Request):
    if request.method == "GET":
        # home_server polls — must supply secret
        if request.query_params.get("secret") != SECRET:
            return _err("forbidden", 403)
        store = load_store()
        pending = [
            {"session_id": sid, "offer": s["offer"], "ice": s.get("viewer_ice", [])}
            for sid, s in store["sessions"].items()
            if not s.get("answer")
        ]
        return JSONResponse({"pending": pending})

    # POST — viewer submits offer + all its ICE candidates bundled
    body = await request.json()
    sid  = body.get("session_id")
    offer = body.get("offer")
    if not sid or not offer:
        return _err("missing session_id or offer", 400)
    store = load_store()
    store["sessions"][sid] = {
        "offer":      offer,
        "viewer_ice": body.get("ice", []),   # bundled viewer ICE
        "answer":     None,
        "server_ice": [],
    }
    save_store(store)
    return _ok()


async def api_answer(request: Request):
    if request.method == "GET":
        # viewer polls for answer
        sid = request.query_params.get("session_id")
        store = load_store()
        if not sid or sid not in store["sessions"]:
            return _err("session not found", 404)
        s = store["sessions"][sid]
        return JSONResponse({"answer": s.get("answer"), "ice": s.get("server_ice", [])})

    # POST — home_server submits answer + its ICE candidates bundled
    body = await request.json()
    if body.get("secret") != SECRET:
        return _err("forbidden", 403)
    sid = body.get("session_id")
    store = load_store()
    if not sid or sid not in store["sessions"]:
        return _err("session not found", 404)
    store["sessions"][sid]["answer"]     = body.get("answer")
    store["sessions"][sid]["server_ice"] = body.get("ice", [])
    save_store(store)
    return _ok()


# ── CORS middleware ───────────────────────────────────────────────────────────
_CORS = {
    "access-control-allow-origin":  "*",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "Content-Type",
}

class CORSMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        if scope["method"] == "OPTIONS":
            await JSONResponse({}, status_code=200, headers=_CORS)(scope, receive, send)
            return
        async def _send(message):
            if message["type"] == "http.response.start":
                hdrs = dict(message.get("headers", []))
                hdrs.update({k.encode(): v.encode() for k, v in _CORS.items()})
                message = {**message, "headers": list(hdrs.items())}
            await send(message)
        await self.inner(scope, receive, _send)


# ── App ───────────────────────────────────────────────────────────────────────
_starlette = Starlette(routes=[
    Route("/",             index,        methods=["GET"]),
    Route("/api/status",   api_status,   methods=["GET"]),
    Route("/api/register", api_register, methods=["POST"]),
    Route("/api/offer",    api_offer,    methods=["GET", "POST"]),
    Route("/api/answer",   api_answer,   methods=["GET", "POST"]),
])

app = CORSMiddleware(_starlette)
