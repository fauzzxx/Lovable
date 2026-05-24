import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from google import genai
from google.genai import types

from deepgram import AsyncDeepgramClient
from deepgram.listen import (
    ListenV1Metadata,
    ListenV1Results,
    ListenV1SpeechStarted,
    ListenV1UtteranceEnd,
)

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not DEEPGRAM_API_KEY or not GEMINI_API_KEY:
    raise RuntimeError("Set DEEPGRAM_API_KEY and GEMINI_API_KEY in .env")

gemini = genai.Client(api_key=GEMINI_API_KEY)
deepgram = AsyncDeepgramClient(api_key=DEEPGRAM_API_KEY)

SYSTEM_PROMPT = (
    "You are a friendly conversational voice assistant. "
    "Respond in 1-3 short sentences as if speaking on a phone call. "
    "Do not use markdown, lists, or code blocks. Be warm and natural."
)

GEMINI_MODEL = "gemini-3.1-flash-lite"
DEEPGRAM_STT_MODEL = "nova-2"

VOICES = [
    {"id": "aura-2-asteria-en", "name": "Asteria", "description": "Female, US — friendly, default"},
    {"id": "aura-2-luna-en", "name": "Luna", "description": "Female, US — soft, warm"},
    {"id": "aura-2-stella-en", "name": "Stella", "description": "Female, US — bright, expressive"},
    {"id": "aura-2-athena-en", "name": "Athena", "description": "Female, US — mature, authoritative"},
    {"id": "aura-2-hera-en", "name": "Hera", "description": "Female, US — business, professional"},
    {"id": "aura-2-helena-en", "name": "Helena", "description": "Female, US — warm, conversational"},
    {"id": "aura-2-orion-en", "name": "Orion", "description": "Male, US — calm, friendly"},
    {"id": "aura-2-arcas-en", "name": "Arcas", "description": "Male, US — natural, casual"},
    {"id": "aura-2-perseus-en", "name": "Perseus", "description": "Male, US — confident, clear"},
    {"id": "aura-2-zeus-en", "name": "Zeus", "description": "Male, US — deep, authoritative"},
    {"id": "aura-2-apollo-en", "name": "Apollo", "description": "Male, US — friendly, upbeat"},
    {"id": "aura-2-jupiter-en", "name": "Jupiter", "description": "Male, US — warm, calm"},
    {"id": "aura-2-sirio-es", "name": "Sirio (Español)", "description": "Male, ES — Spanish"},
    {"id": "aura-2-carina-es", "name": "Carina (Español)", "description": "Female, ES — Spanish"},
]
VOICE_IDS = {v["id"] for v in VOICES}
DEFAULT_VOICE = VOICES[0]["id"]

app = FastAPI()
INDEX_HTML = Path(__file__).parent / "index.html"


@app.get("/")
async def root():
    return FileResponse(INDEX_HTML)


@app.get("/voices")
async def voices():
    return {"voices": VOICES, "default": DEFAULT_VOICE}


async def synthesize_speech(text: str, voice: str) -> bytes:
    chunks: list[bytes] = []
    async for chunk in deepgram.speak.v1.audio.generate(
        text=text,
        model=voice,
        encoding="mp3",
    ):
        chunks.append(chunk)
    return b"".join(chunks)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    voice_param = ws.query_params.get("voice")
    state = {
        "voice": voice_param if voice_param in VOICE_IDS else DEFAULT_VOICE,
    }

    chat = gemini.chats.create(
        model=GEMINI_MODEL,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )

    transcript_queue: asyncio.Queue[str] = asyncio.Queue()
    accumulated: list[str] = []

    async def speak(text: str):
        await ws.send_json({"type": "assistant_text", "text": text})
        try:
            audio = await synthesize_speech(text, state["voice"])
            await ws.send_bytes(audio)
        except Exception as e:
            print(f"TTS error: {e}")
            try:
                await ws.send_json({"type": "error", "text": f"TTS failed: {e}"})
            except Exception:
                pass

    async def handle_pipeline():
        try:
            while True:
                user_text = await transcript_queue.get()
                await ws.send_json({"type": "user_transcript", "text": user_text})
                try:
                    response = await asyncio.to_thread(chat.send_message, user_text)
                    reply = (response.text or "").strip()
                except Exception as e:
                    print(f"Gemini error: {e}")
                    reply = "Sorry, I had trouble thinking of a response."
                if reply:
                    await speak(reply)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"Pipeline error: {e}")

    try:
        async with deepgram.listen.v1.connect(
            model=DEEPGRAM_STT_MODEL,
            language="en-US",
            smart_format=True,
            interim_results=True,
            endpointing=300,
            utterance_end_ms=1000,
            vad_events=True,
        ) as dg:

            async def read_deepgram():
                try:
                    while True:
                        event = await dg.recv()
                        if isinstance(event, ListenV1Results):
                            try:
                                sentence = event.channel.alternatives[0].transcript
                            except (AttributeError, IndexError):
                                continue
                            if not sentence:
                                continue
                            if getattr(event, "is_final", False):
                                accumulated.append(sentence)
                            if getattr(event, "speech_final", False):
                                full = " ".join(accumulated).strip()
                                accumulated.clear()
                                if full:
                                    await transcript_queue.put(full)
                        elif isinstance(event, ListenV1UtteranceEnd):
                            if accumulated:
                                full = " ".join(accumulated).strip()
                                accumulated.clear()
                                if full:
                                    await transcript_queue.put(full)
                        elif isinstance(event, (ListenV1Metadata, ListenV1SpeechStarted)):
                            pass
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"Deepgram recv error: {e}")

            reader_task = asyncio.create_task(read_deepgram())
            pipeline_task = asyncio.create_task(handle_pipeline())
            greeting_task = asyncio.create_task(
                speak("Hi! I'm your voice assistant. What can I help you with?")
            )

            try:
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    audio_bytes = msg.get("bytes")
                    if audio_bytes:
                        await dg.send_media(audio_bytes)
                        continue
                    text = msg.get("text")
                    if text:
                        try:
                            data = json.loads(text)
                        except Exception:
                            continue
                        if data.get("type") == "set_voice":
                            new_voice = data.get("voice")
                            if new_voice in VOICE_IDS:
                                state["voice"] = new_voice
                                await ws.send_json({
                                    "type": "voice_changed",
                                    "voice": new_voice,
                                })
            except WebSocketDisconnect:
                pass
            finally:
                reader_task.cancel()
                pipeline_task.cancel()
                greeting_task.cancel()
                try:
                    await dg.send_close_stream()
                except Exception:
                    pass
    except Exception as e:
        print(f"WS error: {e}")
        try:
            await ws.send_json({"type": "error", "text": str(e)})
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
