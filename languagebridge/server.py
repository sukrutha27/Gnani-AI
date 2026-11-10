"""
LanguageBridge - translate a NORMAL phone call. Nobody installs an app.

  1. Open the web page, enter your number + the other person's number, press Connect.
  2. Twilio calls both phones. Both answer like a normal call.
  3. Every time someone finishes a sentence:
       their audio --> Gnani Prisma v3.0 (detects language) --> translate
                   --> Gnani Timbre TTS --> played to the OTHER phone.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000   (+ ngrok http 8000)
"""
import asyncio, base64, io, json, os, time, uuid, wave

import numpy as np
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

load_dotenv()
GNANI_API_KEY = os.getenv("GNANI_API_KEY", "")
GNANI = "https://api.vachana.ai"
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")      # your ngrok https URL
TTS_VOICE = os.getenv("TTS_VOICE", "Shivani")              # Timbre v3.0 voice: speaks all 10 languages
MOCK = os.getenv("MOCK", "0") == "1"

TRANSLATOR = os.getenv("TRANSLATOR", "google")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

LANGS = {"te-IN": "Telugu", "ta-IN": "Tamil", "hi-IN": "Hindi", "kn-IN": "Kannada",
         "ml-IN": "Malayalam", "mr-IN": "Marathi", "bn-IN": "Bengali", "gu-IN": "Gujarati",
         "pa-IN": "Punjabi", "en-IN": "English"}

app = FastAPI(title="LanguageBridge")
sessions: dict[str, dict] = {}


# ------------------------------------------------------------ audio helpers
# Phone audio from Twilio is 8 kHz mu-law. These convert to/from 16-bit PCM.
_MU_DECODE = None
def mulaw_decode(b: bytes) -> np.ndarray:
    global _MU_DECODE
    if _MU_DECODE is None:
        u = ~np.arange(256, dtype=np.int32) & 0xFF
        t = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4)
        _MU_DECODE = np.where(u & 0x80, 0x84 - t, t - 0x84).astype(np.int16)
    return _MU_DECODE[np.frombuffer(b, dtype=np.uint8)]

def mulaw_encode(pcm: np.ndarray) -> bytes:
    x = pcm.astype(np.int32)
    sign = (x < 0)
    x = np.minimum(np.abs(x), 32635) + 0x84
    exp = np.floor(np.log2(x)).astype(np.int32) - 7
    exp = np.clip(exp, 0, 7)
    mant = (x >> (exp + 3)) & 0x0F
    u = ~((sign.astype(np.int32) << 7) | (exp << 4) | mant) & 0xFF
    return u.astype(np.uint8).tobytes()

