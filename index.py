"""
index.py — Starlette + Jinja2 signaling relay for WebRTC-over-CGNAT
Bundled ICE model: no separate /api/ice polling needed.
  Viewer  → POST /api/offer   { session_id, offer, ice: [...] }
  Server  → GET  /api/offer   → pending sessions with bundled viewer ICE
  Server  → POST /api/answer  { secret, session_id, answer, ice: [...] }
  Viewer  → GET  /api/answer  → { answer, ice: [...] }
  Server  → POST /api/register (heartbeat)
  All     → GET  /api/status
  All     → GET  /api/turn    → iceServers array with live TURN credentials
"""
import json
import os
import time
import urllib.request
import urllib.error

from jinja2 import Environment, BaseLoader
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

# ── Config ────────────────────────────────────────────────────────────────────
SECRET          = os.environ.get("SERVER_SECRET", "test123")

METERED_API_KEY = os.environ.get("METERED_API_KEY", "bdb349d8bf423ddc69f86dc3501c3422c043")
STORE_PATH      = "/tmp/webrtc_store.json"
TURN_CACHE_PATH = "/tmp/webrtc_turn_cache.json"
TURN_CACHE_TTL  = 3600  # seconds — refresh credentials every hour

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

# ── TURN credential helpers ───────────────────────────────────────────────────
_FALLBACK_ICE = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:stun1.l.google.com:19302"},
]

def _load_turn_cache() -> dict | None:
    try:
        with open(TURN_CACHE_PATH) as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) < TURN_CACHE_TTL:
            return data
    except Exception:
        pass
    return None

def _save_turn_cache(ice_servers: list) -> None:
    try:
        with open(TURN_CACHE_PATH, "w") as f:
            json.dump({"ts": time.time(), "ice_servers": ice_servers}, f)
    except Exception:
        pass

def _fetch_metered_credentials() -> list:
    """Fetch fresh TURN credentials from Metered API."""
    # Try account-specific endpoint first, fall back to openrelay
    endpoints = []
    if METERED_API_KEY:
        endpoints.append(
            f"https://ihraren.metered.live/api/v1/turn/credentials?apiKey={METERED_API_KEY}"
        )
    # openrelay.metered.ca is a free public TURN — no key needed
    endpoints.append(
        "https://openrelay.metered.ca/api/v1/turn/credentials?apiKey=openrelayproject"
    )
    for url in endpoints:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            if isinstance(data, list) and data:
                return data
        except Exception:
            continue
    return _FALLBACK_ICE

def get_ice_servers() -> list:
    """Return cached TURN credentials, refreshing if stale."""
    cached = _load_turn_cache()
    if cached:
        return cached["ice_servers"]
    ice = _fetch_metered_credentials()
    _save_turn_cache(ice)
    return ice

# ── Handlers ──────────────────────────────────────────────────────────────────

async def index(request: Request):
    return HTMLResponse(_template.render())


async def api_turn(request: Request):
    """Return iceServers array — viewer and home_server both call this."""
    return JSONResponse({"iceServers": get_ice_servers()})


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
            {"session_id": sid, "offer": s["offer"], "ice": s.get("viewer_ice", [])}
            for sid, s in store["sessions"].items()
            if not s.get("answer")
        ]
        return JSONResponse({"pending": pending})

    body = await request.json()
    sid, offer = body.get("session_id"), body.get("offer")
    if not sid or not offer:
        return _err("missing session_id or offer", 400)
    store = load_store()
    store["sessions"][sid] = {
        "offer":      offer,
        "viewer_ice": body.get("ice", []),
        "answer":     None,
        "server_ice": [],
    }
    save_store(store)
    return _ok()


async def api_answer(request: Request):
    if request.method == "GET":
        sid = request.query_params.get("session_id")
        store = load_store()
        if not sid or sid not in store["sessions"]:
            return _err("session not found", 404)
        s = store["sessions"][sid]
        return JSONResponse({"answer": s.get("answer"), "ice": s.get("server_ice", [])})

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
    Route("/api/turn",     api_turn,     methods=["GET"]),
    Route("/api/status",   api_status,   methods=["GET"]),
    Route("/api/register", api_register, methods=["POST"]),
    Route("/api/offer",    api_offer,    methods=["GET", "POST"]),
    Route("/api/answer",   api_answer,   methods=["GET", "POST"]),
])

app = CORSMiddleware(_starlette)