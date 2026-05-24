"""
final_voice.py — Call Weaver in the browser, then get a deployed website.
==========================================================================

Tap "Call", say what site you want, it asks a couple of quick follow-ups, then
BUILDS the site (Claude, via llm.py) and DEPLOYS it to Vercel (deploy.py) and
shows the live link on the call screen.

The LISTENING + SPEAKING happen in the browser using the built-in Web Speech API
(Chrome/Edge) — no Deepgram, no API key for the mic, no streaming socket. The
server only runs the conversation brain (Gemini REST), the build (Claude), and
the deploy (Vercel).

Run:
    pip install fastapi "uvicorn[standard]" httpx python-dotenv
    # set GEMINI_API_KEY in .env (next to this file) — used for the conversation
    python final_voice.py            # http://localhost:8060   (use Chrome or Edge)

Keys:
    GEMINI_API_KEY   the conversation brain during the call
    (Anthropic key + Vercel token are read from Weaver's saved config so build +
     deploy reuse what you already set in the builder's Settings.)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import deploy
import llm
import prompts
import storage

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
    # also reuse the keys already configured for the example voice agent, if present
    load_dotenv(Path(__file__).resolve().parent / "examples" / "voice" / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

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

storage.init_db()  # share the builder's projects/versions tables

app = FastAPI(title="Weaver Voice → Build → Deploy")
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
        "gemini": bool(GEMINI_API_KEY),
        "anthropic": bool(cfg.get("anthropic_api_key")),
        "vercel": bool(cfg.get("vercel_token")),
        "greeting": GREETING,
    }


# ---------------------------------------------------------------------------
# Conversation (Gemini REST) + BUILD detection
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
        return JSONResponse({"error": "GEMINI_API_KEY is not set on the server."}, 500)
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
        "project_id": pid, "name": name,
        "local_url": f"/site/{pid}/", "vercel_url": None, "deploy_error": None,
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
    import asyncio
    body = await request.json()
    spec = (body.get("spec") or "").strip()
    if not spec:
        return JSONResponse({"error": "Missing build spec."}, 400)
    try:
        return await asyncio.to_thread(build_and_deploy, spec)
    except llm.LLMError as e:
        return JSONResponse({"error": e.message}, 502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 500)


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
