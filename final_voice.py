"""
final_voice.py — Call Weaver, get a deployed website + WhatsApp link.
=====================================================================

Tap "Call", say what site you want (push-to-talk), it asks a couple of quick
follow-ups, then BUILDS the site (Claude, via llm.py), DEPLOYS it to Vercel
(deploy.py), shows the live link on the call screen AND texts it to your
WhatsApp via Twilio.

Voice uses Deepgram (REST, not the streaming socket):
  • listening  → you record a turn, it's sent to Deepgram's pre-recorded
                 transcription (/v1/listen).
  • speaking   → Deepgram Aura voice (/v1/speak).
Conversation brain = Gemini (REST). Build = Claude. Deploy = Vercel. All reuse
your saved Weaver settings + .env.

Run:
    pip install fastapi "uvicorn[standard]" httpx python-dotenv
    # .env needs: DEEPGRAM_API_KEY, GEMINI_API_KEY,
    #             TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, WHATSAPP_TO
    python final_voice.py            # http://localhost:8060  (Chrome/Edge, allow mic)
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import deploy
import llm
import prompts
import storage

# Load env FIRST, then read everything from it (self-contained — no other agent).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
    # load_dotenv(Path(__file__).resolve().parent / "examples" / "voice" / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
DG_TTS_MODEL = os.getenv("DEEPGRAM_TTS_MODEL", "aura-asteria-en")
DG_STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-2")

# Twilio WhatsApp — final_voice's own config, read straight from .env.
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886").strip()
WHATSAPP_TO = os.getenv("WHATSAPP_TO", "").strip()

SYSTEM_PROMPT = (
    "You are Weaver's friendly website-building intake agent on a phone call. "
    "Reply in 1-2 short, natural spoken sentences. No markdown, no lists. Find out "
    "what website the caller wants. Ask at most TWO short follow-ups to capture the "
    "site's purpose/name and its look/colors. Keep it warm and quick. As SOON as you "
    "have enough to build a one-page site, reply with EXACTLY one line and nothing "
    "else, starting with 'BUILD:' followed by a single vivid sentence describing the "
    "site to build (type, name, the sections it needs, and color/style). Do not say "
    "'BUILD:' until you are ready to build."
)
GREETING = "Hi! I'm Weaver. What kind of website would you like me to build for you today?"

storage.init_db()

app = FastAPI(title="Weaver Voice → Build → Deploy → WhatsApp")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
async def root():
    p = BASE_DIR / "final_voice.html"
    if not p.exists():
        return HTMLResponse("<h1>final_voice.html is missing next to final_voice.py</h1>", 500)
    return FileResponse(p)


@app.get("/health")
async def health():
    cfg = storage.load_config()
    return {
        "deepgram": bool(DEEPGRAM_API_KEY),
        "gemini": bool(GEMINI_API_KEY),
        "anthropic": bool(cfg.get("anthropic_api_key")),
        "vercel": bool(cfg.get("vercel_token")),
        "whatsapp": bool(TWILIO_SID and TWILIO_AUTH and WHATSAPP_TO),
        "whatsapp_to": WHATSAPP_TO,
    }


# ---------------------------------------------------------------------------
# Listening — Deepgram pre-recorded transcription (REST)
# ---------------------------------------------------------------------------
@app.post("/api/transcribe")
async def transcribe(request: Request):
    if not DEEPGRAM_API_KEY:
        return JSONResponse({"error": "DEEPGRAM_API_KEY is not set."}, 500)
    audio = await request.body()
    if not audio:
        return {"transcript": ""}
    ctype = request.headers.get("content-type") or "audio/webm"
    url = f"https://api.deepgram.com/v1/listen?model={DG_STT_MODEL}&smart_format=true&punctuate=true"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": ctype},
                content=audio,
            )
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"Network error: {e}"}, 502)
    if r.status_code != 200:
        return JSONResponse({"error": f"Deepgram {r.status_code}: {r.text[:200]}"}, 502)
    d = r.json()
    try:
        t = d["results"]["channels"][0]["alternatives"][0]["transcript"]
    except Exception:
        t = ""
    return {"transcript": (t or "").strip()}


# ---------------------------------------------------------------------------
# Speaking — Deepgram Aura (REST)
# ---------------------------------------------------------------------------
@app.post("/api/tts")
async def tts(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not DEEPGRAM_API_KEY:
        return JSONResponse({"error": "DEEPGRAM_API_KEY is not set."}, 500)
    if not text:
        return JSONResponse({"error": "No text."}, 400)
    url = f"https://api.deepgram.com/v1/speak?model={DG_TTS_MODEL}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
                json={"text": text},
            )
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"Network error: {e}"}, 502)
    if r.status_code != 200:
        return JSONResponse({"error": f"Deepgram TTS {r.status_code}: {r.text[:200]}"}, 502)
    return Response(content=r.content, media_type=r.headers.get("content-type", "audio/mpeg"))


# ---------------------------------------------------------------------------
# Conversation — Gemini (REST) + BUILD detection
# ---------------------------------------------------------------------------
def split_build(reply: str) -> tuple[str, str | None]:
    if not reply:
        return ("Sorry, I didn't catch that.", None)
    m = re.search(r"BUILD:\s*(.+)", reply, re.IGNORECASE | re.DOTALL)
    if m:
        spec = m.group(1).strip().strip('"')
        return ("Perfect — I've got what I need. Building and deploying your site now, one moment.", spec)
    return (reply.replace("*", "").replace("#", "").strip(), None)


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    user = (body.get("text") or "").strip()
    history = body.get("history") or []
    if not GEMINI_API_KEY:
        return JSONResponse({"error": "GEMINI_API_KEY is not set."}, 500)
    if not user:
        return JSONResponse({"error": "Say something first."}, 400)
    convo = "\n".join(f"{m.get('role', 'user').upper()}: {m.get('text', '')}" for m in history[-10:])
    prompt = (convo + "\n" if convo else "") + f"USER: {user}\nASSISTANT:"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 200},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url, headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}, json=payload
            )
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"Network error: {e}"}, 502)
    if r.status_code != 200:
        return JSONResponse({"error": f"Gemini {r.status_code}: {r.text[:200]}"}, 502)
    data = r.json()
    try:
        raw = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]).strip()
    except Exception:
        raw = ""
    spoken, spec = split_build(raw)
    return {"reply": spoken, "build_spec": spec}


# ---------------------------------------------------------------------------
# Build (Claude) + Deploy (Vercel)
# ---------------------------------------------------------------------------
def _name_from_spec(spec: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]", " ", spec).split()
    return (" ".join(words[:5]) or "Voice site").strip()[:60]


def _ensure_index(files: dict[str, str]) -> dict[str, str]:
    if not any(p.split("/")[-1] == "index.html" for p in files):
        files["index.html"] = (
            "<!doctype html><meta charset='utf-8'><body style='font-family:sans-serif;padding:2rem'>"
            "<h2>No index.html was produced.</h2></body>"
        )
    return files


def build_and_deploy(spec: str) -> dict:
    cfg = storage.load_config()
    api_key = cfg.get("anthropic_api_key", "") or ""
    model = cfg.get("anthropic_model", "claude-sonnet-4-6")
    if not api_key:
        raise RuntimeError("No Anthropic key saved — add it in the Weaver builder's Settings, then try again.")

    user_prompt = prompts.build_generate_user_prompt(spec, False)
    result = llm.generate_text(
        api_key=api_key, model=model, prompt=user_prompt,
        system_instruction=prompts.SYSTEM_GENERATE,
        temperature=float(cfg.get("temperature", 0.6)),
        max_output_tokens=int(cfg.get("max_output_tokens", 16000)),
    )
    files, notes = prompts.parse_files(result.text)
    files = _ensure_index(files)

    name = _name_from_spec(spec)
    project = storage.create_project(name, spec, files, notes, result.model)
    pid = project["id"]
    out = {"project_id": pid, "name": name, "local_url": f"/site/{pid}/", "vercel_url": None, "deploy_error": None}

    token = cfg.get("vercel_token", "") or ""
    team_id = cfg.get("vercel_team_id", "") or None
    if token:
        try:
            prep = deploy.prepare_files(files)
            dname = deploy.slugify_name(name)
            env = storage.get_project_env(pid)
            if prep.kind == "python" and env:
                deploy.ensure_project_and_env(token, dname, env, team_id)
            dep = deploy.create_deployment(token, dname, prep.files, production=True, team_id=team_id)
            deploy.disable_protection(token, dep.get("vercel_project_id") or dname, team_id)
            out["vercel_url"] = dep.get("url")
        except Exception as e:  # noqa: BLE001
            out["deploy_error"] = f"{type(e).__name__}: {e}"
    else:
        out["deploy_error"] = "No Vercel token saved — showing the local preview link instead."
    return out


def send_whatsapp_link(name: str, url: str | None) -> str:
    """Text the live link to WHATSAPP_TO via Twilio's REST API (no twilio package)."""
    if not url:
        return "Skipped WhatsApp — no public Vercel URL yet (add a Vercel token to deploy)."
    if not (TWILIO_SID and TWILIO_AUTH and WHATSAPP_TO):
        return "WhatsApp not configured — set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and WHATSAPP_TO in .env."
    to = WHATSAPP_TO if WHATSAPP_TO.startswith("whatsapp:") else f"whatsapp:{WHATSAPP_TO}"
    frm = WHATSAPP_FROM if WHATSAPP_FROM.startswith("whatsapp:") else f"whatsapp:{WHATSAPP_FROM}"
    body = f"✅ Your website is live!\n\n“{name}”\n{url}\n\nBuilt from your Weaver call."
    try:
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_AUTH),
            data={"From": frm, "To": to, "Body": body},
            timeout=30.0,
        )
        if r.status_code in (200, 201):
            return f"Link sent to {WHATSAPP_TO} on WhatsApp."
        return f"WhatsApp failed (Twilio {r.status_code}): {r.json().get('message', r.text[:160])}"
    except Exception as e:  # noqa: BLE001
        return f"WhatsApp failed: {e}"


