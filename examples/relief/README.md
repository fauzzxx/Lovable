# ReliefNet — Disaster Relief Coordination Platform

Single-page, dark, AI-powered platform that connects citizens, volunteers, and NGOs during disasters.
Built with **FastAPI + SQLite + Gemini + WebSockets** and a single-file Tailwind frontend.

## Features

- **Volunteer registration** (with skills, location, availability)
- **Emergency request form** with severity and type
- **Live dashboard** — stats, recent reports, type & severity charts
- **Real-time weather** for any city (Open-Meteo, no key required)
- **AI chatbot (ReliefBot)** powered by Gemini, with safe offline fallback
- **Admin panel** to update status / dispatch / delete reports
- **JWT authentication** (login / register, role-based)
- **Real-time notifications** via WebSocket (toast + bell drawer)
- **Mobile-responsive** dark UI with animations
- **SQLite** auto-initialized on first run

## Quick start

```bash
# 1. (optional) create a virtualenv
python -m venv .venv
.venv\Scripts\activate    # Windows
# source .venv/bin/activate # macOS/Linux

# 2. install
pip install -r requirements.txt

# 3. configure
cp .env.example .env
# edit .env and add your GEMINI_API_KEY (https://aistudio.google.com/apikey)

# 4. run
python main.py
# or: uvicorn main:app --reload --port 8000
```

Open http://localhost:8000

**Default admin login:** `admin@relief.org` / `admin123` (override via `.env`)

## Architecture

```
main.py            FastAPI app (auth, REST, WebSocket, Gemini, weather)
templates/
  index.html      Single-page frontend (Tailwind via CDN, Chart.js, vanilla JS)
relief.db         SQLite (auto-created on first run)
```

## Deployment

The app is a single ASGI process. Deploy on any platform that supports Python:

- **Render / Railway / Fly.io:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Docker:** add a 3-line Dockerfile (`python:3.11-slim`, `pip install -r requirements.txt`, run uvicorn)
- **Behind a reverse proxy:** make sure WebSocket upgrade headers are forwarded (`/ws`)

## Notes

- Without `GEMINI_API_KEY` set, the chatbot falls back to built-in safety guidance.
- Weather uses Open-Meteo + their free geocoding API — no key needed.
- The SQLite file (`relief.db`) is created on first launch and seeded with the admin user.
