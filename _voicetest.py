"""Tests for voice_agent.py — mocks Gemini (llm), Vercel (deploy), and Twilio.
No network calls. Run from the project dir."""
import sys
import time
import types

import storage
storage.DB_PATH = storage.BASE_DIR / "vt_builder.db"
storage.CONFIG_PATH = storage.BASE_DIR / "vt_config.json"
storage.PROJECTS_DIR = storage.BASE_DIR / "vt_projects"
storage.PROJECTS_DIR.mkdir(exist_ok=True)
if storage.DB_PATH.exists():
    storage.DB_PATH.unlink()
storage.init_db()
storage.save_config({"gemini_api_key": "g-key", "model": "gemini-x",
                     "vercel_token": "v-tok"})

import llm
import deploy
import voice_agent as va

FAILS = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

# ============ Part A: real pipeline glue with llm/deploy mocked ============

# generate_site() should call Gemini, parse files, guarantee index.html
def fake_generate_text(**kw):
    text = ("<<<FILE: index.html>>>\n<!doctype html><html><body>Hi</body></html>\n<<<ENDFILE>>>\n"
            "<<<NOTES>>>\nStatic site.\n<<<ENDNOTES>>>")
    return types.SimpleNamespace(text=text, model=kw["model"], finish_reason="STOP",
                                 truncated=False, usage=None)
llm.generate_text = fake_generate_text
files, notes, model = va.generate_site("a portfolio site")
check("generate_site returns index.html", "index.html" in files)
check("generate_site parses notes", "Static" in notes)
check("generate_site uses configured model", model == "gemini-x")

# deploy_and_wait() should create a deployment, poll until READY, return canonical url
created = {}
def fake_create_deployment(token, name, fs, *, production, team_id=None):
    created.update(name=name, production=production, n=len(fs))
    return {"id": "dpl_1", "url": "https://dpl1-hash.vercel.app", "state": "BUILDING", "inspector": "i"}
poll = {"n": 0}
def fake_get_deployment(token, dep_id, team_id=None):
    poll["n"] += 1
    state = "READY" if poll["n"] >= 2 else "BUILDING"
    return {"id": dep_id, "url": "https://dpl1-hash.vercel.app", "state": state, "inspector": "i", "error": None}
deploy.create_deployment = fake_create_deployment
deploy.get_deployment = fake_get_deployment
va.DEPLOY_TIMEOUT = 10

proj = storage.create_project("My Portfolio", "a portfolio site", files, notes, model)
url = va.deploy_and_wait(proj["id"], files, "My Portfolio", {})
check("deploy_and_wait production flag", created.get("production") is True)
check("deploy_and_wait polled to READY", poll["n"] >= 2)
check("deploy_and_wait returns canonical url", url == "https://my-portfolio.vercel.app")
rec = storage.get_deploy_record(proj["id"])
check("deploy record saved READY", rec.get("state") == "READY")

# ============ Part B: background job (wrappers mocked) ============
sent = []
va.generate_site = lambda desc: ({"index.html": "<html>x</html>"}, "", "gemini-x")
va.deploy_and_wait = lambda pid, fs, name, env: "https://built.vercel.app"
va.send_whatsapp = lambda to, body: (sent.append((to, body)) or "SM1")

jid = va.start_build_job("build me a blog", "+15551234567")
for _ in range(50):
    if va.JOBS[jid]["status"] in ("done", "error", "done_no_whatsapp"):
        break
    time.sleep(0.1)
job = va.JOBS[jid]
check("job completes", job["status"] == "done")
check("job recorded url", job["url"] == "https://built.vercel.app")
check("whatsapp sent once", len(sent) == 1)
check("whatsapp to caller (no WHATSAPP_TO)", sent[0][0] == "+15551234567")
check("whatsapp body has link", "https://built.vercel.app" in sent[0][1])

# error path: generate fails -> job error + failure whatsapp
sent.clear()
def boom(desc): raise RuntimeError("Gemini key missing")
va.generate_site = boom
jid2 = va.start_build_job("broken request", "+15559999999")
for _ in range(50):
    if va.JOBS[jid2]["status"] in ("done", "error"):
        break
    time.sleep(0.1)
check("job error status", va.JOBS[jid2]["status"] == "error")
check("error whatsapp sent", len(sent) == 1 and "couldn't finish" in sent[0][1])

# ============ Part C: TwiML endpoints ============
from fastapi.testclient import TestClient
client = TestClient(va.app)

r = client.post("/voice/incoming")
xml = r.text
check("incoming is xml", r.headers["content-type"].startswith("application/xml"))
check("incoming gathers speech", 'input="speech"' in xml and 'action="/voice/handle"' in xml)

# valid speech -> readback + hangup, starts a job
before = len(va.JOBS)
va.generate_site = lambda d: ({"index.html": "<html>x</html>"}, "", "gemini-x")  # keep job from erroring
r = client.post("/voice/handle", data={"SpeechResult": "make a landing page", "From": "+15550001111"})
check("handle reads back request", "make a landing page" in r.text)
check("handle hangs up", "<Hangup/>" in r.text)
check("handle started a job", len(va.JOBS) == before + 1)

# empty speech -> retry gather, no new job
before = len(va.JOBS)
r = client.post("/voice/handle", data={"SpeechResult": "", "From": "+15550001111"})
check("empty speech reprompts", 'input="speech"' in r.text)
check("empty speech no job", len(va.JOBS) == before)

# status page
r = client.get("/")
check("home renders dashboard", r.status_code == 200 and "Voice" in r.text)
r = client.get("/jobs")
check("jobs endpoint json", r.status_code == 200 and isinstance(r.json(), list))

print("\n" + ("ALL VOICE TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