@app.post("/api/build")
async def build(request: Request):
    body = await request.json()
    spec = (body.get("spec") or "").strip()
    if not spec:
        return JSONResponse({"error": "Missing build spec."}, 400)
    try:
        result = await asyncio.to_thread(build_and_deploy, spec)
    except llm.LLMError as e:
        return JSONResponse({"error": e.message}, 502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 500)
    result["whatsapp"] = await asyncio.to_thread(send_whatsapp_link, result["name"], result.get("vercel_url"))
    return result


# ---------------------------------------------------------------------------
# Local preview of a generated site
# ---------------------------------------------------------------------------
_MIME = {
    ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
    ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".ico": "image/x-icon", ".txt": "text/plain",
}


@app.get("/site/{project_id}")
def site_root(project_id: str):
    return _serve_site(project_id, "index.html")


@app.get("/site/{project_id}/{path:path}")
def site_path(project_id: str, path: str):
    return _serve_site(project_id, path or "index.html")


def _serve_site(project_id: str, rel: str):
    pdir = storage.project_dir(project_id).resolve()
    if not pdir.exists():
        return HTMLResponse("<h2>Site not found.</h2>", 404)
    target = (pdir / (rel or "index.html")).resolve()
    if not str(target).startswith(str(pdir)):
        return HTMLResponse("<h2>Invalid path.</h2>", 400)
    if target.is_dir():
        target = target / "index.html"
    if not target.exists():
        target = pdir / "index.html"
        if not target.exists():
            return HTMLResponse("<h2>No index.html in this site.</h2>", 404)
    return FileResponse(str(target), media_type=_MIME.get(target.suffix.lower(), "application/octet-stream"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("final_voice:app", host="127.0.0.1", port=int(os.getenv("PORT", "8060")), reload=False)
