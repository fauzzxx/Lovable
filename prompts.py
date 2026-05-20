"""
prompts.py
----------
System prompts that turn a natural-language request (+ optional reference
image) into a runnable web app, plus the parser that turns the model's
delimited output back into a {path: content} file map.

Output contract the model MUST follow
======================================
The model replies with ONE OR MORE file blocks and nothing else:

    <<<FILE: index.html>>>
    ...file contents...
    <<<ENDFILE>>>
    <<<FILE: backend/server.py>>>
    ...file contents...
    <<<ENDFILE>>>

After the file blocks it may add a single short notes block:

    <<<NOTES>>>
    one or two sentences about what was built / what env vars are needed
    <<<ENDNOTES>>>

This delimiter format is used (instead of JSON) because website code is full
of quotes, braces and newlines that make JSON escaping fragile.
"""

from __future__ import annotations

import re

FILE_OPEN = "<<<FILE:"
FILE_CLOSE = "<<<ENDFILE>>>"
NOTES_OPEN = "<<<NOTES>>>"
NOTES_CLOSE = "<<<ENDNOTES>>>"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_FORMAT_RULES = f"""
OUTPUT FORMAT — follow EXACTLY, output nothing outside these blocks:

For every file, emit:
{FILE_OPEN} <relative/path> >>>
<full file contents>
{FILE_CLOSE}

Optionally, finish with ONE short notes block:
{NOTES_OPEN}
1-2 sentences: what you built and which API keys / env vars the user must set.
{NOTES_CLOSE}

Hard rules:
- Do NOT wrap files in markdown code fences (no ``` ).
- Do NOT add commentary, headings, or prose outside the blocks.
- Always output the COMPLETE contents of every file (no "// unchanged" or "...").
- The very first file MUST be `index.html`.
- Use a relative path for every file (e.g. `index.html`, `backend/server.py`).
"""

_FRONTEND_RULES = """
FRONTEND requirements:
- `index.html` must be a single, self-contained file: HTML + <style> + <script>
  inline. No external build step. You MAY load libraries from a CDN
  (cdnjs.cloudflare.com / jsdelivr / unpkg) via <script>/<link> tags.
- Modern, clean, responsive design. Sensible spacing, readable typography,
  works on mobile and desktop.
- If a reference image is provided, match its layout, color palette, spacing
  and overall vibe as closely as you reasonably can.
- All interactivity should work. No dead buttons.
"""

_BACKEND_RULES = """
BACKEND requirements (only when the app needs server logic, data persistence,
external APIs, auth, websockets, file handling, etc.):
- Use Python + FastAPI. Put it in `backend/server.py`.
- CRITICAL: the backend MUST serve the frontend itself, so the whole app runs
  from one process. Add this to `backend/server.py`:
      from fastapi.responses import HTMLResponse
      from pathlib import Path
      @app.get("/", response_class=HTMLResponse)
      def _index():
          # index.html sits one directory up from backend/
          p = Path(__file__).resolve().parent.parent / "index.html"
          return HTMLResponse(p.read_text(encoding="utf-8"))
  The frontend's fetch()/WebSocket calls should therefore use RELATIVE paths
  (e.g. fetch("/api/items"), not http://localhost:8000/api/items) so they work
  no matter which port the server runs on.
- Enable permissive CORS during development.
- Read every secret (API keys, tokens) from environment variables via
  os.getenv(...). NEVER hardcode secrets. List each required env var clearly
  in your NOTES block so the user knows what to provide.
- Provide a `requirements.txt` listing every third-party package you import
  (fastapi and uvicorn[standard] at minimum). Pin nothing unless needed.
- Provide a `.env.example` listing every required env var with empty values
  and a short comment each.
- If the app is purely static (no server logic needed), DO NOT create a
  backend — just emit `index.html`.
"""

SYSTEM_GENERATE = f"""You are an expert full-stack web engineer that builds complete, \
runnable web applications from a short description and an optional reference image. \
You write production-quality HTML/CSS/JS frontends and, when needed, Python FastAPI \
backends.

{_FRONTEND_RULES}
{_BACKEND_RULES}
{_FORMAT_RULES}
"""

