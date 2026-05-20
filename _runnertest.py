"""End-to-end test of runner.py: build a tiny project with a FastAPI backend,
launch it via the runner, hit its endpoints, then stop it."""
import subprocess, sys, time, json, urllib.request
from pathlib import Path

import storage
storage.DB_PATH = storage.BASE_DIR / "rt_builder.db"
storage.CONFIG_PATH = storage.BASE_DIR / "rt_config.json"
storage.PROJECTS_DIR = storage.BASE_DIR / "rt_projects"
storage.PROJECTS_DIR.mkdir(exist_ok=True)
if storage.DB_PATH.exists(): storage.DB_PATH.unlink()
storage.init_db()

import runner as runner_mod
runner = runner_mod.runner

index_html = "<!doctype html><html><body><h1>RUNNER OK</h1></body></html>"
server_py = (
    "from fastapi import FastAPI\n"
    "from fastapi.responses import HTMLResponse\n"
    "from pathlib import Path\n"
    "app = FastAPI()\n"
    "@app.get('/', response_class=HTMLResponse)\n"
    "def idx():\n"
    "    p = Path(__file__).resolve().parent.parent / 'index.html'\n"
    "    return HTMLResponse(p.read_text(encoding='utf-8'))\n"
    "@app.get('/api/ping')\n"
    "def ping(): return {'pong': True}\n"
)
files = {"index.html": index_html, "backend/server.py": server_py,
         "backend/requirements.txt": "fastapi\nuvicorn[standard]\n"}
proj = storage.create_project("Runner Test", "test", files, "", "test")
pid = proj["id"]
pdir = storage.project_dir(pid)

# Pre-create the venv with system site-packages so the pip step reuses the
# already-installed fastapi/uvicorn (keeps this test fast). The runner's own
# venv-creation path is plain stdlib `python -m venv`.
subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(pdir / ".venv")], check=True)

print("starting backend via runner...")
runner.start(pid)
deadline = time.time() + 40
status = {}
while time.time() < deadline:
    status = runner.status(pid)
    if status["status"] in ("running", "error", "exited"):
        break
    time.sleep(1)

print("final status:", status["status"], "| port:", status["port"], "| msg:", status["message"])
ok = True
if status["status"] != "running":
    print("LOGS:\n" + "\n".join(status["logs"][-25:]))
    ok = False
else:
    port = status["port"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            body = r.read().decode()
        print("GET / ->", "RUNNER OK" in body)
        ok = ok and ("RUNNER OK" in body)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=5) as r:
            j = json.loads(r.read().decode())
        print("GET /api/ping ->", j)
        ok = ok and (j.get("pong") is True)
    except Exception as e:
        print("request error:", e); ok = False

print("stopping...")
st = runner.stop(pid)
print("after stop:", st["status"])
ok = ok and (st["status"] == "stopped")

print("\n" + ("RUNNER E2E PASSED" if ok else "RUNNER E2E FAILED"))
sys.exit(0 if ok else 1)
