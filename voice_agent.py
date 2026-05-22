"""
voice_agent.py
--------------
Inbound phone-call agent:  DIAL  →  SPEAK your request  →  it builds a website,
deploys it to Vercel (production), and WhatsApps you the live link.

It is a small, *separate* FastAPI service that REUSES the builder's engine:
  - generation:  llm.py + prompts.py     (Google Gemini)
  - storage:     storage.py              (shares config.json + projects with the UI)
  - deploy:      deploy.py               (Vercel REST API)
  - telephony + WhatsApp: Twilio

Because it reuses storage.config.json, your Gemini key and Vercel token are read
from the SAME place you set them in the builder's ⚙ Settings. Twilio + WhatsApp
settings come from a local .env (see .env.example).

Call flow (one-shot + text-back):
  1. Twilio hits  POST /voice/incoming  → we ask "what do you want to build?"
  2. Twilio transcribes the speech, hits  POST /voice/handle  with SpeechResult.
  3. We read the request back, say we'll text the link, and hang up.
  4. A background job generates → deploys → sends the Vercel link over WhatsApp.

Run:
    python voice_agent.py            # serves on :8001
Expose it publicly (Twilio must reach it), e.g.:
    ngrok http 8001
…then set your Twilio number's Voice webhook to  https://<tunnel>/voice/incoming
See README.md section 11.
"""

from __future__ import annotations

import html
import os
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import deploy
import llm
import prompts
import storage

load_dotenv(Path(__file__).resolve().parent / ".env")

# ---- Twilio / WhatsApp config (from .env) ---------------------------------
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN", "")
# Twilio WhatsApp sender. The shared sandbox number is +14155238886.
WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
# Where to send the finished link. If blank, we text the number that called.
WHATSAPP_TO = os.getenv("WHATSAPP_TO", "").strip()
DEPLOY_TIMEOUT = int(os.getenv("VOICE_DEPLOY_TIMEOUT", "210"))

storage.init_db()

app = FastAPI(title="Voice → Website → WhatsApp agent")

# job_id -> dict(status, request, name, project_id, url, error, caller, at)
JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


# ===========================================================================
# Reusable pipeline pieces (generate / deploy / notify)
# ===========================================================================

def _normalize_e164(num: str) -> str:
    num = (num or "").strip()
    if num.startswith("whatsapp:"):
        num = num[len("whatsapp:"):]
    return num


def generate_site(description: str) -> tuple[dict, str, str]:
    """Generate the website files from a spoken description. Returns (files, notes, model)."""
    cfg = storage.load_config()
    key = cfg.get("gemini_api_key", "")
    model = cfg.get("model") or "gemini-2.5-flash"
    if not key:
        raise RuntimeError("No Gemini API key — set it in the builder's Settings first.")
    user_prompt = prompts.build_generate_user_prompt(description, has_image=False)
    result = llm.generate_text(
        api_key=key,
        model=model,
        prompt=user_prompt,
        system_instruction=prompts.SYSTEM_GENERATE,
        temperature=float(cfg.get("temperature", 0.6)),
        max_output_tokens=int(cfg.get("max_output_tokens", 32768)),
    )
    files, notes = prompts.parse_files(result.text)
    if not any(p.split("/")[-1] == "index.html" for p in files):
        files["index.html"] = "<!doctype html><h2>No index.html produced.</h2>"
    return files, notes, result.model


