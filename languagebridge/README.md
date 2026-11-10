# LanguageBridge 📞

**Translate a normal phone call. Nobody installs an app.**

1. Open the web page. Enter **your number** and **their number**, then press **Connect**.
2. **Both phones ring with a normal call.** Answer like any call.
3. They speak Tamil and you hear Telugu. You speak Telugu and they hear Tamil. Their language is detected automatically.

```
Phone A ──► Twilio ──► our server ──► Gnani Prisma v3.0 (speech → text, detects language)
                                  ──► translate
                                  ──► Gnani Timbre TTS ──► Twilio ──► Phone B
```

---

## One-time setup (about 20 minutes, all free)

### 1. Twilio free trial (no credit card)
1. Sign up at https://www.twilio.com/try-twilio
2. On the Console home page, copy the **Account SID** and **Auth Token**.
3. Get the free **Twilio phone number** (a US number is fine; it is only used to call out).
4. **Verify both demo phones:** Console → Phone Numbers → Manage → **Verified Caller IDs** → Add.
   Your sign-up number is already verified. Add your teammate's number (they get an SMS code).
5. **Allow calls to India:** Console → Voice → Settings → **Geo permissions** → tick **India**.

### 2. Install
```bash
pip install -r requirements.txt
cp .env.example .env
```
Fill `.env` with your Gnani key and Twilio details.

### 3. Start (two terminals)
**Terminal 1:**
```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```
**Terminal 2:**
```bash
ngrok http 8000
```
Copy the `https://....ngrok-free.app` link into `PUBLIC_URL` in `.env`, then **restart Terminal 1**
(Ctrl+C, run uvicorn again). Twilio needs this link to send the call audio to your laptop.

### 4. Call
Open the ngrok link (or http://localhost:8000), enter both numbers, and press **Connect call**.
- Both phones ring. Answer.
- Trial account: a voice says it's a trial account. **Press any key** to continue.
- After the **beep**, talk normally and **pause about 1 second** after each sentence.
- The web page shows live captions and the delay. Keep it on the projector for the judges.

---

## Tips for the demo
- **Speak one sentence, then pause.** The translation plays after you stop.
- Don't talk at the same time as the other person.
- Quiet room. If noise keeps triggering it, raise `SPEECH_RMS` in `.env` (e.g. 700).
- Test with `MOCK=1` first to see the page without making real calls.
- Twilio trial credit is limited, so keep practice calls short.

## Troubleshooting
| Problem | Fix |
|---|---|
| "Twilio: ... unverified" | Verify that number in Twilio → Verified Caller IDs |
| "Twilio: ... permission" | Enable India in Voice → Geo permissions |
| Phones ring but there's silence / no beep | `PUBLIC_URL` is wrong or ngrok restarted. Update `.env` and restart the server. |
| Captions show an error with 401 | The Gnani API key is wrong |
| Translation is wrong for mixed sentences | Set `TRANSLATOR=llm` with an LLM key |

## Honest limits (tell the judges)
- Turn-by-turn, like a walkie-talkie: each sentence is translated after you pause.
- Trial account: only verified numbers, plus a trial message at the start.
- **Next steps:** an Indian number via Exotel/Twilio so anyone can dial in, Gnani's streaming STT/TTS for lower delay, and voice cloning so each person hears the other's real voice.