SYSTEM_REFINE = f"""You are an expert full-stack web engineer. You are iterating on an \
existing web app. You will be given the current files and a change request. Apply the \
requested change while keeping everything else working.

{_FRONTEND_RULES}
{_BACKEND_RULES}

IMPORTANT for refinement:
- Re-output the COMPLETE, updated contents of EVERY file in the project, even files
  you did not change. Never use placeholders like "// unchanged".
- Keep the same file paths unless the change explicitly requires renaming.
- If you add a new dependency, update requirements.txt. If you add a new secret,
  update .env.example and mention it in NOTES.

{_FORMAT_RULES}
"""


# ---------------------------------------------------------------------------
# User-message builders
# ---------------------------------------------------------------------------

def build_generate_user_prompt(description: str, has_image: bool) -> str:
    img_line = (
        "A reference image is attached — use it to guide the visual design.\n\n"
        if has_image
        else ""
    )
    return (
        f"{img_line}"
        f"Build a web app described as follows:\n\n"
        f"\"\"\"\n{description.strip()}\n\"\"\"\n\n"
        f"Produce all files now, following the OUTPUT FORMAT exactly."
    )


def build_refine_user_prompt(files: dict[str, str], change_request: str, has_image: bool) -> str:
    img_line = (
        "A new reference image is attached — use it to guide the change.\n\n"
        if has_image
        else ""
    )
    current = render_files_for_prompt(files)
    return (
        f"{img_line}"
        f"Here are the CURRENT project files:\n\n"
        f"{current}\n\n"
        f"Change request:\n\"\"\"\n{change_request.strip()}\n\"\"\"\n\n"
        f"Re-output the COMPLETE set of files (every file, full contents) with the "
        f"change applied, following the OUTPUT FORMAT exactly."
    )


def render_files_for_prompt(files: dict[str, str]) -> str:
    """Render the current files using the same delimiter format for context."""
    chunks = []
    for path, content in files.items():
        chunks.append(f"{FILE_OPEN} {path} >>>\n{content}\n{FILE_CLOSE}")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Matches:  <<<FILE: some/path >>>\n  ... \n<<<ENDFILE>>>
_FILE_RE = re.compile(
    r"<<<FILE:\s*(?P<path>.+?)\s*>>>\r?\n(?P<body>.*?)(?:\r?\n)?<<<ENDFILE>>>",
    re.DOTALL,
)
_NOTES_RE = re.compile(r"<<<NOTES>>>\r?\n(?P<body>.*?)(?:\r?\n)?<<<ENDNOTES>>>", re.DOTALL)

# Defensive: strip an accidental ``` fence the model may wrap a file body in.
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(?P<inner>.*?)\n?```\s*$", re.DOTALL)


class ParseError(Exception):
    pass


def _clean_path(raw: str) -> str:
    p = raw.strip().strip('"').strip("'").replace("\\", "/")
    p = p.lstrip("/")
    # block path traversal
    parts = [seg for seg in p.split("/") if seg not in ("", ".", "..")]
    return "/".join(parts)


def _strip_fence(body: str) -> str:
    m = _FENCE_RE.match(body)
    if m:
        return m.group("inner")
    return body


def parse_files(text: str) -> tuple[dict[str, str], str]:
    """
    Parse the model output into ({path: content}, notes).
    Raises ParseError if no files could be found.
    """
    files: dict[str, str] = {}
    for m in _FILE_RE.finditer(text):
        path = _clean_path(m.group("path"))
        if not path:
            continue
        body = _strip_fence(m.group("body"))
        files[path] = body

    if not files:
        # Fallback: maybe the model returned a single bare HTML doc.
        salvaged = _salvage_single_html(text)
        if salvaged:
            files["index.html"] = salvaged
        else:
            raise ParseError(
                "Could not find any file blocks in the model's response. "
                "The model may not have followed the output format."
            )

    notes_m = _NOTES_RE.search(text)
    notes = notes_m.group("body").strip() if notes_m else ""
    return files, notes


def _salvage_single_html(text: str) -> str | None:
    """If the model ignored the format and returned raw HTML, rescue it."""
    fence = _FENCE_RE.match(text.strip())
    candidate = fence.group("inner") if fence else text
    low = candidate.lower()
    if "<html" in low or "<!doctype html" in low:
        return candidate.strip()
    return None
