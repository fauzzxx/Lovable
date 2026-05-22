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

**Deploy to Vercel** — Click **▲ Deploy** in the preview bar to publish the project
to Vercel in one click. See section 6 below.

**Switch / open projects** — Use the dropdown at the top to open a past project or
start a new one.

---

## 6. One-click deploy to Vercel

**Setup (once):** Create a Vercel account, then make a token at
<https://vercel.com/account/tokens> and paste it into **⚙ Settings → Vercel token**.
(Optional: a **Team ID** if you want to deploy under a Vercel team instead of your
personal account.) You create the account and token yourself — the builder only uses
the token you provide.

**Deploy:** Open a project, click **▲ Deploy**, choose a target, and go:

- **Preview** — a unique throwaway URL for testing.
- **Production** — your stable `your-app.vercel.app` URL.

The builder uploads the files via the Vercel API and shows the live URL plus a link to
the build logs. It polls until the build is **READY** (or shows the error).

**What gets deployed**

- **Static apps** (frontend only) deploy as-is and just work.
- **Backend apps** are automatically adapted to Vercel's Python serverless layout: an
  `api/index.py` shim that loads your FastAPI `app`, a root `requirements.txt`, and a
  `vercel.json` that routes all requests to the function. Any **Backend keys** you
  saved are pushed to Vercel as encrypted environment variables before the deploy, so
  add them *first*.

**⚠ Vercel serverless limitations** (the builder warns you about these in the deploy
dialog when it detects them):

- **WebSockets don't work** on Vercel serverless functions — real-time apps (like a
  live transcript / call dashboard) won't function there. Use **Render**, **Railway**,
  or **Fly.io** for those (the **⤓ Export** zip runs anywhere).
- **SQLite / local files don't persist** — the filesystem is ephemeral and resets per
  request. Use an external database (Vercel Postgres, Neon, Supabase) for real data.
- Functions are short-lived, so background workers / long-running loops won't run.

For request/response apps (CRUD with an external API, form handlers, etc.) Vercel works
great. For stateful or real-time apps, deploy elsewhere.

---

## 7. How generated apps are structured

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

## 8. Security note (please read)

This tool **runs AI-generated Python code on your computer** when you click *Run
backend*. That is what makes full-stack testing possible, but it also means you
should **review the code** (Code tab) before running anything you don't trust, just
as you would with any code from the internet. Generated apps run in their own
virtual-env but still have normal access to your machine.

Your Gemini key is stored locally in `config.json`; per-project secrets live in each
project's `.env`. Neither is committed to git (see `.gitignore`).

---

## 9. Project layout (the builder itself)

| File            | Purpose                                                            |
|-----------------|--------------------------------------------------------------------|
| `server.py`     | FastAPI app: generate/refine/run/preview/versions/export/deploy    |
| `llm.py`        | Gemini REST wrapper (text + image input, friendly errors)          |
| `prompts.py`    | System prompts + the multi-file output parser                      |
| `storage.py`    | SQLite projects/versions, settings, per-project env + deploy info  |
| `runner.py`     | Per-project venv + uvicorn subprocess manager (start/stop/logs)    |
| `deploy.py`     | Vercel adaptation (static/serverless) + Vercel REST API client     |
| `voice_agent.py`| Inbound-call agent (one-shot): phone → build → deploy → WhatsApp    |
| `voice_realtime.py`| Full-duplex agent: real conversation (Deepgram+Gemini+ElevenLabs)|
| `index.html`    | The single-page builder UI                                         |
| `_smoketest.py` | Optional: `python _smoketest.py` — tests parser/storage/API        |
| `_runnertest.py`| Optional: `python _runnertest.py` — end-to-end backend run test    |
| `_deploytest.py`| Optional: `python _deploytest.py` — tests Vercel adaptation logic  |
| `projects/`     | Generated apps live here, one folder each (auto-created)           |

---

## 10. Troubleshooting

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

**"Vercel rejected your token (401/403)"** — Create a fresh token at
vercel.com/account/tokens and paste it into Settings. If you deploy under a team, also
set the **Team ID**.

**Deployed backend errors / 500s** — Most often a missing env var (add it in *Backend
keys* and redeploy) or an unsupported feature (WebSockets/SQLite — see section 6). Open
the deploy dialog's **live logs ↗** link to see Vercel's build/runtime output.

---

## 11. Phone → website → WhatsApp (inbound call agent)

`voice_agent.py` lets you **call a phone number, speak what you want, and receive the
live Vercel link on WhatsApp** a minute or two later. It reuses the same engine
(Gemini generation + Vercel deploy) and reads your Gemini key & Vercel token from the
builder's Settings, so you only configure those once.

**Call flow:** dial → "describe the website you'd like me to build" → you speak → it
reads your request back, says it'll text you, and hangs up → in the background it
generates the site, deploys it to Vercel **production**, and **WhatsApps you the link**.

### Setup

