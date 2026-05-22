"""Tests for voice_realtime.py — conversation logic + TwiML.
Mocks Gemini (llm) and the build pipeline (va.start_build_job). No audio/network."""
import asyncio
import sys
import types

import storage
storage.DB_PATH = storage.BASE_DIR / "rt2_builder.db"
storage.CONFIG_PATH = storage.BASE_DIR / "rt2_config.json"
storage.PROJECTS_DIR = storage.BASE_DIR / "rt2_projects"
storage.PROJECTS_DIR.mkdir(exist_ok=True)
if storage.DB_PATH.exists():
    storage.DB_PATH.unlink()
storage.init_db()
storage.save_config({"gemini_api_key": "g", "model": "gemini-x"})

import voice_realtime as vr

FAILS = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

# --- mocks ---
RESP = {"text": ""}
def fake_generate_text(**kw):
    return types.SimpleNamespace(text=RESP["text"], model=kw.get("model"),
                                 finish_reason="STOP", truncated=False, usage=None)
vr.llm.generate_text = fake_generate_text

builds = []
vr.va.start_build_job = lambda spec, caller: (builds.append((spec, caller)) or "job1")

def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)

# 1) Normal turn: agent asks a question, no build yet
vr.MAX_TURNS = 4
RESP["text"] = "Sure! What colours or vibe would you like?"
s = vr.Session(None)
s.caller = "+15551112222"
out = run(s.think_and_reply("I want a portfolio site"))
check("normal turn returns question", "colours" in out.lower())
check("normal turn no build", s.pending_end is False and len(builds) == 0)
check("normal turn recorded history", s.history[-2][0] == "USER" and s.history[-1][0] == "AGENT")

# 2) READY turn: agent confirms + emits spec -> triggers build, says goodbye, ends
builds.clear()
RESP["text"] = "Great, a dark photography portfolio with a gallery.\nREADY::Dark-themed photography portfolio with home, gallery, and contact pages; moody colours; lightbox gallery; contact form."
s2 = vr.Session(None)
s2.caller = "+15553334444"
out2 = run(s2.think_and_reply("dark theme, gallery and contact"))
check("READY does not speak the marker", "READY::" not in out2 and "READY" not in out2)
check("READY speaks confirmation", "portfolio" in out2.lower())
check("READY adds goodbye", "whatsapp" in out2.lower() and "goodbye" in out2.lower())
check("READY sets pending_end", s2.pending_end is True)
check("build triggered once", len(builds) == 1)
check("build spec passed through", "lightbox gallery" in builds[0][0])
check("build caller passed through", builds[0][1] == "+15553334444")

# 3) Force-build safety net after MAX_TURNS even if model didn't say READY
builds.clear()
vr.MAX_TURNS = 1
RESP["text"] = "And what's the business name?"   # model keeps asking
s3 = vr.Session(None)
s3.caller = "+15555556666"
out3 = run(s3.think_and_reply("a bakery website with online orders"))
check("force-build triggers at MAX_TURNS", len(builds) == 1 and s3.pending_end is True)
check("force-build spec from history", "bakery" in builds[0][0].lower())
vr.MAX_TURNS = 4

# 4) Gemini failure -> graceful fallback (no crash, no build)
builds.clear()
def boom(**kw): raise RuntimeError("network down")
vr.llm.generate_text = boom
s4 = vr.Session(None); s4.caller = "+1"
out4 = run(s4.think_and_reply("make me a site"))
check("gemini failure graceful", isinstance(out4, str) and len(out4) > 0)
check("gemini failure no build", len(builds) == 0)
vr.llm.generate_text = fake_generate_text

# 5) TwiML endpoint: Connect>Stream with wss url + from parameter
from fastapi.testclient import TestClient
client = TestClient(vr.app)
r = client.post("/voice/incoming", data={"From": "+15551234567"})
xml = r.text
check("incoming xml", r.headers["content-type"].startswith("application/xml"))
check("incoming has Connect+Stream", "<Connect>" in xml and "<Stream" in xml)
check("incoming wss url to /media", 'wss://' in xml and '/media"' in xml)
check("incoming passes caller param", 'name="from" value="+15551234567"' in xml)

# 6) readiness dashboard + jobs
r = client.get("/")
check("home dashboard renders", r.status_code == 200 and "voice" in r.text.lower())
r = client.get("/jobs")
check("jobs endpoint json list", r.status_code == 200 and isinstance(r.json(), list))

print("\n" + ("ALL REALTIME TESTS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
