"""
storage.py
----------
Persistence for the builder.

* SQLite (`builder.db`) holds project metadata and the full version history.
  Every generate / refine creates a new immutable `version` row whose `files`
  column is the JSON {path: content} snapshot. This gives us history + rollback.

* The CURRENT files of each project are ALSO written to disk under
  `projects/<project_id>/` so they can be served as a live preview and run as a
  real backend.

Settings (the Gemini key, model, generated-app env vars) live in `config.json`
next to this file — not in git, not in the DB.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "builder.db"
PROJECTS_DIR = BASE_DIR / "projects"
CONFIG_PATH = BASE_DIR / "config.json"

PROJECTS_DIR.mkdir(exist_ok=True)


def _now() -> float:
    return time.time()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  TEXT NOT NULL,
                version_num INTEGER NOT NULL,
                kind        TEXT NOT NULL,           -- 'generate' | 'refine' | 'rollback'
                prompt      TEXT,
                notes       TEXT,
                files       TEXT NOT NULL,           -- JSON {path: content}
                model       TEXT,
                created_at  REAL NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_versions_project ON versions(project_id)"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "app"


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def _write_files_to_disk(project_id: str, files: dict[str, str]) -> None:
    """
    Overwrite the project's working directory with the given files.
    Preserves runtime-only dirs (.venv) and the project's .env between writes.
    """
    pdir = project_dir(project_id)
    pdir.mkdir(parents=True, exist_ok=True)

    keep = {".venv", ".env", ".runner.log"}
    for child in pdir.iterdir():
        if child.name in keep:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass

    for rel, content in files.items():
        # path is already cleaned by the parser, but be defensive
        safe = "/".join(seg for seg in rel.replace("\\", "/").split("/") if seg not in ("", ".", ".."))
        if not safe:
            continue
        dest = pdir / safe
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Projects + versions
# ---------------------------------------------------------------------------

def create_project(name: str, prompt: str, files: dict[str, str], notes: str, model: str) -> dict:
    pid = f"{_slug(name)}-{uuid.uuid4().hex[:8]}"
    ts = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (pid, name.strip() or "Untitled app", ts, ts),
        )
        conn.execute(
            """INSERT INTO versions
               (project_id, version_num, kind, prompt, notes, files, model, created_at)
               VALUES (?, 1, 'generate', ?, ?, ?, ?, ?)""",
            (pid, prompt, notes, json.dumps(files), model, ts),
        )
    _write_files_to_disk(pid, files)
    return get_project(pid)


def add_version(
    project_id: str,
    *,
    kind: str,
    prompt: str,
    files: dict[str, str],
    notes: str,
    model: str,
) -> dict:
    ts = _now()
    with _conn() as conn:
        row = conn.execute(
            "SELECT MAX(version_num) AS n FROM versions WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        next_num = (row["n"] or 0) + 1
        conn.execute(
            """INSERT INTO versions
               (project_id, version_num, kind, prompt, notes, files, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, next_num, kind, prompt, notes, json.dumps(files), model, ts),
        )
        conn.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?", (ts, project_id)
        )
    _write_files_to_disk(project_id, files)
    return get_project(project_id)


def rollback_to_version(project_id: str, version_num: int) -> dict:
    """Make an old version current by copying it forward as a new version."""
    target = get_version(project_id, version_num)
    if not target:
        raise KeyError(f"version {version_num} not found")
    files = json.loads(target["files"])
    return add_version(
        project_id,
        kind="rollback",
        prompt=f"Rolled back to version {version_num}",
        files=files,
        notes=target.get("notes") or "",
        model=target.get("model") or "",
    )


def list_projects() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            v = conn.execute(
                "SELECT MAX(version_num) AS n FROM versions WHERE project_id = ?",
                (r["id"],),
            ).fetchone()
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "version_count": v["n"] or 0,
                }
            )
        return out


def get_project(project_id: str) -> dict | None:
    with _conn() as conn:
        p = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not p:
            return None
        latest = conn.execute(
            """SELECT * FROM versions WHERE project_id = ?
               ORDER BY version_num DESC LIMIT 1""",
            (project_id,),
        ).fetchone()
        versions = conn.execute(
            """SELECT version_num, kind, prompt, notes, model, created_at
               FROM versions WHERE project_id = ? ORDER BY version_num DESC""",
            (project_id,),
        ).fetchall()
    files = json.loads(latest["files"]) if latest else {}
    return {
        "id": p["id"],
        "name": p["name"],
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
        "current_version": latest["version_num"] if latest else 0,
        "files": files,
        "notes": latest["notes"] if latest else "",
        "model": latest["model"] if latest else "",
        "has_backend": any(
            f.endswith("server.py") or f.endswith("app.py") or f.endswith("main.py")
            for f in files
        ),
        "versions": [dict(v) for v in versions],
    }


def get_version(project_id: str, version_num: int) -> dict | None:
    with _conn() as conn:
        v = conn.execute(
            "SELECT * FROM versions WHERE project_id = ? AND version_num = ?",
            (project_id, version_num),
        ).fetchone()
    return dict(v) if v else None


def get_current_files(project_id: str) -> dict[str, str]:
    p = get_project(project_id)
    return p["files"] if p else {}


def delete_project(project_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM versions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    shutil.rmtree(project_dir(project_id), ignore_errors=True)


# ---------------------------------------------------------------------------
# Settings / config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "gemini_api_key": "",
    "model": "gemini-3.1-flash-lite",
    "temperature": 0.6,
    "max_output_tokens": 32768,
    # per-project environment variables for the GENERATED apps' backends:
    #   { "<project_id>": { "TWILIO_SID": "...", ... } }
    "project_env": {},
}


def load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    cfg.setdefault("project_env", {})
    return cfg


def save_config(updates: dict) -> dict:
    cfg = load_config()
    for k in ("gemini_api_key", "model", "temperature", "max_output_tokens"):
        if k in updates and updates[k] is not None:
            cfg[k] = updates[k]
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def get_project_env(project_id: str) -> dict[str, str]:
    return load_config().get("project_env", {}).get(project_id, {})


def set_project_env(project_id: str, env: dict[str, str]) -> dict[str, str]:
    cfg = load_config()
    cfg.setdefault("project_env", {})[project_id] = {
        str(k): str(v) for k, v in env.items() if str(k).strip()
    }
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    # also drop a .env into the project dir so the backend picks it up
    _write_project_dotenv(project_id, cfg["project_env"][project_id])
    return cfg["project_env"][project_id]


def _write_project_dotenv(project_id: str, env: dict[str, str]) -> None:
    pdir = project_dir(project_id)
    if not pdir.exists():
        return
    lines = [f"{k}={v}" for k, v in env.items()]
    (pdir / ".env").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
