"""
voice_realtime.py
------------------
FULL-DUPLEX inbound voice agent. You call, have a natural conversation, and it
gathers what you want, then builds + deploys the site and WhatsApps you the link.

Stack (mirrors the proven outbound-agent pattern):
  • Twilio Media Streams  → bidirectional μ-law 8k audio over a WebSocket
  • Deepgram streaming ASR → always-on transcription + VAD (enables barge-in)
  • Gemini                → the conversation brain (via llm.py REST wrapper)
  • Deepgram Aura TTS      → realistic, low-latency voice (free-tier friendly)
  • Barge-in               → if you start talking, the agent stops and listens

It reuses the build → deploy → WhatsApp pipeline from voice_agent.py, so the
moment the agent has enough detail it kicks off the same background job.

Run:
    python voice_realtime.py                 # serves on :8002
Expose (Twilio must reach it over wss):
    ngrok http 8002
Then set your Twilio number's Voice webhook to  https://<tunnel>/voice/incoming

Extra .env keys (on top of the Twilio/WhatsApp vars used by voice_agent.py):
    DEEPGRAM_API_KEY=...          # does BOTH speech-to-text and text-to-speech (Aura)
    DEEPGRAM_TTS_MODEL=...        # optional; default aura-asteria-en
    PUBLIC_BASE_URL=https://...   # optional; otherwise derived from the request host
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
from pathlib import Path

import httpx
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

import llm
import storage
import voice_agent as va  # reuse start_build_job, _twilio_client, config, JOBS

load_dotenv(Path(__file__).resolve().parent / ".env")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
# Text-to-speech now uses Deepgram "Aura" (same key as speech-to-text). It streams
# mu-law 8k natively — exactly what Twilio needs — and is covered by Deepgram's free
# credit, so no ElevenLabs / paid plan required. Pick any Aura voice below.
# aura-asteria-en (warm female) · aura-luna-en · aura-stella-en · aura-athena-en
# aura-orion-en (male) · aura-arcas-en · aura-perseus-en · aura-zeus-en
DEEPGRAM_TTS_MODEL = os.getenv("DEEPGRAM_TTS_MODEL", "aura-asteria-en")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_TURNS = int(os.getenv("VOICE_MAX_TURNS", "4"))

GREETING = (
    "Hi! I'm your A I website builder. In a sentence or two, tell me what kind "
    "of website you'd like me to create."
)

CONVO_SYSTEM = (
    "You are a friendly, concise AI website-builder agent talking to a caller on "
    "the PHONE. The caller will describe a website they want you to build.\n"
    "Your job:\n"
    "- Understand what they want. Ask AT MOST TWO short clarifying questions in "
    "total (for example: the site's purpose/pages, and the style/colors or one key "
    "feature). If the first request is already detailed, skip questions.\n"
    "- Keep EVERY reply to one, at most two, SHORT spoken sentences. Natural, human "
    "speech. No markdown, no lists, no stage directions, no emojis.\n"
    "- As soon as you have enough to build, confirm in ONE short sentence, then on a "
    "NEW LINE output exactly:\n"
    "  READY::<one detailed paragraph describing the website to build — purpose, "
    "pages/sections, visual style, colors, and any features the caller mentioned>\n"
    "- Never say the word READY out loud and never read the spec aloud; everything "
    "after READY:: is for the system only, not spoken."
)

app = FastAPI(title="Realtime voice → website builder")


# ===========================================================================
# Per-call session
# ===========================================================================
class Session:
    def __init__(self, twilio_ws: WebSocket):
        self.twilio_ws = twilio_ws
        self.stream_sid = None
        self.call_sid = None
        self.caller = ""               # the phone number that called us
        self.dg_ws = None              # deepgram websocket
        self.history: list[tuple[str, str]] = []
        self.turns = 0
        self.build_spec: str | None = None
        self.pending_end = False       # set once we've decided to build + hang up
        self.ai_speaking = False
        self.tts_task: asyncio.Task | None = None
        self.closing = False

    # ---------- Deepgram ----------
    async def connect_deepgram(self):
        url = (
            "wss://api.deepgram.com/v1/listen"
            "?encoding=mulaw&sample_rate=8000&channels=1"
            "&model=nova-2-phonecall&language=en-US"
            "&punctuate=true&smart_format=true"
            "&interim_results=true&endpointing=250"
            "&vad_events=true&utterance_end_ms=1000"
        )
        hdr = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        try:
            self.dg_ws = await websockets.connect(url, additional_headers=hdr)
        except TypeError:  # older websockets lib
            self.dg_ws = await websockets.connect(url, extra_headers=hdr)
        print("🟢 Deepgram connected")

    async def deepgram_keepalive(self):
        try:
            while not self.closing and self.dg_ws:
                await asyncio.sleep(7)
                try:
                    await self.dg_ws.send(json.dumps({"type": "KeepAlive"}))
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    # ---------- Twilio audio helpers ----------
    async def send_twilio_clear(self):
        if self.stream_sid:
            try:
                await self.twilio_ws.send_text(json.dumps(
                    {"event": "clear", "streamSid": self.stream_sid}))
            except Exception:
                pass

    async def cancel_tts(self):
        self.ai_speaking = False
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
            try:
                await self.tts_task
            except (asyncio.CancelledError, Exception):
                pass
        self.tts_task = None

    async def barge_in(self):
        # Don't let the caller interrupt the final goodbye (or we'd hang up early).
        if self.ai_speaking and not self.pending_end:
            print("⚡ Barge-in")
            await self.cancel_tts()
            await self.send_twilio_clear()

    # ---------- TTS (Deepgram Aura streaming) ----------
    async def speak(self, text: str, clear_first: bool = True):
        await self.cancel_tts()
        if clear_first:
            await self.send_twilio_clear()
        self.ai_speaking = True
        self.tts_task = asyncio.create_task(self._stream_tts(text))

    async def _stream_tts(self, text: str):
        # Deepgram Aura TTS → raw mu-law 8k (Twilio's native format), low latency.
        url = (
            f"https://api.deepgram.com/v1/speak?model={DEEPGRAM_TTS_MODEL}"
            f"&encoding=mulaw&sample_rate=8000&container=none"
        )
        headers = {
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {"text": text}
        sent = 0
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        err = await resp.aread()
                        print(f"❌ Deepgram TTS {resp.status_code}: {err[:200]!r}")
                        return
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        if not self.ai_speaking:
                            return
                        if not chunk:
                            continue
                        try:
                            await self.twilio_ws.send_text(json.dumps({
                                "event": "media",
                                "streamSid": self.stream_sid,
                                "media": {"payload": base64.b64encode(chunk).decode()},
                            }))
                            sent += len(chunk)
                        except Exception:
                            return
                    # Tell Twilio to notify us when this audio finishes playing.
                    try:
                        await self.twilio_ws.send_text(json.dumps({
                            "event": "mark", "streamSid": self.stream_sid,
                            "mark": {"name": "tts_done"},
                        }))
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ TTS error: {type(e).__name__}: {e}")
        finally:
            self.ai_speaking = False

    # ---------- Gemini conversation ----------
    async def _gemini_reply(self) -> str:
        cfg = storage.load_config()
        key = cfg.get("gemini_api_key", "")
        model = cfg.get("model") or "gemini-2.5-flash"
        convo = "\n".join(f"{r}: {c}" for r, c in self.history[-10:])
        prompt = (
            f"Conversation so far:\n{convo}\n\n"
            f"Reply as the AGENT now (1-2 short spoken sentences, per your rules)."
        )
        try:
            res = await asyncio.to_thread(
                llm.generate_text,
                api_key=key, model=model, prompt=prompt,
                system_instruction=CONVO_SYSTEM,
                temperature=0.6, max_output_tokens=400, timeout=30.0,
            )
            return (res.text or "").replace("*", "").replace("#", "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ Gemini convo error: {e}")
            return "Sorry, could you tell me a bit more about the website you'd like?"

    def _summary_spec(self) -> str:
        said = " ".join(c for r, c in self.history if r == "USER")
        return "A website based on the caller's request: " + said.strip()

    async def think_and_reply(self, user_text: str) -> str:
        """Returns what the agent should SAY. Sets build_spec + pending_end when
        it's time to build."""
        self.history.append(("USER", user_text))
        self.turns += 1

        reply = await self._gemini_reply()

        # Safety net: if the convo drags on, build from what we have.
        if self.turns >= MAX_TURNS and "READY::" not in reply:
            reply = "Okay, I have enough to get started.\nREADY::" + self._summary_spec()

        if "READY::" in reply:
            spoken, _, spec = reply.partition("READY::")
            spoken = spoken.strip() or "Perfect, I've got what I need."
            self.build_spec = (spec.strip() or self._summary_spec())
            self.history.append(("AGENT", spoken))
            # Kick off the real pipeline now (build runs while we say goodbye).
            va.start_build_job(self.build_spec, self.caller)
            self.pending_end = True
            return (
                spoken
                + " I'm building it now and I'll send the link to your WhatsApp "
                + "in a minute or two. Thanks, goodbye!"
            )

        self.history.append(("AGENT", reply))
        return reply


