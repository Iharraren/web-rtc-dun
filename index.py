"""
index.py — Starlette + Jinja2 signaling relay for WebRTC-over-CGNAT
Vercel serverless: all /api/* routes + / (viewer UI) handled here.
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
SECRET = os.environ.get("SERVER_SECRET", "test123")
STORE_PATH = "/tmp/webrtc_store.json"

# ── Jinja2 (inline template — no filesystem dependency on Vercel) ─────────────
# The HTML is loaded from the same directory as this file at import time.
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
try:
    with open(_HTML_PATH) as _f:
        _HTML_SRC = _f.read()
except FileNotFoundError:
    _HTML_SRC = "<h1>index.html not found</h1>"

jinja = Environment(loader=BaseLoader(), autoescape=False)
_template = jinja.from_string(_HTML_SRC)

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

def _ok(**kw):        return JSONResponse({"status": "ok", **kw})
def _err(msg, code):  return JSONResponse({"error": msg}, status_code=code)

# ── Route handlers ────────────────────────────────────────────────────────────

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
        if request.query_params.get("secret") != SECRET:
            return _err("forbidden", 403)
        store = load_store()
        pending = [
            {"session_id": sid, "offer": s["offer"], "ice_candidates": s.get("viewer_ice", [])}
            for sid, s in store["sessions"].items() if not s.get("answer")
        ]
        return JSONResponse({"pending": pending})

    body = await request.json()
    sid, offer = body.get("session_id"), body.get("offer")
    if not sid or not offer:
        return _err("missing session_id or offer", 400)
    store = load_store()
    store["sessions"][sid] = {"offer": offer, "answer": None, "viewer_ice": [], "server_ice": []}
    save_store(store)
    return _ok()


async def api_answer(request: Request):
    if request.method == "GET":
        sid = request.query_params.get("session_id")
        store = load_store()
        if not sid or sid not in store["sessions"]:
            return _err("session not found", 404)
        s = store["sessions"][sid]
        return JSONResponse({"answer": s.get("answer"), "ice_candidates": s.get("server_ice", [])})

    body = await request.json()
    if body.get("secret") != SECRET:
        return _err("forbidden", 403)
    sid = body.get("session_id")
    store = load_store()
    if not sid or sid not in store["sessions"]:
        return _err("session not found", 404)
    store["sessions"][sid]["answer"] = body.get("answer")
    save_store(store)
    return _ok()


async def api_ice(request: Request):
    if request.method == "GET":
        sid = request.query_params.get("session_id")
        role = request.query_params.get("role")
        store = load_store()
        if not sid or sid not in store["sessions"]:
            return _err("session not found", 404)
        key = "viewer_ice" if role == "server" else "server_ice"
        return JSONResponse({"candidates": store["sessions"][sid].get(key, [])})

    body = await request.json()
    sid, role = body.get("session_id"), body.get("role")
    store = load_store()
    if not sid or sid not in store["sessions"]:
        return _err("session not found", 404)
    key = "server_ice" if role == "server" else "viewer_ice"
    store["sessions"][sid].setdefault(key, []).append(body.get("candidate"))
    save_store(store)
    return _ok()


# ── CORS middleware ───────────────────────────────────────────────────────────
_CORS = {
    "access-control-allow-origin": "*",
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
            response = JSONResponse({}, status_code=200, headers=_CORS)
            await response(scope, receive, send)
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                hdrs = dict(message.get("headers", []))
                hdrs.update({k.encode(): v.encode() for k, v in _CORS.items()})
                message = {**message, "headers": list(hdrs.items())}
            await send(message)

        await self.inner(scope, receive, send_with_cors)


# ── App ───────────────────────────────────────────────────────────────────────
_starlette = Starlette(routes=[
    Route("/",             index,        methods=["GET"]),
    Route("/api/status",   api_status,   methods=["GET"]),
    Route("/api/register", api_register, methods=["POST"]),
    Route("/api/offer",    api_offer,    methods=["GET", "POST"]),
    Route("/api/answer",   api_answer,   methods=["GET", "POST"]),
    Route("/api/ice",      api_ice,      methods=["GET", "POST"]),
])

app = CORSMiddleware(_starlette)
