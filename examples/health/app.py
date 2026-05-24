"""
AI-Powered Rural Healthcare Platform
Single FastAPI backend serving symptom checker, medicine lookup,
doctor booking, multilingual assistant, and emergency alerts.
Powered by Google Gemini 2.5 Flash Lite.
"""
import os
import json
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyDUMMY_REPLACE_ME")
GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
BOOKINGS_FILE = DATA_DIR / "bookings.json"
ALERTS_FILE = DATA_DIR / "alerts.json"

# Mock data — pharmacy stock + doctor roster for rural region
MEDICINES = [
    {"name": "Paracetamol 500mg", "stock": 240, "price": 12, "pharmacy": "Jan Aushadhi - Rampur"},
    {"name": "Amoxicillin 250mg", "stock": 80, "price": 45, "pharmacy": "Jan Aushadhi - Rampur"},
    {"name": "ORS Sachet", "stock": 500, "price": 8, "pharmacy": "PHC Rampur"},
    {"name": "Metformin 500mg", "stock": 150, "price": 18, "pharmacy": "Jan Aushadhi - Rampur"},
    {"name": "Cetirizine 10mg", "stock": 320, "price": 10, "pharmacy": "PHC Rampur"},
    {"name": "Azithromycin 500mg", "stock": 0, "price": 65, "pharmacy": "Jan Aushadhi - Rampur"},
    {"name": "Insulin (Regular)", "stock": 12, "price": 180, "pharmacy": "District Hospital"},
    {"name": "Salbutamol Inhaler", "stock": 25, "price": 95, "pharmacy": "District Hospital"},
    {"name": "Iron + Folic Acid", "stock": 600, "price": 5, "pharmacy": "PHC Rampur"},
    {"name": "Antiseptic Solution", "stock": 75, "price": 35, "pharmacy": "PHC Rampur"},
]

DOCTORS = [
    {"id": "d1", "name": "Dr. Anita Sharma", "specialty": "General Physician", "village": "Rampur PHC", "slots": ["Mon 10:00", "Wed 14:00", "Fri 11:00"]},
    {"id": "d2", "name": "Dr. Rakesh Verma", "specialty": "Pediatrician", "village": "District Hospital", "slots": ["Tue 09:00", "Thu 15:00"]},
    {"id": "d3", "name": "Dr. Sunita Devi", "specialty": "Gynecologist", "village": "Rampur PHC", "slots": ["Mon 14:00", "Sat 10:00"]},
    {"id": "d4", "name": "Dr. Manoj Kumar", "specialty": "Cardiologist (Tele)", "village": "Telemedicine", "slots": ["Wed 16:00", "Fri 16:00"]},
    {"id": "d5", "name": "Dr. Priya Singh", "specialty": "Dermatologist (Tele)", "village": "Telemedicine", "slots": ["Tue 11:00", "Thu 11:00"]},
]


app = FastAPI(title="Rural Healthcare AI")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------- Gemini call ----------
class ChatRequest(BaseModel):
    message: str
    language: str = "English"
    mode: str = "assistant"  # "assistant" or "symptom"


SYSTEM_PROMPTS = {
    "assistant": (
        "You are a friendly rural healthcare assistant for villages in India. "
        "Give short, practical, low-cost guidance. Always recommend visiting "
        "the nearest PHC for serious issues. Reply ONLY in {lang}. Keep replies "
        "under 120 words. Use simple words a non-medical villager can understand."
    ),
    "symptom": (
        "You are a careful symptom triage assistant for rural India. "
        "Given the symptoms, respond in {lang} with EXACTLY this structure:\n"
        "1) Likely cause (1 short line)\n"
        "2) Urgency: LOW / MEDIUM / HIGH / EMERGENCY\n"
        "3) Home care (2-3 bullets)\n"
        "4) When to see a doctor (1 line)\n"
        "Be conservative — if anything suggests stroke, heart attack, severe "
        "bleeding, breathing trouble, or unconsciousness, mark EMERGENCY and "
        "say to call 108 immediately. Do NOT diagnose definitively."
    ),
}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    system = SYSTEM_PROMPTS.get(req.mode, SYSTEM_PROMPTS["assistant"]).format(
        lang=req.language
    )
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": req.message}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 400,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                GEMINI_URL,
                params={"key": GEMINI_API_KEY},
                json=payload,
            )
        if r.status_code != 200:
            return JSONResponse(
                {"reply": f"AI service error ({r.status_code}). Using offline fallback.",
                 "offline": True},
                status_code=200,
            )
        data = r.json()
        text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        ).strip()
        if not text:
            text = "Sorry, no reply generated. Please try again."
        return {"reply": text, "offline": False}
    except Exception as e:
        return {"reply": f"Network issue: {e}. Use offline guidance below.",
                "offline": True}


# ---------- Medicine lookup ----------
@app.get("/api/medicines")
def medicines(q: Optional[str] = None):
    if not q:
        return MEDICINES
    ql = q.lower().strip()
    return [m for m in MEDICINES if ql in m["name"].lower()]


# ---------- Doctors + booking ----------
@app.get("/api/doctors")
def doctors():
    return DOCTORS


class Booking(BaseModel):
    doctor_id: str
    patient_name: str
    phone: str
    slot: str
    village: str = ""
    notes: str = ""


@app.post("/api/book")
def book(b: Booking):
    doc = next((d for d in DOCTORS if d["id"] == b.doctor_id), None)
    if not doc:
        raise HTTPException(404, "Doctor not found")
    bookings = _load_json(BOOKINGS_FILE, [])
    record = {
        "id": f"BK{int(time.time())}",
        "doctor": doc["name"],
        "specialty": doc["specialty"],
        "venue": doc["village"],
        "patient": b.patient_name,
        "phone": b.phone,
        "slot": b.slot,
        "village": b.village,
        "notes": b.notes,
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    bookings.append(record)
    _save_json(BOOKINGS_FILE, bookings)
    return {"ok": True, "booking": record}


@app.get("/api/bookings")
def list_bookings():
    return _load_json(BOOKINGS_FILE, [])


# ---------- Emergency ----------
class Emergency(BaseModel):
    name: str
    phone: str
    location: str
    description: str


@app.post("/api/emergency")
def emergency(e: Emergency):
    alerts = _load_json(ALERTS_FILE, [])
    record = {
        "id": f"EM{int(time.time())}",
        "name": e.name,
        "phone": e.phone,
        "location": e.location,
        "description": e.description,
        "ambulance": "108",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "DISPATCHED",
    }
    alerts.append(record)
    _save_json(ALERTS_FILE, alerts)
    # In production this would SMS/call 108 + local ASHA worker
    return {"ok": True, "alert": record,
            "message": "Ambulance 108 notified. Stay calm. Help is on the way."}


# ---------- Serve frontend ----------
@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8010, reload=True)