def pcm_to_wav(pcm: np.ndarray, rate=8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()

def wav_to_pcm8k(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1: pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if rate != 8000:
        idx = np.arange(0, len(pcm), rate / 8000)
        pcm = np.interp(idx, np.arange(len(pcm)), pcm).astype(np.int16)
    return pcm

def beep(freq=880, ms=180) -> bytes:
    t = np.arange(int(8 * ms)) / 8000
    return mulaw_encode((np.sin(2 * np.pi * freq * t) * 6000).astype(np.int16))


# ---------------------------------------------------------------- Gnani APIs
def gnani_stt(wav: bytes) -> dict:
    if MOCK:
        return {"transcript": "வணக்கம் உங்கள் பார்சல் வந்துவிட்டது", "detected_language_code": "ta-IN"}
    r = requests.post(f"{GNANI}/stt/v3", headers={"X-API-Key-ID": GNANI_API_KEY},
                      files={"audio_file": ("speech.wav", wav, "audio/wav")},
                      data={"model": "prisma-v3.0", "language_code": "indic-auto", "format": "transcribe"},
                      timeout=30)
    r.raise_for_status()
    return r.json()

def gnani_tts(text: str, lang: str) -> np.ndarray:
    if MOCK:
        t = np.arange(8000) / 8000
        return (np.sin(2 * np.pi * 440 * t) * 4000).astype(np.int16)
    r = requests.post(f"{GNANI}/api/v1/tts/inference",
                      headers={"X-API-Key-ID": GNANI_API_KEY, "Content-Type": "application/json"},
                      json={"text": text, "model": "timbre-v3.0", "voice": TTS_VOICE, "language": lang,
                            "audio_config": {"sample_rate": 8000, "num_channels": 1, "sample_width": 2,
                                             "encoding": "linear_pcm", "container": "wav"}},
                      timeout=30)
    r.raise_for_status()
    return wav_to_pcm8k(r.content)

def translate(text: str, src: str, tgt: str) -> str:
    if not text or src == tgt: return text
    if MOCK: return "నమస్కారం మీ పార్సెల్ వచ్చింది"
    if TRANSLATOR == "llm" and LLM_API_KEY:
        prompt = (f"Translate this {LANGS.get(src, src)} phone-call sentence (it may mix English words) "
                  f"into natural spoken {LANGS.get(tgt, tgt)} in its native script. Keep names, numbers "
                  f"and amounts exactly. Reply with the translation only.\n\n{text}")
        r = requests.post(f"{LLM_BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                          json={"model": LLM_MODEL, "temperature": 0,
                                "messages": [{"role": "user", "content": prompt}]}, timeout=30)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto", target=tgt.split("-")[0]).translate(text)


# ------------------------------------------------------------ live captions
async def push(sess: dict, msg: dict):
    sess["log"].append(msg)
    for ws in list(sess["viewers"]):
        try: await ws.send_json(msg)
        except Exception: sess["viewers"].discard(ws)


# ---------------------------------------------------------- web page + API
@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))

@app.get("/config")
def config():
    return {"mock": MOCK, "languages": LANGS}

class ConnectReq(BaseModel):
    my_number: str
    my_lang: str = "te-IN"
    their_number: str
    their_lang: str = "auto"   # auto = detect from their speech

@app.post("/connect")
def connect(req: ConnectReq):
    sid = uuid.uuid4().hex[:8]
    sessions[sid] = {
        "lang": {"you": req.my_lang, "them": None if req.their_lang == "auto" else req.their_lang},
        "numbers": {"you": req.my_number, "them": req.their_number},
        "legs": {}, "calls": {}, "viewers": set(), "log": [],
    }
    if MOCK:
        return {"ok": True, "session": sid, "mock": True}
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_NUMBER and PUBLIC_URL):
        return JSONResponse({"ok": False, "error": "Set TWILIO_* and PUBLIC_URL in .env"}, 400)
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    ws_url = PUBLIC_URL.replace("https://", "wss://").replace("http://", "ws://") + "/media"
    try:
        for leg in ("you", "them"):
            twiml = (f'<Response><Connect><Stream url="{ws_url}">'
                     f'<Parameter name="session" value="{sid}"/><Parameter name="leg" value="{leg}"/>'
                     f'</Stream></Connect></Response>')
            call = client.calls.create(to=sessions[sid]["numbers"][leg], from_=TWILIO_NUMBER, twiml=twiml)
            sessions[sid]["calls"][leg] = call.sid
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Twilio: {e}"}, 400)
    return {"ok": True, "session": sid}

@app.post("/hangup/{sid}")
def hangup(sid: str):
    sess = sessions.get(sid)
    if sess and not MOCK:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        for call_sid in sess["calls"].values():
            try: client.calls(call_sid).update(status="completed")
            except Exception: pass
    return {"ok": True}