def deploy_and_wait(project_id: str, files: dict, name: str, env: dict) -> str:
    """Deploy to Vercel (production) and block until READY. Returns the live URL."""
    cfg = storage.load_config()
    token = cfg.get("vercel_token", "")
    if not token:
        raise RuntimeError("No Vercel token — set it in the builder's Settings first.")
    team = (cfg.get("vercel_team_id") or "").strip() or None

    prep = deploy.prepare_files(files)
    slug = deploy.slugify_name(name)
    if prep.kind == "python" and env:
        deploy.ensure_project_and_env(token, slug, env, team)
    result = deploy.create_deployment(token, slug, prep.files, production=True, team_id=team)

    storage.set_deploy_record(project_id, {
        "id": result["id"], "url": result["url"], "state": result["state"],
        "inspector": result.get("inspector"), "production": True,
        "kind": prep.kind, "at": time.time(),
    })

    deadline = time.time() + DEPLOY_TIMEOUT
    info = result
    while time.time() < deadline:
        info = deploy.get_deployment(token, result["id"], team)
        if info["state"] in ("READY", "ERROR", "CANCELED"):
            break
        time.sleep(3)

    storage.set_deploy_record(project_id, {
        "id": info["id"], "url": info["url"], "state": info["state"],
        "inspector": info.get("inspector"), "production": True,
        "kind": prep.kind, "at": time.time(),
    })
    if info["state"] != "READY":
        raise RuntimeError(f"Vercel build did not succeed (state={info['state']}).")
    # Canonical production URL is nicer to share than the per-deploy hostname.
    return f"https://{slug}.vercel.app"


def _twilio_client():
    if not (TWILIO_SID and TWILIO_AUTH):
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set in .env")
    from twilio.rest import Client  # imported lazily so the builder needs no twilio
    return Client(TWILIO_SID, TWILIO_AUTH)


def send_whatsapp(to_number: str, body: str) -> str:
    to = _normalize_e164(to_number)
    if not to:
        raise RuntimeError("No WhatsApp recipient (set WHATSAPP_TO in .env).")
    client = _twilio_client()
    msg = client.messages.create(from_=WHATSAPP_FROM, to=f"whatsapp:{to}", body=body)
    return msg.sid


# ===========================================================================
# Background job: speech → site → deploy → WhatsApp
# ===========================================================================

def _set_job(job_id: str, **fields):
    with _JOBS_LOCK:
        JOBS.setdefault(job_id, {})
        JOBS[job_id].update(fields)


def start_build_job(description: str, caller: str) -> str:
    job_id = uuid.uuid4().hex[:10]
    _set_job(
        job_id,
        id=job_id, status="queued", request=description, caller=caller,
        name=_name_from(description), project_id=None, url=None, error=None,
        at=time.time(),
    )
    threading.Thread(target=_run_job, args=(job_id, description, caller), daemon=True).start()
    return job_id


def _run_job(job_id: str, description: str, caller: str) -> None:
    to = WHATSAPP_TO or _normalize_e164(caller)
    name = _name_from(description)
    try:
        _set_job(job_id, status="generating")
        files, notes, model = generate_site(description)

        project = storage.create_project(name, description, files, notes, model)
        pid = project["id"]
        _set_job(job_id, project_id=pid, status="deploying")

        env = storage.get_project_env(pid)
        url = deploy_and_wait(pid, files, name, env)

        _set_job(job_id, status="notifying", url=url)
        try:
            send_whatsapp(to, f"✅ Your website is live!\n\n“{name}”\n{url}\n\nBuilt from your call. — AI Website Builder")
            _set_job(job_id, status="done")
        except Exception as e:  # site is live, only the text failed
            _set_job(job_id, status="done_no_whatsapp", error=f"WhatsApp failed: {e}")
    except Exception as e:  # noqa: BLE001
        _set_job(job_id, status="error", error=str(e))
        try:
            send_whatsapp(to, f"⚠️ Sorry, I couldn't finish building “{name}”.\nReason: {e}")
        except Exception:
            pass


def _name_from(description: str) -> str:
    words = (description or "").split()
    return (" ".join(words[:6]) or "Voice site").strip()[:60]


# ===========================================================================
# TwiML helpers
# ===========================================================================

def _twiml(xml_body: str) -> Response:
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{xml_body}</Response>'
    return Response(content=xml, media_type="application/xml")


def _say(text: str) -> str:
    return f'<Say voice="Polly.Joanna">{html.escape(text)}</Say>'


# ===========================================================================
# Twilio webhooks
# ===========================================================================

@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    """Twilio dials this when a call comes in. Ask the caller what to build."""
    gather = (
        '<Gather input="speech" action="/voice/handle" method="POST" '
        'speechTimeout="auto" language="en-US" speechModel="phone_call">'
        + _say(
            "Hi! This is your A I website builder. After the tone, describe the "
            "website you'd like me to build, then pause when you're done."
        )
        + "</Gather>"
    )
    # If we got nothing, re-prompt once by looping back.
    reprompt = _say("I didn't catch that.") + '<Redirect method="POST">/voice/incoming</Redirect>'
    return _twiml(gather + reprompt)


