"""
deploy.py
---------
One-click deployment of a generated project to Vercel, using the Vercel REST
API and a personal access token (created by the user at
https://vercel.com/account/tokens and pasted into Settings).

Two kinds of projects are handled:

* STATIC  — only a frontend (index.html + assets). Deployed as-is; Vercel
            serves it as a static site. Works perfectly.

* PYTHON  — has a FastAPI backend. We adapt it into Vercel's Python serverless
            layout: an `api/index.py` shim that imports the generated FastAPI
            `app`, a root `requirements.txt`, and a `vercel.json` that routes
            every request to the ASGI function (which also serves index.html).

Important Vercel serverless limitations we detect and warn about:
  - WebSockets are NOT supported on Vercel serverless functions.
  - The filesystem is ephemeral and per-invocation — SQLite files and any
    in-memory state do NOT persist. Use an external DB for real persistence.
  - Functions are short-lived; long-running background workers won't run.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field

import httpx

VERCEL_API = "https://api.vercel.com"

# entry-point filenames we look for, in priority order (mirrors runner.py)
_ENTRY_CANDIDATES = [
    "backend/server.py", "server.py",
    "backend/main.py", "main.py",
    "backend/app.py", "app.py",
]
_APP_VAR_RE = re.compile(r"^\s*(\w+)\s*=\s*FastAPI\s*\(", re.MULTILINE)


class VercelError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def slugify_name(name: str) -> str:
    """Vercel project name: lowercase, alnum + hyphens, <=100, no leading/trailing -."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = "app"
    if s[0].isdigit():
        s = "app-" + s
    return s[:100].strip("-") or "app"


# ---------------------------------------------------------------------------
# Project adaptation
# ---------------------------------------------------------------------------

@dataclass
class Prepared:
    files: dict[str, str]           # path -> text content (to be base64'd at send time)
    kind: str                       # 'static' | 'python'
    warnings: list[str] = field(default_factory=list)
    needs_env: bool = False


def _find_entry(files: dict[str, str]) -> str | None:
    for cand in _ENTRY_CANDIDATES:
        if cand in files:
            return cand
    for path, content in files.items():
        if path.endswith(".py") and "FastAPI(" in content:
            return path
    return None


def _app_var(content: str) -> str:
    m = _APP_VAR_RE.search(content or "")
    return m.group(1) if m else "app"


def _module_name(entry_path: str) -> str:
    return entry_path.rsplit("/", 1)[-1][:-3]  # strip dir + ".py"


def _merge_requirements(files: dict[str, str]) -> str:
    """Collect all requirements*.txt, ensure fastapi present, drop uvicorn (not
    needed on Vercel) to keep the build slim."""
    reqs: list[str] = []
    seen: set[str] = set()
    for path, content in files.items():
        if path.rsplit("/", 1)[-1] == "requirements.txt":
            for line in content.splitlines():
                ln = line.strip()
                if not ln or ln.startswith("#"):
                    continue
                base = re.split(r"[<>=!~ \[]", ln, 1)[0].lower()
                if base in ("uvicorn", "gunicorn"):
                    continue  # Vercel provides the server
                if base not in seen:
                    seen.add(base)
                    reqs.append(ln)
    if "fastapi" not in seen:
        reqs.insert(0, "fastapi")
    return "\n".join(reqs) + "\n"


