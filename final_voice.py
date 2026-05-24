"""
final_voice.py — "Call Weaver, get a deployed website."
==========================================================

A browser voice "call" that builds and deploys a site for you, hands-free:

    1. You tap the phone button and the agent greets you (Deepgram Aura TTS).
    2. You say what kind of website you want; it asks a couple of short
       follow-ups (Deepgram streaming ASR  →  Gemini does the talking).
    3. The moment it has enough, it BUILDS the site with the same engine the
       Weaver builder uses (Claude via llm.py) and DEPLOYS it to Vercel
       (deploy.py) — then shows the live link right on the page.

This is a standalone app on its own port; it reuses the builder's modules
(llm, prompts, storage, deploy) so it shares your Weaver settings (Anthropic
key + Vercel token come from the saved config).

Run:
    pip install fastapi "uvicorn[standard]" httpx websockets python-dotenv
    # set DEEPGRAM_API_KEY and GEMINI_API_KEY in the environment (or .env)
    python final_voice.py            # http://localhost:8060  (Chrome/Edge, allow mic)

Keys:
    DEEPGRAM_API_KEY   speech-to-text (ASR) + Aura text-to-speech
    GEMINI_API_KEY     the conversation brain during the call
    (Anthropic key + Vercel token are read from Weaver's saved config so the
     build + deploy steps reuse what you already set in the builder's Settings.)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import deploy
import llm
import prompts
import storage

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DG_TTS_MODEL = os.getenv("DEEPGRAM_TTS_MODEL", "aura-asteria-en")

GREETING = "Hi! I'm Weaver. What kind of website would you like me to build for you today?"

# The intake brain. It chats briefly, then emits a single BUILD: line when ready.
INTAKE_SYSTEM = (
    "You are Weaver's friendly voice intake agent. You are on a phone-style call, so "
    "reply in 1-2 short, natural spoken sentences — no markdown, no lists. Your job is "
    "to find out what website the caller wants. Ask at most TWO short follow-up questions "
    "to capture: (a) the site's purpose and a name, and (b) the look/style or colors. "
    "Keep it quick and warm. As SOON as you have enough to build a good one-page site, "
    "reply with EXACTLY one line and nothing else, starting with 'BUILD:' followed by a "
    "single vivid sentence describing the website to build (type, name, the sections it "
    "needs, and the color/style). Do not output 'BUILD:' until you are ready to build."
)

storage.init_db()  # make sure the projects/versions tables exist (shared with the builder)

app = FastAPI(title="Weaver Voice → Build → Deploy")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    p = BASE_DIR / "final_voice.html"
    if not p.exists():
        return HTMLResponse("<h1>final_voice.html is missing next to final_voice.py</h1>", 500)
    return HTMLResponse(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Conversation (Gemini) + BUILD detection
# ---------------------------------------------------------------------------
def split_build(reply: str) -> tuple[str, str | None]:
    """If the model is ready, its reply is a single 'BUILD: <spec>' line.
    Return (spoken_text, build_spec_or_None)."""
    if not reply:
        return ("Sorry, I didn't catch that.", None)
    m = re.search(r"BUILD:\s*(.+)", reply, re.IGNORECASE | re.DOTALL)
    if m:
        spec = m.group(1).strip().strip('"')
        spoken = "Perfect — I've got what I need. Building and deploying your site now, give me a moment."
        return (spoken, spec)
    return (reply.strip(), None)


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    user = (body.get("text") or "").strip()
    history = body.get("history") or []
    if not GEMINI_API_KEY:
        return JSONResponse({"error": "GEMINI_API_KEY is not set on the server."}, 500)
    if not user:
        return JSONResponse({"error": "Say something first."}, 400)

    convo = "\n".join(f"{m.get('role', 'user').upper()}: {m.get('text', '')}" for m in history[-10:])
    prompt = (convo + "\n" if convo else "") + f"USER: {user}\nASSISTANT:"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": INTAKE_SYSTEM}]},
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 200},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url,
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json=payload,
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
    spoken = spoken.replace("*", "").replace("#", "")
    return {"reply": spoken, "build_spec": spec}


# ---------------------------------------------------------------------------
# Text-to-speech (Deepgram Aura)
# ---------------------------------------------------------------------------
@app.post("/api/tts")
async def tts(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not DEEPGRAM_API_KEY:
        return JSONResponse({"error": "DEEPGRAM_API_KEY is not set on the server."}, 500)
    if not text:
        return JSONResponse({"error": "No text to speak."}, 400)
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
        return JSONResponse({"error": f"Deepgram {r.status_code}: {r.text[:200]}"}, 502)
    return Response(content=r.content, media_type=r.headers.get("content-type", "audio/mpeg"))


# ---------------------------------------------------------------------------
# Build (Claude) + Deploy (Vercel) — reuses the Weaver builder modules
# ---------------------------------------------------------------------------
def _name_from_spec(spec: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]", " ", spec).split()
    return (" ".join(words[:5]) or "Voice site").strip()[:60]


def _ensure_index(files: dict[str, str]) -> dict[str, str]:
    if not any(p.split("/")[-1] == "index.html" for p in files):
        files["index.html"] = (
            "<!doctype html><meta charset='utf-8'>"
            "<body style='font-family:sans-serif;padding:2rem'>"
            "<h2>No index.html was produced.</h2></body>"
        )
    return files


def build_and_deploy(spec: str) -> dict:
    """Generate the site with Claude, store it, deploy to Vercel if a token is set.
    Always returns a usable link: the public Vercel URL when possible, plus a local
    preview URL served by this app. Raises RuntimeError with a friendly message."""
    cfg = storage.load_config()
    api_key = cfg.get("anthropic_api_key", "") or ""
    model = cfg.get("anthropic_model", "claude-sonnet-4-6")
    if not api_key:
        raise RuntimeError(
            "No Anthropic key saved. Open the Weaver builder → Settings and add your "
            "Claude key, then try the call again."
        )

    user_prompt = prompts.build_generate_user_prompt(spec, False)
    result = llm.generate_text(
        api_key=api_key,
        model=model,
        prompt=user_prompt,
        system_instruction=prompts.SYSTEM_GENERATE,
        temperature=float(cfg.get("temperature", 0.6)),
        max_output_tokens=int(cfg.get("max_output_tokens", 16000)),
    )
    files, notes = prompts.parse_files(result.text)
    files = _ensure_index(files)

    name = _name_from_spec(spec)
    project = storage.create_project(name, spec, files, notes, result.model)
    pid = project["id"]

    out = {
        "project_id": pid,
        "name": name,
        "notes": notes,
        "local_url": f"/site/{pid}/",
        "vercel_url": None,
        "deploy_error": None,
    }

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
            out["vercel_url"] = dep.get("url")
        except Exception as e:  # noqa: BLE001
            out["deploy_error"] = f"{type(e).__name__}: {e}"
    else:
        out["deploy_error"] = "No Vercel token saved — showing the local preview link instead."
    return out


@app.post("/api/build")
async def build(request: Request):
    body = await request.json()
    spec = (body.get("spec") or "").strip()
    if not spec:
        return JSONResponse({"error": "Missing build spec."}, 400)
    try:
        # build + deploy is blocking (network + LLM); run it off the event loop
        result = await asyncio.to_thread(build_and_deploy, spec)
    except llm.LLMError as e:
        return JSONResponse({"error": e.message}, 502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 500)
    return result


# ---------------------------------------------------------------------------
# Local preview of a generated site (fallback link, always works)
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


def _serve_site(project_id: str, rel: str) -> Response:
    pdir = storage.project_dir(project_id).resolve()
    if not pdir.exists():
        return HTMLResponse("<h2>Site not found.</h2>", 404)
    rel = rel or "index.html"
    target = (pdir / rel).resolve()
    if not str(target).startswith(str(pdir)):
        return HTMLResponse("<h2>Invalid path.</h2>", 400)
    if target.is_dir():
        target = target / "index.html"
    if not target.exists():
        target = pdir / "index.html"
        if not target.exists():
            return HTMLResponse("<h2>No index.html in this site.</h2>", 404)
    return FileResponse(str(target), media_type=_MIME.get(target.suffix.lower(), "application/octet-stream"))


# ---------------------------------------------------------------------------
# Speech-to-text proxy (browser mic → Deepgram streaming ASR → transcripts)
# ---------------------------------------------------------------------------
async def _dg_connect(url: str, headers: dict):
    try:
        return await websockets.connect(url, additional_headers=headers)
    except TypeError:
        return await websockets.connect(url, extra_headers=headers)


@app.websocket("/ws/asr")
async def ws_asr(ws: WebSocket):
    await ws.accept()
    if not DEEPGRAM_API_KEY:
        await ws.send_text(json.dumps({"error": "DEEPGRAM_API_KEY is not set on the server."}))
        await ws.close()
        return
    dg_url = (
        "wss://api.deepgram.com/v1/listen?model=nova-2&interim_results=true"
        "&smart_format=true&punctuate=true&endpointing=300&utterance_end_ms=1000&vad_events=true"
    )
    try:
        dg = await _dg_connect(dg_url, {"Authorization": f"Token {DEEPGRAM_API_KEY}"})
        print("[ws/asr] connected to Deepgram")
    except Exception as e:  # noqa: BLE001
        await ws.send_text(json.dumps({"error": f"Could not connect to Deepgram ({type(e).__name__}: {e})."}))
        await ws.close()
        return

    async def pump_audio():
        try:
            while True:
                data = await ws.receive_bytes()
                await dg.send(data)
        except Exception:
            pass
        finally:
            try:
                await dg.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass

    async def pump_transcripts():
        try:
            async for msg in dg:
                d = json.loads(msg)
                if d.get("type") == "Results" or d.get("channel"):
                    alt = (d.get("channel", {}).get("alternatives") or [{}])[0]
                    t = (alt.get("transcript") or "").strip()
                    if t:
                        await ws.send_text(json.dumps({
                            "transcript": t,
                            "is_final": bool(d.get("is_final")),
                            "speech_final": bool(d.get("speech_final")),
                        }))
                elif d.get("type") == "UtteranceEnd":
                    await ws.send_text(json.dumps({"utterance_end": True}))
        except Exception:
            pass

    async def keepalive():
        try:
            while True:
                await asyncio.sleep(5)
                await dg.send(json.dumps({"type": "KeepAlive"}))
        except Exception:
            pass

    tasks = [
        asyncio.create_task(pump_audio()),
        asyncio.create_task(pump_transcripts()),
        asyncio.create_task(keepalive()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
    finally:
        try:
            await dg.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("final_voice:app", host="127.0.0.1", port=int(os.getenv("PORT", "8060")), reload=False)
