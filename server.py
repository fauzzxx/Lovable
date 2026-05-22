"""
server.py
---------
The AI Website Builder backend (a Lovable-style app builder).

Run it with:
    python -m uvicorn server:app --reload --port 8000
or simply:
    python server.py

Then open http://localhost:8000 in your browser.
"""

from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)

import deploy
import llm
import prompts
import storage
from runner import runner

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"

MIME_BY_EXT = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".txt": "text/plain",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    yield
    runner.stop_all()


app = FastAPI(title="AI Website Builder", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _parse_image(data_url: str | None) -> list[dict]:
    """Accept a 'data:image/...;base64,xxxx' string (or raw base64)."""
    if not data_url:
        return []
    s = data_url.strip()
    if s.startswith("data:"):
        try:
            header, b64 = s.split(",", 1)
        except ValueError:
            return []
        mime = header[5:].split(";")[0] or "image/png"
        return [{"mime_type": mime, "data": b64}]
    return [{"mime_type": "image/png", "data": s}]


def _gen_settings() -> dict:
    cfg = storage.load_config()
    return {
        "api_key": cfg.get("gemini_api_key", ""),
        "model": cfg.get("model") or "gemini-2.5-flash",
        "temperature": float(cfg.get("temperature", 0.6)),
        "max_output_tokens": int(cfg.get("max_output_tokens", 32768)),
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    if not INDEX_HTML.exists():
        return HTMLResponse("<h1>index.html is missing next to server.py</h1>", 500)
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings():
    cfg = storage.load_config()
    key = cfg.get("gemini_api_key", "") or ""
    vtok = cfg.get("vercel_token", "") or ""
    return {
        "has_key": bool(key),
        "key_hint": ("…" + key[-4:]) if len(key) >= 4 else "",
        "model": cfg.get("model", "gemini-2.5-flash"),
        "temperature": cfg.get("temperature", 0.6),
        "max_output_tokens": cfg.get("max_output_tokens", 32768),
        "has_vercel_token": bool(vtok),
        "vercel_token_hint": ("…" + vtok[-4:]) if len(vtok) >= 4 else "",
        "vercel_team_id": cfg.get("vercel_team_id", ""),
    }


@app.post("/api/settings")
async def post_settings(request: Request):
    body = await request.json()
    updates = {}
    # only overwrite the key if a non-empty one is supplied
    if body.get("gemini_api_key"):
        updates["gemini_api_key"] = body["gemini_api_key"].strip()
    if body.get("model"):
        updates["model"] = body["model"].strip()
    if body.get("temperature") is not None:
        try:
            updates["temperature"] = max(0.0, min(2.0, float(body["temperature"])))
        except (TypeError, ValueError):
            pass
    if body.get("max_output_tokens") is not None:
        try:
            updates["max_output_tokens"] = max(1024, int(body["max_output_tokens"]))
        except (TypeError, ValueError):
            pass
    if body.get("vercel_token"):
        updates["vercel_token"] = body["vercel_token"].strip()
    if body.get("vercel_team_id") is not None:
        updates["vercel_team_id"] = body["vercel_team_id"].strip()
    storage.save_config(updates)
    return await get_settings()


@app.get("/api/models")
async def models():
    cfg = storage.load_config()
    return {"models": llm.list_models(cfg.get("gemini_api_key", ""))}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.get("/api/projects")
async def projects_list():
    return {"projects": storage.list_projects()}


@app.get("/api/projects/{project_id}")
async def project_get(project_id: str):
    p = storage.get_project(project_id)
    if not p:
        return _err("Project not found", 404)
    p["env"] = storage.get_project_env(project_id)
    p["run"] = runner.status(project_id)
    p["deploy"] = storage.get_deploy_record(project_id)
    return p


@app.post("/api/generate")
async def generate(request: Request):
    body = await request.json()
    description = (body.get("prompt") or "").strip()
    name = (body.get("name") or "").strip() or _name_from_prompt(description)
    images = _parse_image(body.get("image"))
    if not description:
        return _err("Please describe the website you want to build.")

    s = _gen_settings()
    if not s["api_key"]:
        return _err("No Gemini API key set. Open Settings and add your key.", 401)

    user_prompt = prompts.build_generate_user_prompt(description, bool(images))
    try:
        result = llm.generate_text(
            api_key=s["api_key"],
            model=s["model"],
            prompt=user_prompt,
            system_instruction=prompts.SYSTEM_GENERATE,
            images=images,
            temperature=s["temperature"],
            max_output_tokens=s["max_output_tokens"],
        )
    except llm.LLMError as e:
        return _err(e.message, e.status or 502)

    try:
        files, notes = prompts.parse_files(result.text)
    except prompts.ParseError as e:
        return _err(
            f"{e} (model finish reason: {result.finish_reason}). "
            f"Try again, or raise 'Max output tokens' in Settings if the reply was cut off."
        )

    files = _ensure_index(files)
    project = storage.create_project(name, description, files, notes, result.model)
    project["env"] = storage.get_project_env(project["id"])
    project["run"] = runner.status(project["id"])
    project["truncated"] = result.truncated
    return project


@app.post("/api/projects/{project_id}/refine")
async def refine(project_id: str, request: Request):
    body = await request.json()
    change = (body.get("prompt") or "").strip()
    images = _parse_image(body.get("image"))
    if not change:
        return _err("Describe the change you'd like to make.")

    current = storage.get_current_files(project_id)
    if not current:
        return _err("Project not found", 404)

    s = _gen_settings()
    if not s["api_key"]:
        return _err("No Gemini API key set. Open Settings and add your key.", 401)

    user_prompt = prompts.build_refine_user_prompt(current, change, bool(images))
    try:
        result = llm.generate_text(
            api_key=s["api_key"],
            model=s["model"],
            prompt=user_prompt,
            system_instruction=prompts.SYSTEM_REFINE,
            images=images,
            temperature=s["temperature"],
            max_output_tokens=s["max_output_tokens"],
        )
    except llm.LLMError as e:
        return _err(e.message, e.status or 502)

    try:
        files, notes = prompts.parse_files(result.text)
    except prompts.ParseError as e:
        return _err(f"{e} Try again or simplify the request.")

    files = _ensure_index(files, fallback=current)
    project = storage.add_version(
        project_id, kind="refine", prompt=change, files=files, notes=notes, model=result.model
    )
    project["env"] = storage.get_project_env(project_id)
    project["run"] = runner.status(project_id)
    project["truncated"] = result.truncated
    return project


@app.post("/api/projects/{project_id}/rollback")
async def rollback(project_id: str, request: Request):
    body = await request.json()
    try:
        vnum = int(body.get("version_num"))
    except (TypeError, ValueError):
        return _err("version_num must be an integer.")
    try:
        project = storage.rollback_to_version(project_id, vnum)
    except KeyError:
        return _err("That version does not exist.", 404)
    project["env"] = storage.get_project_env(project_id)
    project["run"] = runner.status(project_id)
    return project


@app.delete("/api/projects/{project_id}")
async def project_delete(project_id: str):
    runner.stop(project_id)
    storage.delete_project(project_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Generated-app env vars (API keys the generated backend needs)
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/env")
async def env_get(project_id: str):
    return {"env": storage.get_project_env(project_id)}


@app.post("/api/projects/{project_id}/env")
async def env_set(project_id: str, request: Request):
    body = await request.json()
    env = body.get("env") or {}
    if not isinstance(env, dict):
        return _err("env must be an object of key/value pairs.")
    saved = storage.set_project_env(project_id, env)
    return {"env": saved}


# ---------------------------------------------------------------------------
# Run / stop generated backend
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/run")
async def run_backend(project_id: str):
    if not storage.get_project(project_id):
        return _err("Project not found", 404)
    return runner.start(project_id)


@app.post("/api/projects/{project_id}/stop")
async def stop_backend(project_id: str):
    return runner.stop(project_id)


@app.get("/api/projects/{project_id}/run-status")
async def run_status(project_id: str):
    return runner.status(project_id)


# ---------------------------------------------------------------------------
# Deploy to Vercel
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/deploy-info")
async def deploy_info(project_id: str):
    """Preview what will be deployed: kind (static/python), warnings, file list,
    and whether a Vercel token is configured — without deploying anything."""
    p = storage.get_project(project_id)
    if not p:
        return _err("Project not found", 404)
    prep = deploy.prepare_files(p["files"])
    cfg = storage.load_config()
    return {
        "kind": prep.kind,
        "warnings": prep.warnings,
        "files": sorted(prep.files.keys()),
        "has_vercel_token": bool(cfg.get("vercel_token")),
        "needs_env": prep.needs_env,
        "env_count": len(storage.get_project_env(project_id)),
        "last_deploy": storage.get_deploy_record(project_id),
        "suggested_name": deploy.slugify_name(p["name"]),
    }


@app.post("/api/projects/{project_id}/deploy")
async def deploy_project(project_id: str, request: Request):
    body = await request.json()
    production = bool(body.get("production"))

    p = storage.get_project(project_id)
    if not p:
        return _err("Project not found", 404)

    cfg = storage.load_config()
    token = cfg.get("vercel_token", "")
    if not token:
        return _err(
            "No Vercel token set. Open Settings and add a token from "
            "vercel.com/account/tokens.",
            401,
        )
    team_id = (cfg.get("vercel_team_id") or "").strip() or None

    prep = deploy.prepare_files(p["files"])
    name = deploy.slugify_name(p["name"])
    env = storage.get_project_env(project_id)

    try:
        # Pre-create project + push env vars so the deployment picks them up.
        if prep.kind == "python" and env:
            deploy.ensure_project_and_env(token, name, env, team_id)
        result = deploy.create_deployment(
            token, name, prep.files, production=production, team_id=team_id
        )
    except deploy.VercelError as e:
        return _err(e.message, e.status or 502)

    import time as _t
    record = {
        "id": result["id"],
        "url": result["url"],
        "state": result["state"],
        "inspector": result.get("inspector"),
        "production": production,
        "kind": prep.kind,
        "at": _t.time(),
    }
    storage.set_deploy_record(project_id, record)
    return {**record, "warnings": prep.warnings}


@app.get("/api/projects/{project_id}/deploy-status")
async def deploy_status(project_id: str, id: str):
    cfg = storage.load_config()
    token = cfg.get("vercel_token", "")
    if not token:
        return _err("No Vercel token set.", 401)
    team_id = (cfg.get("vercel_team_id") or "").strip() or None
    try:
        info = deploy.get_deployment(token, id, team_id)
    except deploy.VercelError as e:
        return _err(e.message, e.status or 502)
    rec = storage.get_deploy_record(project_id)
    if rec.get("id") == info["id"]:
        rec.update({"state": info["state"], "url": info["url"] or rec.get("url")})
        storage.set_deploy_record(project_id, rec)
    return info


# ---------------------------------------------------------------------------
# Static preview of a project's current frontend
# ---------------------------------------------------------------------------

@app.get("/preview/{project_id}")
async def preview_root(project_id: str):
    return _serve_preview(project_id, "index.html")


@app.get("/preview/{project_id}/{path:path}")
async def preview_path(project_id: str, path: str):
    return _serve_preview(project_id, path or "index.html")


def _serve_preview(project_id: str, rel: str) -> Response:
    pdir = storage.project_dir(project_id).resolve()
    if not pdir.exists():
        return HTMLResponse("<h2>Preview not available — project not found.</h2>", 404)
    # default + path-traversal guard
    rel = rel or "index.html"
    target = (pdir / rel).resolve()
    if not str(target).startswith(str(pdir)):
        return HTMLResponse("<h2>Invalid path.</h2>", 400)
    if target.is_dir():
        target = target / "index.html"
    if not target.exists():
        # fall back to index.html for SPA-ish routing
        target = pdir / "index.html"
        if not target.exists():
            return HTMLResponse("<h2>No index.html in this project.</h2>", 404)
    mime = MIME_BY_EXT.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(str(target), media_type=mime)


# ---------------------------------------------------------------------------
# Download project as a zip
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/download")
async def download(project_id: str):
    project = storage.get_project(project_id)
    if not project:
        return _err("Project not found", 404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in project["files"].items():
            zf.writestr(path, content)
        # include an .env.example reminder if backend present
        if project["has_backend"] and ".env.example" not in project["files"]:
            zf.writestr(".env.example", "# Add the env vars your backend reads here\n")
    buf.seek(0)
    safe = project_id + ".zip"
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------

def _ensure_index(files: dict[str, str], fallback: dict | None = None) -> dict:
    if any(p.split("/")[-1] == "index.html" for p in files):
        return files
    if fallback and "index.html" in fallback:
        files["index.html"] = fallback["index.html"]
        return files
    # synthesize a minimal index that explains the situation
    files["index.html"] = (
        "<!doctype html><meta charset='utf-8'>"
        "<body style='font-family:sans-serif;padding:2rem'>"
        "<h2>No index.html was produced.</h2>"
        "<p>Try refining with: \"add an index.html frontend\".</p></body>"
    )
    return files


def _name_from_prompt(prompt: str) -> str:
    words = prompt.split()
    return (" ".join(words[:5]) or "Untitled app")[:60]


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.getenv("BUILDER_PORT", "8000"))
    uvicorn.run("server:app", host="127.0.0.1", port=port, reload=False)