# ===========================================================================
# Deepgram receive loop — ASR + barge-in
# ===========================================================================
async def deepgram_loop(sess: Session):
    pending = ""
    try:
        async for raw in sess.dg_ws:
            if sess.closing:
                break
            data = json.loads(raw)
            typ = data.get("type")
            if typ == "SpeechStarted":
                await sess.barge_in()
            elif typ == "Results":
                alt = data.get("channel", {}).get("alternatives", [{}])[0]
                transcript = (alt.get("transcript") or "").strip()
                is_final = data.get("is_final", False)
                speech_final = data.get("speech_final", False)
                if transcript and is_final:
                    pending = (pending + " " + transcript).strip()
                if speech_final and pending and not sess.pending_end:
                    user_text, pending = pending, ""
                    print(f"🎤 {user_text}")
                    reply = await sess.think_and_reply(user_text)
                    await sess.speak(reply)
            elif typ == "UtteranceEnd":
                if pending and not sess.pending_end:
                    user_text, pending = pending, ""
                    print(f"🎤 (utt_end) {user_text}")
                    reply = await sess.think_and_reply(user_text)
                    await sess.speak(reply)
    except Exception as e:  # noqa: BLE001
        print("Deepgram loop err:", e)


def _hangup(call_sid: str | None):
    if not call_sid:
        return
    try:
        va._twilio_client().calls(call_sid).update(status="completed")
    except Exception as e:  # noqa: BLE001
        print("hangup err:", e)


