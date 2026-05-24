"""Smoke tests for the AI Website Builder. Run from the project dir.
Exercises the parser, storage (create/version/rollback), and API endpoints
via FastAPI TestClient. Does NOT call the real Gemini API.
"""
import json
import os
import sys

import prompts
import storage

FAILS = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)

# ---------- 1. parser ----------
sample = """Some preamble that should be ignored.
<<<FILE: index.html>>>
<!doctype html><html><body><h1>Hi</h1>
<script>fetch('/api/x')</script></body></html>
<<<ENDFILE>>>
<<<FILE: backend/server.py>>>
from fastapi import FastAPI
app = FastAPI()
@app.get('/api/x')
def x(): return {'ok': True}
<<<ENDFILE>>>
<<<FILE: requirements.txt>>>
fastapi
uvicorn[standard]
<<<ENDFILE>>>
<<<NOTES>>>
Set OPENAI_API_KEY before running.
<<<ENDNOTES>>>
"""
files, notes = prompts.parse_files(sample)
check("parser finds 3 files", len(files) == 3)
check("parser keeps index.html first key", list(files)[0] == "index.html")
check("parser nested path", "backend/server.py" in files)
check("parser body intact", "FastAPI()" in files["backend/server.py"])
check("parser notes", "OPENAI_API_KEY" in notes)

# fence-stripping + salvage
salvage, _ = prompts.parse_files("```html\n<!doctype html><html></html>\n```")
check("salvage bare html", "index.html" in salvage)

# path traversal blocked
trav, _ = prompts.parse_files("<<<FILE: ../../etc/passwd>>>\nx\n<<<ENDFILE>>>")
check("path traversal stripped", all(".." not in p for p in trav))

# ---------- 2. storage ----------
storage.DB_PATH = storage.BASE_DIR / "test_builder.db"
storage.CONFIG_PATH = storage.BASE_DIR / "test_config.json"
storage.PROJECTS_DIR = storage.BASE_DIR / "test_projects"
storage.PROJECTS_DIR.mkdir(exist_ok=True)
if storage.DB_PATH.exists():
    storage.DB_PATH.unlink()
storage.init_db()

proj = storage.create_project("My Test App", "build a thing", files, notes, "gemini-test")
pid = proj["id"]
check("create_project ok", proj["current_version"] == 1)
check("has_backend detected", proj["has_backend"] is True)
check("files written to disk", (storage.project_dir(pid) / "backend" / "server.py").exists())

v2 = storage.add_version(pid, kind="refine", prompt="make it blue",
                         files={"index.html": "<html>blue</html>"}, notes="", model="gemini-test")
check("add_version bumps to 2", v2["current_version"] == 2)
check("refine replaced files on disk", not (storage.project_dir(pid) / "backend").exists())

v3 = storage.rollback_to_version(pid, 1)
check("rollback creates v3", v3["current_version"] == 3)
check("rollback restored backend", (storage.project_dir(pid) / "backend" / "server.py").exists())
check("version history length 3", len(v3["versions"]) == 3)

# project env
storage.set_project_env(pid, {"OPENAI_API_KEY": "sk-test", "EMPTY": ""})
env = storage.get_project_env(pid)
check("env saved", env.get("OPENAI_API_KEY") == "sk-test")
check("project .env written", (storage.project_dir(pid) / ".env").exists())

# config
storage.save_config({"model": "gemini-9.9-ultra", "temperature": 0.9})
cfg = storage.load_config()
check("config saved model", cfg["model"] == "gemini-9.9-ultra")

# ---------- 3. API via TestClient ----------
from fastapi.testclient import TestClient
import server
client = TestClient(server.app)

r = client.get("/")
check("GET / serves UI", r.status_code == 200 and "Weaver" in r.text)

r = client.get("/api/settings")
check("GET /api/settings", r.status_code == 200 and "model" in r.json())

r = client.get("/api/projects")
check("GET /api/projects", r.status_code == 200 and "projects" in r.json())

# generate with no key -> 401 friendly error
storage.save_config({"gemini_api_key": ""})
r = client.post("/api/generate", json={"prompt": "a landing page"})
check("generate without key -> 401", r.status_code == 401 and "key" in r.json().get("error", "").lower())

# preview serving of the project we made
r = client.get(f"/preview/{pid}/")
check("preview serves index.html", r.status_code == 200 and "<html" in r.text.lower())

# download zip
r = client.get(f"/api/projects/{pid}/download")
check("download zip", r.status_code == 200 and r.headers["content-type"] == "application/zip" and len(r.content) > 100)

# run-status on a fresh project (no run yet)
r = client.get(f"/api/projects/{pid}/run-status")
check("run-status default stopped", r.status_code == 200 and r.json()["status"] in ("stopped",))

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