def prepare_files(files: dict[str, str]) -> Prepared:
    """Turn the project's current files into a Vercel-deployable file set."""
    entry = _find_entry(files)

    if entry is None:
        # Pure static site — deploy frontend files as-is.
        prep = Prepared(files=dict(files), kind="static")
        return prep

    # ---- FastAPI backend: build serverless layout -------------------------
    warnings: list[str] = []
    out: dict[str, str] = {}

    # keep all original project files (frontend + backend sources)
    for path, content in files.items():
        # we'll write our own root requirements.txt below
        if path == "requirements.txt":
            continue
        out[path] = content

    module = _module_name(entry)
    entry_dir = entry.rsplit("/", 1)[0] if "/" in entry else ""
    app_var = _app_var(files[entry])

    # api/index.py shim — Vercel detects the ASGI `app` here and serves it.
    rel_to_root = "Path(__file__).resolve().parent.parent"
    backend_path_line = (
        f'sys.path.insert(0, str({rel_to_root} / "{entry_dir}"))\n'
        if entry_dir
        else f"sys.path.insert(0, str({rel_to_root}))\n"
    )
    shim = (
        "# Auto-generated by AI Website Builder for Vercel serverless deploy.\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, str({rel_to_root}))\n"
        f"{backend_path_line}"
        f"from {module} import {app_var} as app  # noqa: E402,F401\n"
    )
    out["api/index.py"] = shim

    # root requirements.txt for the Python build
    out["requirements.txt"] = _merge_requirements(files)

    # vercel.json — route everything to the ASGI function (which serves "/")
    out["vercel.json"] = (
        "{\n"
        '  "version": 2,\n'
        '  "rewrites": [\n'
        '    { "source": "/(.*)", "destination": "/api/index" }\n'
        "  ]\n"
        "}\n"
    )

    # ---- warnings: detect things Vercel serverless can't do ----------------
    joined = "\n".join(files.values()).lower()
    if "websocket" in joined or "wss://" in joined or "ws://" in joined:
        warnings.append(
            "This app uses WebSockets, which Vercel serverless functions do NOT "
            "support. Real-time features won't work on Vercel — host it on Render, "
            "Railway, or Fly.io instead for full functionality."
        )
    if "sqlite" in joined or re.search(r"\.db\b", joined):
        warnings.append(
            "This app uses SQLite. Vercel's filesystem is ephemeral, so the "
            "database resets on every request. Use an external database "
            "(e.g. Vercel Postgres, Neon, Supabase) for real persistence."
        )
    needs_env = any(
        p.rsplit("/", 1)[-1] == ".env.example" for p in files
    ) or "os.getenv" in joined or "os.environ" in joined
    if needs_env:
        warnings.append(
            "This backend reads environment variables. Add them under "
            "'Backend keys' BEFORE deploying so they're set on Vercel too."
        )

    return Prepared(files=out, kind="python", warnings=warnings, needs_env=needs_env)


# ---------------------------------------------------------------------------
# Vercel REST calls
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _team_q(team_id: str | None) -> dict:
    return {"teamId": team_id} if team_id else {}


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail = ""
    try:
        detail = (resp.json().get("error") or {}).get("message", "")
    except Exception:
        detail = resp.text[:300]
    if resp.status_code in (401, 403):
        raise VercelError(
            "Vercel rejected your token (401/403). Create a token at "
            "vercel.com/account/tokens and paste it in Settings. " + detail,
            resp.status_code,
        )
    raise VercelError(f"Vercel API error {resp.status_code}: {detail}", resp.status_code)


def ensure_project_and_env(
    token: str, name: str, env: dict[str, str], team_id: str | None = None
) -> None:
    """Create the project (ignore if it exists) and upsert env vars so they're
    available to the deployment we create next."""
    q = _team_q(team_id)
    with httpx.Client(timeout=60.0) as client:
        # create project (idempotent: 409 means it already exists)
        r = client.post(
            f"{VERCEL_API}/v11/projects",
            params=q,
            headers=_headers(token),
            json={"name": name},
        )
        if r.status_code not in (200, 201, 409):
            _raise_for(r)

        for key, value in (env or {}).items():
            if not str(key).strip():
                continue
            client.post(
                f"{VERCEL_API}/v10/projects/{name}/env",
                params={**q, "upsert": "true"},
                headers=_headers(token),
                json={
                    "key": str(key),
                    "value": str(value),
                    "type": "encrypted",
                    "target": ["production", "preview", "development"],
                },
            )  # best-effort; don't fail the whole deploy on one env var


def create_deployment(
    token: str,
    name: str,
    files: dict[str, str],
    *,
    production: bool,
    team_id: str | None = None,
) -> dict:
    file_payload = [
        {
            "file": path,
            "data": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
        }
        for path, content in files.items()
    ]
    body = {
        "name": name,
        "files": file_payload,
        "projectSettings": {"framework": None},
    }
    if production:
        body["target"] = "production"

    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            f"{VERCEL_API}/v13/deployments",
            params=_team_q(team_id),
            headers=_headers(token),
            json=body,
        )
    _raise_for(r)
    data = r.json()
    return {
        "id": data.get("id"),
        "url": ("https://" + data["url"]) if data.get("url") else None,
        "state": data.get("readyState") or data.get("status") or "QUEUED",
        "inspector": data.get("inspectorUrl"),
    }


def get_deployment(token: str, dep_id: str, team_id: str | None = None) -> dict:
    with httpx.Client(timeout=30.0) as client:
        r = client.get(
            f"{VERCEL_API}/v13/deployments/{dep_id}",
            params=_team_q(team_id),
            headers=_headers(token),
        )
    _raise_for(r)
    data = r.json()
    return {
        "id": data.get("id") or dep_id,
        "url": ("https://" + data["url"]) if data.get("url") else None,
        "state": data.get("readyState") or data.get("status") or "QUEUED",
        "inspector": data.get("inspectorUrl"),
        "error": (data.get("error") or {}).get("message")
        if isinstance(data.get("error"), dict)
        else None,
    }