1. **Create a Twilio account** (you do this yourself) at <https://console.twilio.com>
   and buy a phone number with **Voice** capability.
2. **Set credentials** in `.env` (copy from `.env.example`):
   ```
   TWILIO_ACCOUNT_SID=ACxxxxxxxx
   TWILIO_AUTH_TOKEN=xxxxxxxx
   WHATSAPP_TO=+1XXXXXXXXXX        # your WhatsApp number (E.164)
   ```
3. **Enable WhatsApp.** Easiest for testing is the **Twilio WhatsApp Sandbox**
   (Console → Messaging → Try it out → WhatsApp). From your phone, send
   `join <your-sandbox-code>` to **+1 415 523 8886** so Twilio is allowed to message
   you. (Leave `TWILIO_WHATSAPP_FROM` as the default sandbox number.) For production,
   set up an approved WhatsApp sender and put its number in `TWILIO_WHATSAPP_FROM`.
4. **Make sure Gemini + Vercel are set** in the builder's ⚙ Settings (run `server.py`
   once if you haven't), since the agent reuses them.

### Run + expose

The agent must be reachable by Twilio, so run it locally and tunnel it:

```bash
python voice_agent.py            # serves on http://localhost:8001
# in another terminal:
ngrok http 8001                  # gives you https://<something>.ngrok-free.app
```

Then in the Twilio Console, open your phone number → **Voice → A call comes in** →
**Webhook**, set it to:

```
https://<your-tunnel>/voice/incoming      (HTTP POST)
```

Open `http://localhost:8001/` to see a readiness dashboard (green ticks for Gemini,
Vercel, Twilio, WhatsApp) and a live list of call jobs and their resulting links.

### Now just call your Twilio number

Say something like *"build a one-page portfolio site for a wedding photographer with a
gallery and contact section."* Hang up, and the Vercel link arrives on WhatsApp.

### Notes & limits

- Builds that need **WebSockets or SQLite won't fully work on Vercel** (see section 6).
  The agent still deploys them, but real-time/stateful features won't run there.
- Twilio Sandbox WhatsApp sessions expire after 24h of inactivity — re-send the `join`
  code if messages stop arriving.
- Standard Twilio call/number/WhatsApp charges apply to your account.

---

## 12. Full-duplex / conversational call agent (`voice_realtime.py`)

This is the upgraded, *natural-sounding* version. Instead of the one-shot "speak then
wait" flow, it has a **real-time, two-way conversation** with a human-sounding voice,
and you can interrupt it (barge-in). It chats to gather your requirements — asking up
to two quick clarifying questions — then builds + deploys + WhatsApps the link, exactly
like `voice_agent.py`.

**Stack:** Twilio Media Streams (live audio) · Deepgram (streaming speech-to-text) ·
Gemini (the brain) · ElevenLabs (the voice).

### Extra keys it needs

On top of everything `voice_agent.py` uses, add to `.env`:
```
DEEPGRAM_API_KEY=...      # console.deepgram.com  (free credit to start)
ELEVEN_API_KEY=...        # elevenlabs.io → Profile → API key
ELEVEN_VOICE_ID=...       # optional; pick a voice in the ElevenLabs library
```

### Run + expose (same idea, different port)

```bash
python voice_realtime.py       # serves on :8002
ngrok http 8002                # then set the Twilio Voice webhook to /voice/incoming
```

Open `http://localhost:8002/` for a readiness dashboard (Gemini, Vercel, Twilio,
Deepgram, ElevenLabs, WhatsApp). Use **either** `voice_agent.py` **or**
`voice_realtime.py` — point your Twilio number's webhook at whichever one you're running.

### What to say on the call

It's a conversation, so just talk naturally. Good openers:

- *"I want a landing page for a coffee shop called Daybreak — menu, hours, and a map."*
- *"Build me a portfolio site for a photographer, dark theme, with a gallery and a contact form."*
- *"A simple one-page site for my dog-walking business with pricing and a booking button."*

It'll ask a follow-up or two — answer naturally, e.g.:

- *Agent:* "Any particular colours or vibe?" → *You:* "Warm, earthy tones, friendly and modern."
- *Agent:* "Which pages do you need?" → *You:* "Just home, about, and contact."

When it says it has enough, it confirms, tells you it's building, and hangs up — the
Vercel link lands on WhatsApp a minute or two later. Tips: speak in clear, complete
sentences, and pause when you're done talking so it knows it's your turn finished.

### Notes & limits

- Needs a stable public **wss** endpoint. Free ngrok works for testing but can be flaky
  for sustained audio; for anything serious, host it (Render/Railway/Fly). It **cannot**
  run on Vercel (that's serverless — no WebSockets).
- Three metered APIs bill per call now: Twilio minutes + Deepgram minutes + ElevenLabs
  characters.
- Latency matters — Deepgram → Gemini → ElevenLabs round-trips should stay snappy; a
  fast Gemini model (e.g. `gemini-2.5-flash-lite`) helps.
