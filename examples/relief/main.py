"""
Disaster Relief Coordination Platform
Single-file FastAPI backend: SQLite + Gemini + Open-Meteo + WebSocket notifications.
"""
import os
import sqlite3
import json
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
import httpx
import jwt
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

try:
    import google.generativeai as genai
except Exception:
    genai = None

load_dotenv()

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "relief.db"
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALG = "HS256"
JWT_EXP_HOURS = 24 * 7
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@relief.org")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

if GEMINI_API_KEY and genai is not None:
    genai.configure(api_key=GEMINI_API_KEY)


# ---------- Database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS volunteers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        email TEXT NOT NULL,
        skills TEXT,
        location TEXT NOT NULL,
        availability TEXT DEFAULT 'available',
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS emergencies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT NOT NULL,
        severity TEXT NOT NULL,
        location TEXT NOT NULL,
        description TEXT NOT NULL,
        reporter_name TEXT NOT NULL,
        reporter_phone TEXT NOT NULL,
        people_affected INTEGER DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending',
        assigned_to INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(assigned_to) REFERENCES volunteers(id)
    );

    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'info',
        created_at TEXT NOT NULL
    );
    """)
    # seed admin
    c.execute("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,))
    if not c.fetchone():
        ph = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
        c.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, 'admin', ?)",
            ("Admin", ADMIN_EMAIL, ph, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()
    conn.close()


# ---------- Auth helpers ----------
def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_pw(pw: str, ph: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), ph.encode())
    except Exception:
        return False


def make_token(user: dict) -> str:
    payload = {
        "sub": str(user["id"]),
        "email": user["email"],
        "role": user["role"],
        "name": user["name"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXP_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        return None


def current_user(request: Request) -> Optional[dict]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return decode_token(auth.split(" ", 1)[1])
    return None


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Authentication required")
    return u


def require_admin(request: Request) -> dict:
    u = require_user(request)
    if u.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return u


# ---------- WebSocket Manager ----------
class WSManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, payload: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.disconnect(d)


ws_manager = WSManager()


async def push_notification(message: str, kind: str = "info"):
    conn = db()
    conn.execute(
        "INSERT INTO notifications (message, kind, created_at) VALUES (?, ?, ?)",
        (message, kind, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    await ws_manager.broadcast({"type": "notification", "message": message, "kind": kind})


# ---------- Schemas ----------
class RegisterIn(BaseModel):
    name: str = Field(min_length=2)
    email: EmailStr
    password: str = Field(min_length=6)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class VolunteerIn(BaseModel):
    name: str
    phone: str
    email: EmailStr
    skills: str = ""
    location: str
    availability: str = "available"


class EmergencyIn(BaseModel):
    type: str
    severity: str
    location: str
    description: str
    reporter_name: str
    reporter_phone: str
    people_affected: int = 1


class StatusUpdate(BaseModel):
    status: str
    assigned_to: Optional[int] = None


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []


# ---------- App ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Disaster Relief Coordination Platform", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------- Routes ----------
@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/api/auth/register")
async def register(payload: RegisterIn):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (payload.email,))
    if c.fetchone():
        conn.close()
        raise HTTPException(400, "Email already registered")
    c.execute(
        "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, 'user', ?)",
        (payload.name, payload.email, hash_pw(payload.password), datetime.now(timezone.utc).isoformat()),
    )
    user_id = c.lastrowid
    conn.commit()
    user = {"id": user_id, "email": payload.email, "name": payload.name, "role": "user"}
    token = make_token(user)
    conn.close()
    return {"token": token, "user": user}


@app.post("/api/auth/login")
async def login(payload: LoginIn):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email = ?", (payload.email,))
    row = c.fetchone()
    conn.close()
    if not row or not verify_pw(payload.password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    user = {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}
    return {"token": make_token(user), "user": user}


@app.get("/api/auth/me")
async def me(request: Request):
    return require_user(request)


# Volunteers
@app.post("/api/volunteers")
async def create_volunteer(payload: VolunteerIn, request: Request):
    user = current_user(request)
    conn = db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO volunteers (user_id, name, phone, email, skills, location, availability, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(user["sub"]) if user else None,
            payload.name, payload.phone, payload.email, payload.skills,
            payload.location, payload.availability,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    vid = c.lastrowid
    conn.commit()
    conn.close()
    await push_notification(f"New volunteer registered: {payload.name} ({payload.location})", "success")
    return {"id": vid, "ok": True}


@app.get("/api/volunteers")
async def list_volunteers():
    conn = db()
    rows = conn.execute("SELECT * FROM volunteers ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Emergencies
@app.post("/api/emergencies")
async def create_emergency(payload: EmergencyIn, request: Request):
    user = current_user(request)
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO emergencies
        (user_id, type, severity, location, description, reporter_name, reporter_phone, people_affected,
         status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (
            int(user["sub"]) if user else None,
            payload.type, payload.severity, payload.location, payload.description,
            payload.reporter_name, payload.reporter_phone, payload.people_affected,
            now, now,
        ),
    )
    eid = c.lastrowid
    conn.commit()
    conn.close()
    await push_notification(
        f"🚨 {payload.severity.upper()} {payload.type} reported at {payload.location}",
        "danger" if payload.severity in ("high", "critical") else "warning",
    )
    return {"id": eid, "ok": True}


@app.get("/api/emergencies")
async def list_emergencies(status_filter: Optional[str] = None):
    conn = db()
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM emergencies WHERE status = ? ORDER BY created_at DESC",
            (status_filter,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM emergencies ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.patch("/api/emergencies/{eid}")
async def update_emergency(eid: int, payload: StatusUpdate, request: Request):
    require_admin(request)
    conn = db()
    c = conn.cursor()
    c.execute(
        "UPDATE emergencies SET status = ?, assigned_to = ?, updated_at = ? WHERE id = ?",
        (payload.status, payload.assigned_to, datetime.now(timezone.utc).isoformat(), eid),
    )
    conn.commit()
    conn.close()
    await push_notification(f"Emergency #{eid} updated to {payload.status}", "info")
    return {"ok": True}


@app.delete("/api/emergencies/{eid}")
async def delete_emergency(eid: int, request: Request):
    require_admin(request)
    conn = db()
    conn.execute("DELETE FROM emergencies WHERE id = ?", (eid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# Stats
@app.get("/api/stats")
async def stats():
    conn = db()
    c = conn.cursor()
    total_emerg = c.execute("SELECT COUNT(*) AS n FROM emergencies").fetchone()["n"]
    pending = c.execute("SELECT COUNT(*) AS n FROM emergencies WHERE status='pending'").fetchone()["n"]
    resolved = c.execute("SELECT COUNT(*) AS n FROM emergencies WHERE status='resolved'").fetchone()["n"]
    in_progress = c.execute("SELECT COUNT(*) AS n FROM emergencies WHERE status='in_progress'").fetchone()["n"]
    volunteers = c.execute("SELECT COUNT(*) AS n FROM volunteers").fetchone()["n"]
    available = c.execute("SELECT COUNT(*) AS n FROM volunteers WHERE availability='available'").fetchone()["n"]
    people = c.execute("SELECT COALESCE(SUM(people_affected),0) AS n FROM emergencies").fetchone()["n"]
    by_type_rows = c.execute(
        "SELECT type, COUNT(*) AS n FROM emergencies GROUP BY type"
    ).fetchall()
    by_severity_rows = c.execute(
        "SELECT severity, COUNT(*) AS n FROM emergencies GROUP BY severity"
    ).fetchall()
    conn.close()
    return {
        "emergencies": total_emerg,
        "pending": pending,
        "in_progress": in_progress,
        "resolved": resolved,
        "volunteers": volunteers,
        "available_volunteers": available,
        "people_affected": people,
        "by_type": {r["type"]: r["n"] for r in by_type_rows},
        "by_severity": {r["severity"]: r["n"] for r in by_severity_rows},
    }


# Weather (Open-Meteo, no key required) + geocoding
@app.get("/api/weather")
async def weather(city: str):
    if not city.strip():
        raise HTTPException(400, "City is required")
    async with httpx.AsyncClient(timeout=10) as client:
        geo = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1},
        )
        gd = geo.json()
        if not gd.get("results"):
            raise HTTPException(404, "Location not found")
        loc = gd["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        w = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code,precipitation",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
                "timezone": "auto",
            },
        )
        data = w.json()
    return {
        "location": f"{loc.get('name')}, {loc.get('country', '')}".strip(", "),
        "latitude": lat, "longitude": lon,
        "current": data.get("current", {}),
        "daily": data.get("daily", {}),
    }


# AI chat (Gemini)
SYSTEM_PROMPT = """You are ReliefBot, a calm, expert AI assistant for a Disaster Relief Coordination Platform.
You help citizens, volunteers, and responders during floods, earthquakes, cyclones, fires, and other disasters.