@app.websocket("/live/{sid}")
async def live(ws: WebSocket, sid: str):
    await ws.accept()
    sess = sessions.get(sid)
    if not sess:
        await ws.close(); return
    for m in sess["log"]: await ws.send_json(m)
    sess["viewers"].add(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        sess["viewers"].discard(ws)


# ---------------------------------------------- Twilio media stream (per phone)
SPEECH_RMS = int(os.getenv("SPEECH_RMS", "450"))   # loudness that counts as speech
END_SILENCE_FRAMES = 40                              # 40 x 20 ms = 0.8 s of silence ends a sentence
MIN_SPEECH_FRAMES = 15                               # ignore noises shorter than 0.3 s
MAX_FRAMES = 25 * 50                                 # Gnani REST limit is 30 s

async def send_audio(leg: dict, mulaw: bytes):
    """Play mu-law audio to one phone and remember when it finishes (to ignore echo)."""
    ws, sid = leg["ws"], leg["stream_sid"]
    for i in range(0, len(mulaw), 8000):
        await ws.send_json({"event": "media", "streamSid": sid,
                            "media": {"payload": base64.b64encode(mulaw[i:i + 8000]).decode()}})
    leg["playing_until"] = max(time.time(), leg.get("playing_until", 0)) + len(mulaw) / 8000

async def handle_sentence(sess: dict, who: str, pcm: np.ndarray):
    other = "them" if who == "you" else "you"
    t0 = time.perf_counter()
    try:
        stt = await asyncio.to_thread(gnani_stt, pcm_to_wav(pcm))
    except Exception as e:
        await push(sess, {"type": "error", "text": f"Speech-to-text failed: {e}"}); return
    text = (stt.get("transcript") or "").strip()
    if not text: return
    src = stt.get("detected_language_code") or sess["lang"][who] or "hi-IN"
    if who == "them": sess["lang"]["them"] = src          # remember the caller's language
    t_stt = time.perf_counter() - t0
    tgt = sess["lang"][other] or "hi-IN"
    try:
        t1 = time.perf_counter()
        out = await asyncio.to_thread(translate, text, src, tgt); t_tr = time.perf_counter() - t1
        t2 = time.perf_counter()
        pcm_out = await asyncio.to_thread(gnani_tts, out, tgt); t_tts = time.perf_counter() - t2
    except Exception as e:
        await push(sess, {"type": "error", "text": f"Translate/voice failed: {e}"}); return
    leg = sess["legs"].get(other)
    if leg: await send_audio(leg, mulaw_encode(pcm_out))
    await push(sess, {"type": "caption", "who": who, "original": text, "src": LANGS.get(src, src),
                      "translated": out, "tgt": LANGS.get(tgt, tgt),
                      "timing": {"stt": round(t_stt, 2), "translate": round(t_tr, 2),
                                 "tts": round(t_tts, 2), "total": round(time.perf_counter() - t0, 2)}})

@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    sess = leg = None; who = None
    buf: list[np.ndarray] = []; pre: list[np.ndarray] = []
    speaking = False; silence = 0; voiced = 0
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            ev = msg.get("event")
            if ev == "start":
                p = msg["start"].get("customParameters", {})
                sess = sessions.get(p.get("session")); who = p.get("leg")
                if not sess: break
                leg = {"ws": ws, "stream_sid": msg["start"]["streamSid"], "playing_until": 0}
                sess["legs"][who] = leg
                await push(sess, {"type": "status", "who": who, "state": "answered"})
                if len(sess["legs"]) == 2:   # both answered: short beep so both know it's live
                    for l in sess["legs"].values(): await send_audio(l, beep())
                    await push(sess, {"type": "status", "state": "connected"})
            elif ev == "media" and sess:
                if time.time() < leg["playing_until"]:      # we are talking to this phone: ignore echo
                    continue
                frame = mulaw_decode(base64.b64decode(msg["media"]["payload"]))
                loud = np.sqrt(np.mean(frame.astype(np.float32) ** 2)) > SPEECH_RMS
                if not speaking:
                    pre = (pre + [frame])[-15:]               # keep 0.3 s before speech starts
                    voiced = voiced + 1 if loud else 0
                    if voiced >= 3:
                        speaking, buf, silence = True, list(pre), 0
                        await push(sess, {"type": "speaking", "who": who})
                else:
                    buf.append(frame)
                    silence = 0 if loud else silence + 1
                    if silence >= END_SILENCE_FRAMES or len(buf) >= MAX_FRAMES:
                        speaking, voiced = False, 0
                        if len(buf) - silence >= MIN_SPEECH_FRAMES:
                            asyncio.create_task(handle_sentence(sess, who, np.concatenate(buf)))
                        buf = []
            elif ev == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        if sess and who:
            sess["legs"].pop(who, None)
            await push(sess, {"type": "status", "who": who, "state": "hung up"})