@app.post("/voice/handle")
async def voice_handle(request: Request):
    """Twilio posts the transcribed speech here. Read it back, hang up, build."""
    form = await request.form()
    speech = (form.get("SpeechResult") or "").strip()
    caller = form.get("From") or ""

    if not speech:
        retry = (
            '<Gather input="speech" action="/voice/handle" method="POST" '
            'speechTimeout="auto" language="en-US" speechModel="phone_call">'
            + _say("Sorry, I didn't hear a request. Please describe the website now.")
            + "</Gather>"
            + _say("Still nothing — goodbye.") + "<Hangup/>"
        )
        return _twiml(retry)

    job_id = start_build_job(speech, caller)
    to = WHATSAPP_TO or _normalize_e164(caller)
    where = "your WhatsApp" if to else "you"
    body = (
        _say(f"Got it. I'll build: {speech}.")
        + _say(f"This takes a minute or two. I'll send the link to {where} when it's ready. Goodbye!")
        + "<Hangup/>"
    )
    print(f"📞 job {job_id} from {caller}: {speech!r}")
    return _twiml(body)


# ===========================================================================
# Status / debugging
# ===========================================================================

@app.get("/jobs")
async def jobs():
    with _JOBS_LOCK:
        return JSONResponse(sorted(JOBS.values(), key=lambda j: j.get("at", 0), reverse=True))


@app.get("/", response_class=HTMLResponse)
async def home():
    cfg = storage.load_config()
    def chip(ok, label):
        color = "#16a34a" if ok else "#dc2626"
        mark = "✓" if ok else "✗"
        return f'<span style="display:inline-block;margin:3px;padding:4px 10px;border-radius:20px;background:{color};color:#fff;font-size:13px">{mark} {label}</span>'
    rows = ""
    with _JOBS_LOCK:
        for j in sorted(JOBS.values(), key=lambda x: x.get("at", 0), reverse=True)[:25]:
            link = f'<a href="{j["url"]}" target="_blank">{j["url"]}</a>' if j.get("url") else "—"
            rows += (
                f'<tr><td>{j.get("status","")}</td>'
                f'<td>{html.escape((j.get("request") or "")[:70])}</td>'
                f'<td>{link}</td>'
                f'<td style="color:#b91c1c">{html.escape(j.get("error") or "")}</td></tr>'
            )
    return f"""<!doctype html><meta charset=utf-8>
<title>Voice → Website agent</title>
<body style="font-family:system-ui;max-width:820px;margin:40px auto;padding:0 16px;color:#111">
<h1>📞 Voice → Website → WhatsApp</h1>
<p>Dial your Twilio number, say what to build, get the live link on WhatsApp.</p>
<h3>Readiness</h3>
<div>
{chip(bool(cfg.get('gemini_api_key')), 'Gemini key')}
{chip(bool(cfg.get('vercel_token')), 'Vercel token')}
{chip(bool(TWILIO_SID and TWILIO_AUTH), 'Twilio creds')}
{chip(bool(WHATSAPP_TO), 'WhatsApp recipient')}
</div>
<p style="color:#555;font-size:14px">Set Gemini &amp; Vercel in the builder's ⚙ Settings.
Set Twilio &amp; WhatsApp in <code>.env</code>. Point your Twilio number's Voice webhook to
<code>/voice/incoming</code> (POST).</p>
<h3>Recent jobs</h3>
<table border=1 cellpadding=6 style="border-collapse:collapse;width:100%;font-size:14px">
<tr><th>Status</th><th>Request</th><th>Link</th><th>Error</th></tr>
{rows or '<tr><td colspan=4 style="color:#888">No calls yet.</td></tr>'}
</table>
</body>"""


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("VOICE_AGENT_PORT", "8001"))
    print(f"🎙️  Voice agent on http://127.0.0.1:{port}  (expose with ngrok; webhook = /voice/incoming)")
    uvicorn.run("voice_agent:app", host="0.0.0.0", port=port, reload=False)