Your job:
- Give clear, actionable safety guidance (short steps, plain language).
- Triage urgency: if life is in danger, tell them to call local emergency services immediately and report via the platform.
- Help users use the platform: registering as a volunteer, reporting emergencies, checking weather.
- Provide first-aid, evacuation, shelter, water-safety, and communication tips appropriate to the disaster type.
- Never invent specific phone numbers or addresses you don't know — direct users to local authorities.
- Keep replies under 180 words unless the user explicitly asks for more detail. Use bullet points where helpful.
"""


@app.post("/api/chat")
async def chat(payload: ChatIn):
    msg = payload.message.strip()
    if not msg:
        raise HTTPException(400, "Message required")
    if not GEMINI_API_KEY or genai is None:
        return {"reply": _fallback_reply(msg)}
    try:
        model = genai.GenerativeModel("gemini-1.5-flash", system_instruction=SYSTEM_PROMPT)
        history = []
        for m in payload.history[-8:]:
            role = "user" if m.get("role") == "user" else "model"
            history.append({"role": role, "parts": [m.get("content", "")]})
        chat = model.start_chat(history=history)
        resp = await asyncio.to_thread(chat.send_message, msg)
        return {"reply": resp.text}
    except Exception as e:
        return {"reply": _fallback_reply(msg), "warning": str(e)}


def _fallback_reply(msg: str) -> str:
    m = msg.lower()
    if any(k in m for k in ("flood", "water rising", "drown")):
        return (
            "If you are in a flood:\n"
            "• Move to higher ground immediately — do not walk or drive through moving water.\n"
            "• Turn off electricity at the main switch only if safe to do so.\n"
            "• Drink only bottled/boiled water.\n"
            "• Report your location on this platform via 'Report Emergency'."
        )
    if "earthquake" in m:
        return (
            "Earthquake safety:\n"
            "• DROP, COVER, HOLD ON — get under a sturdy table.\n"
            "• Stay away from windows and heavy furniture.\n"
            "• After shaking stops, check for injuries and gas leaks.\n"
            "• Expect aftershocks; evacuate damaged buildings."
        )
    if "fire" in m:
        return (
            "Fire safety:\n"
            "• Get out, stay out, and call local emergency services.\n"
            "• Crawl low under smoke; feel doors before opening.\n"
            "• If clothes catch fire: stop, drop, and roll.\n"
            "• Report location on this platform."
        )
    return (
        "I'm ReliefBot. Tell me what's happening (flood, earthquake, fire, injury, shelter need, etc.) "
        "and your location, and I'll guide you. For life-threatening emergencies, contact local emergency "
        "services immediately and submit a report under 'Report Emergency'."
    )


# Notifications
@app.get("/api/notifications")
async def get_notifications(limit: int = 20):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        await ws.send_json({"type": "hello", "message": "Connected to Relief Platform"})
        while True:
            # Keep-alive: just await pings/messages
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        ws_manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