# ===========================================================================
# HTTP: TwiML that opens the bidirectional media stream
# ===========================================================================
@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    form = await request.form()
    caller = form.get("From") or ""
    if PUBLIC_BASE_URL:
        host = PUBLIC_BASE_URL.replace("https://", "").replace("http://", "")
    else:
        host = request.headers.get("x-forwarded-host") or request.url.netloc
    wss = f"wss://{host}/media"
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f'    <Stream url="{wss}">\n'
        f'      <Parameter name="from" value="{html.escape(caller)}"/>\n'
        "    </Stream>\n"
        "  </Connect>\n"
        "</Response>"
    )
    return Response(content=twiml, media_type="application/xml")


# ===========================================================================
# WebSocket: Twilio Media Stream
# ===========================================================================
@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    sess = Session(ws)
    try:
        await sess.connect_deepgram()
    except Exception as e:  # noqa: BLE001
        print("❌ Deepgram connect failed:", e)
        await ws.close()
        return

    dg_task = asyncio.create_task(deepgram_loop(sess))
    ka_task = asyncio.create_task(sess.deepgram_keepalive())
    try:
        while True:
            data = json.loads(await ws.receive_text())
            ev = data.get("event")
            if ev == "start":
                start = data["start"]
                sess.stream_sid = start["streamSid"]
                sess.call_sid = start.get("callSid")
                cust = start.get("customParameters") or {}
                sess.caller = cust.get("from") or ""
                print(f"🟢 stream {sess.stream_sid} caller={sess.caller}")
                sess.history.append(("AGENT", GREETING))
                await sess.speak(GREETING, clear_first=False)
            elif ev == "media":
                audio = base64.b64decode(data["media"]["payload"])
                try:
                    await sess.dg_ws.send(audio)
                except Exception:
                    pass
            elif ev == "mark":
                name = (data.get("mark") or {}).get("name")
                if name == "tts_done" and sess.pending_end:
                    await asyncio.sleep(0.3)
                    _hangup(sess.call_sid)
                    break
            elif ev == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        print("media err:", e)
    finally:
        sess.closing = True
        await sess.cancel_tts()
        try:
            if sess.dg_ws:
                await sess.dg_ws.send(json.dumps({"type": "CloseStream"}))
                await sess.dg_ws.close()
        except Exception:
            pass
        dg_task.cancel()
        ka_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass
        print("🧹 session cleaned up")


# ===========================================================================
# Status / readiness
# ===========================================================================
@app.get("/jobs")
async def jobs():
    with va._JOBS_LOCK:
        return JSONResponse(sorted(va.JOBS.values(), key=lambda j: j.get("at", 0), reverse=True))


@app.get("/", response_class=HTMLResponse)
async def home():
    cfg = storage.load_config()

    def chip(ok, label):
        c = "#16a34a" if ok else "#dc2626"
        return (f'<span style="display:inline-block;margin:3px;padding:4px 10px;'
                f'border-radius:20px;background:{c};color:#fff;font-size:13px">'
                f'{"✓" if ok else "✗"} {label}</span>')

    return f"""<!doctype html><meta charset=utf-8><title>Realtime voice builder</title>
<body style="font-family:system-ui;max-width:760px;margin:40px auto;padding:0 16px">
<h1>🎙️ Full-duplex voice → website builder</h1>
<p>Call your Twilio number, have a quick chat, get the live link on WhatsApp.</p>
<div>
{chip(bool(cfg.get('gemini_api_key')), 'Gemini')}
{chip(bool(cfg.get('vercel_token')), 'Vercel')}
{chip(bool(va.TWILIO_SID and va.TWILIO_AUTH), 'Twilio')}
{chip(bool(DEEPGRAM_API_KEY), 'Deepgram (speech + voice)')}
{chip(bool(va.WHATSAPP_TO), 'WhatsApp')}
</div>
<p style="color:#555;font-size:14px">Webhook (HTTP POST): <code>/voice/incoming</code>.
Set Gemini &amp; Vercel in the builder's Settings; Twilio, Deepgram &amp;
WhatsApp in <code>.env</code>.</p>
</body>"""


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("VOICE_RT_PORT", "8002"))
    print(f"🎙️  Realtime voice agent on :{port}  (expose with ngrok; webhook = /voice/incoming)")
    uvicorn.run("voice_realtime:app", host="0.0.0.0", port=port, reload=False)
