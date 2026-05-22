"""Tests for deploy.py adaptation + that the new endpoints register.
Does NOT call the real Vercel API."""
import sys
import deploy

FAILS = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

# ---- slugify ----
check("slug lowercases+hyphens", deploy.slugify_name("My Cool App!") == "my-cool-app")
check("slug collapses dashes", deploy.slugify_name("a   b") == "a-b")
check("slug leading digit", deploy.slugify_name("123go").startswith("app-"))
check("slug empty -> app", deploy.slugify_name("  ") == "app")

# ---- static project ----
static_files = {"index.html": "<!doctype html><html><body>hi</body></html>"}
ps = deploy.prepare_files(static_files)
check("static kind", ps.kind == "static")
check("static keeps index.html", ps.files == static_files)
check("static no warnings", ps.warnings == [])

# ---- python project (CRUD, no websocket) ----
py_files = {
    "index.html": "<html><script>fetch('/api/items')</script></html>",
    "backend/server.py": (
        "import os\n"
        "from fastapi import FastAPI\n"
        "from fastapi.responses import HTMLResponse\n"
        "from pathlib import Path\n"
        "app = FastAPI()\n"
        "KEY = os.getenv('OPENAI_API_KEY')\n"
        "@app.get('/', response_class=HTMLResponse)\n"
        "def idx(): return HTMLResponse((Path(__file__).resolve().parent.parent/'index.html').read_text())\n"
        "@app.get('/api/items')\n"
        "def items(): return []\n"
    ),
    "backend/requirements.txt": "fastapi\nuvicorn[standard]\nhttpx\n",
    ".env.example": "OPENAI_API_KEY=\n",
}
pp = deploy.prepare_files(py_files)
check("python kind", pp.kind == "python")
check("creates api/index.py", "api/index.py" in pp.files)
check("shim imports app from server", "from server import app as app" in pp.files["api/index.py"])
check("shim adds backend to path", 'parent / "backend"' in pp.files["api/index.py"])
check("creates vercel.json", "vercel.json" in pp.files)
check("vercel.json routes to function", "/api/index" in pp.files["vercel.json"])
check("root requirements has fastapi", pp.files["requirements.txt"].splitlines()[0] != "" and "fastapi" in pp.files["requirements.txt"])
check("root requirements drops uvicorn", "uvicorn" not in pp.files["requirements.txt"])
check("root requirements keeps httpx", "httpx" in pp.files["requirements.txt"])
check("keeps original backend file", "backend/server.py" in pp.files)
check("keeps index.html", "index.html" in pp.files)
check("warns about env (os.getenv)", pp.needs_env is True)
check("env warning present", any("environment variables" in w for w in pp.warnings))
check("no websocket warning here", not any("WebSocket" in w for w in pp.warnings))

# ---- python project with websocket + sqlite ----
ws_files = {
    "index.html": "<html><script>new WebSocket('ws://'+location.host+'/live')</script></html>",
    "server.py": (
        "import sqlite3\n"
        "from fastapi import FastAPI, WebSocket\n"
        "app = FastAPI()\n"
        "DB = sqlite3.connect('data.db')\n"
        "@app.websocket('/live')\n"
        "async def live(ws: WebSocket): await ws.accept()\n"
    ),
    "requirements.txt": "fastapi\n",
}
pw = deploy.prepare_files(ws_files)
check("ws entry detected (server.py at root)", "api/index.py" in pw.files)
check("ws warning present", any("WebSocket" in w for w in pw.warnings))
check("sqlite warning present", any("SQLite" in w for w in pw.warnings))

# ---- app var detection (non-default name) ----
custom = {"index.html": "<html></html>", "main.py": "from fastapi import FastAPI\napi = FastAPI()\n"}
pc = deploy.prepare_files(custom)
check("custom app var in shim", "from main import api as app" in pc.files["api/index.py"])

# ---- endpoints register on the app ----
import storage
storage.DB_PATH = storage.BASE_DIR / "dt_builder.db"
storage.CONFIG_PATH = storage.BASE_DIR / "dt_config.json"
storage.PROJECTS_DIR = storage.BASE_DIR / "dt_projects"
storage.PROJECTS_DIR.mkdir(exist_ok=True)
if storage.DB_PATH.exists(): storage.DB_PATH.unlink()
storage.init_db()
import server
from fastapi.testclient import TestClient
client = TestClient(server.app)

proj = storage.create_project("Deploy Me", "x", py_files, "", "test")
pid = proj["id"]

r = client.get(f"/api/projects/{pid}/deploy-info")
check("deploy-info ok", r.status_code == 200 and r.json()["kind"] == "python")
check("deploy-info lists files", "api/index.py" in r.json()["files"])
check("deploy-info no token", r.json()["has_vercel_token"] is False)

# deploy without a token -> 401 friendly
r = client.post(f"/api/projects/{pid}/deploy", json={"production": False})
check("deploy without token -> 401", r.status_code == 401 and "token" in r.json().get("error","").lower())

# settings expose vercel fields
r = client.get("/api/settings")
check("settings expose vercel flags", "has_vercel_token" in r.json())

# saving a vercel token round-trips (masked)
r = client.post("/api/settings", json={"vercel_token": "abcd1234efgh"})
check("save vercel token", r.json()["has_vercel_token"] is True and r.json()["vercel_token_hint"].endswith("efgh"))

print("\n" + ("ALL DEPLOY TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
