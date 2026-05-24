"""Verify the restyled index.html: every element id the JS references still exists,
the JS still contains all key handlers, and the server serves the page."""
import re, sys
from pathlib import Path

html = Path("index.html").read_text(encoding="utf-8")
ids = set(re.findall(r'id="([^"]+)"', html))
# the script body (after the last <script>)
script = html.rsplit("<script>", 1)[-1]
refs = set(re.findall(r"\$\('([^']+)'\)", script))
refs |= set(re.findall(r"getElementById\('([^']+)'\)", script))

FAILS = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

missing = sorted(r for r in refs if r not in ids)
check(f"all {len(refs)} JS id-refs exist in DOM", not missing)
if missing: print("   MISSING IDS:", missing)

# selector-based hooks the JS relies on
check("has .tab buttons", 'class="tab' in html and "querySelectorAll('.tab')" in script)
check("has .example buttons", 'class="example"' in html)
check("has .view panels", 'class="view' in html)
check("deployTarget radios present", 'name="deployTarget"' in html)
check("data-tab attrs present", 'data-tab="preview"' in html and 'data-tab="code"' in html and 'data-tab="versions"' in html and 'data-tab="keys"' in html)
check("data-view attrs present", 'data-view="preview"' in html and 'data-view="code"' in html)

# functionality-bearing JS markers unchanged
for marker in ["/api/generate","/api/projects/'+state.project.id+'/refine",
               "/api/projects/'+state.project.id+'/deploy","/api/settings",
               "function send(","function renderAll(","function switchTab(",
               "function openDeploy(","hljs.highlightElement"]:
    check("JS keeps: "+marker, marker in script)

check("brand is Weaver", "<title>Weaver</title>" in html)
check("no leftover old brand title", "<title>AI Website Builder</title>" not in html)

# server still serves it
import storage
storage.DB_PATH = storage.BASE_DIR / "ui_builder.db"
storage.CONFIG_PATH = storage.BASE_DIR / "ui_config.json"
storage.PROJECTS_DIR = storage.BASE_DIR / "ui_projects"; storage.PROJECTS_DIR.mkdir(exist_ok=True)
if storage.DB_PATH.exists(): storage.DB_PATH.unlink()
storage.init_db()
from fastapi.testclient import TestClient
import server
client = TestClient(server.app)
r = client.get("/")
check("server GET / -> 200", r.status_code == 200)
check("served page has #promptInput", 'id="promptInput"' in r.text)
check("served page has #deployBtn", 'id="deployBtn"' in r.text)

print("\n" + ("ALL UI TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
