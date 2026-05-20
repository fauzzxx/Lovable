# AI Website Builder (a Lovable-style app builder)

Type a prompt — optionally with a reference screenshot — and this tool generates a
**complete, runnable web app**: an HTML/CSS/JS frontend plus an optional **Python
(FastAPI) backend**. Preview it live in the browser, **run the backend** to test
real APIs/data, then keep chatting to **refine** it. Every change is versioned, so
you can roll back. Powered by Google **Gemini**.

```
 ┌──────────────┐     prompt + image      ┌─────────────┐   files   ┌──────────────┐
 │  Builder UI  │ ───────────────────────▶│  Gemini API │ ─────────▶│  Your new    │
 │ (index.html) │ ◀───────────────────────│             │           │  web app     │
 └──────────────┘   live preview / code   └─────────────┘           └──────────────┘
        │  ▲                                                               │
        │  └──────────── run backend (venv + uvicorn) ◀────────────────────┘
        ▼
   refine / rollback / export .zip
```

---

## 1. Prerequisites

- **Python 3.10 or newer** — check with `python --version`
- A **Google Gemini API key** — free from <https://aistudio.google.com/apikey>
- Windows, macOS, or Linux

## 2. Install

Open a terminal **in this folder** (`Lovable`) and run:

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> The builder needs only the packages in `requirements.txt`. Each *generated app*
> gets its **own** separate virtual environment automatically when you run it, so
> their dependencies never mix with the builder's.

## 3. Run

```bash
python server.py
```

Then open **<http://localhost:8000>** in your browser.

(You can also run `python -m uvicorn server:app --reload --port 8000`.)

## 4. First-time setup (add your key)

1. Click the **⚙ (Settings)** button, top-right.
2. Paste your **Gemini API key**.
3. Leave **Model** as `gemini-3.1-flash-lite` (or whatever you prefer). If you ever
   get a *"model not found"* error, change it to `gemini-2.5-flash` or
   `gemini-2.5-flash-lite` — see *Troubleshooting*.
4. **Save**.

---

## 5. How to use it

**Generate** — Type a description in the chat box (e.g. *"a to-do app with a FastAPI
backend that stores tasks in SQLite"*). Optionally click 🖼 to attach a screenshot or
mockup so it matches a design. Press **Generate**.

**Preview** — The right pane shows a live preview of the frontend immediately.

**Run the backend** — If the app has a Python backend, click **▶ Run backend**. The
builder creates an isolated virtual-env for that project, installs its
`requirements.txt`, and launches it with uvicorn. When it's live, the preview
switches to the running server so buttons, APIs, and websockets actually work. Click
**■ Stop** when done. Use the **▤ logs** button to watch install/startup output.

**Backend keys** — If the generated backend needs secrets (API keys, tokens), a
**Backend keys** tab appears. Add them as `KEY=value` pairs. They're saved to a local
`.env` for that project and injected when you run it. They never leave your machine.
(The generation's 📌 note tells you which variables are needed.)

**Refine** — Keep chatting: *"make the header sticky"*, *"add a dark mode toggle"*,
*"add a /export endpoint that returns CSV"*. The whole project is regenerated with
your change applied.

**Versions** — The **Versions** tab lists every generation/refinement. Click
**Restore** to roll any version back to current.

**Export** — Click **⤓ Export** to download the project as a `.zip` you can run or
deploy anywhere.

**Switch / open projects** — Use the dropdown at the top to open a past project or
start a new one.

---

## 6. How generated apps are structured

When a backend is needed, the model produces:

```
index.html              # the frontend (self-contained)
backend/server.py        # FastAPI app — also serves index.html at "/"
backend/requirements.txt # the app's own dependencies
.env.example             # the secrets the app expects
```

Because the generated backend serves its own `index.html` and the frontend uses
**relative** API paths (`fetch("/api/...")`), each generated app runs as a single
process on whatever port the builder assigns — exactly like the example you provided.

---

## 7. Security note (please read)

This tool **runs AI-generated Python code on your computer** when you click *Run
backend*. That is what makes full-stack testing possible, but it also means you
should **review the code** (Code tab) before running anything you don't trust, just
as you would with any code from the internet. Generated apps run in their own
virtual-env but still have normal access to your machine.

Your Gemini key is stored locally in `config.json`; per-project secrets live in each
project's `.env`. Neither is committed to git (see `.gitignore`).

---

## 8. Project layout (the builder itself)

| File            | Purpose                                                            |
|-----------------|--------------------------------------------------------------------|
| `server.py`     | FastAPI app: generate/refine/run/preview/versions/export endpoints |
| `llm.py`        | Gemini REST wrapper (text + image input, friendly errors)          |
| `prompts.py`    | System prompts + the multi-file output parser                      |
| `storage.py`    | SQLite projects/versions, settings, per-project env                |
| `runner.py`     | Per-project venv + uvicorn subprocess manager (start/stop/logs)    |
| `index.html`    | The single-page builder UI                                         |
| `_smoketest.py` | Optional: `python _smoketest.py` — tests parser/storage/API        |
| `_runnertest.py`| Optional: `python _runnertest.py` — end-to-end backend run test    |
| `projects/`     | Generated apps live here, one folder each (auto-created)           |

---

## 9. Troubleshooting

**"model not found (404)"** — The model name in Settings doesn't exist for your key.
Switch to `gemini-2.5-flash` or `gemini-2.5-flash-lite` and save.

**"Gemini rejected your API key (401/403)"** — Re-copy the key from
aistudio.google.com/apikey into Settings.

**"rate limit / quota (429)"** — Free-tier limits. Wait a minute and retry, or use a
model with higher quota.

**The reply was cut off / files look incomplete** — Raise **Max output tokens** in
Settings (e.g. 65536) and regenerate, or ask the builder to "finish the file".

**Run backend is slow the first time** — It's building the project's virtual-env and
installing dependencies. Subsequent runs reuse the env and are fast. Watch the ▤ logs.

**Port already in use** — The builder picks a free port per run automatically; if the
builder's own port 8000 is taken, set `BUILDER_PORT` (e.g. `set BUILDER_PORT=8010` on
Windows) before `python server.py`.
