import os, hmac, hashlib, base64, time, json, requests
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import resend

# ---------- ENV ----------
DID_KEY = os.getenv("D_ID_API_KEY", "")
RESEND_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Eccomi Video <onboarding@resend.dev>")
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"

HEYGEN_KEY = os.getenv("HEYGEN_API_KEY", "")
HEYGEN_AVATAR = os.getenv("HEYGEN_AVATAR_ID", "")
HEYGEN_VOICE_ID = os.getenv("HEYGEN_VOICE_ID", "it_male_energetic")

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")   # opzionale

if RESEND_KEY:
    resend.api_key = RESEND_KEY

# ---------- APP ----------
app = FastAPI(title="Eccomi Video Automation", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.eccomionline.com","https://eccomionline.com","*"],
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- MODELLI ----------
class Job(BaseModel):
    image_url: str
    script: Optional[str] = None      # testo se non passi audio_url
    voice: Optional[str] = "ms:it-IT-GiuseppeNeural"  # "ms:<VOICE>" o "eleven:<VOICE_ID>"
    audio_url: Optional[str] = None
    to_email: str
    order_name: Optional[str] = None

# ---------- UTILS ----------
def send_email(to_email: str, subject: str, html: str):
    if not RESEND_KEY:
        print("⚠️ RESEND_API_KEY mancante: salto invio email")
        return
    try:
        r = resend.Emails.send({"from": FROM_EMAIL, "to": to_email, "subject": subject, "html": html})
        print("✅ Email inviata:", r)
    except Exception as e:
        print("❌ ERRORE invio email:", e)

# === D-ID ===
def did_headers():
    if not DID_KEY:
        raise HTTPException(500, "D_ID_API_KEY mancante")
    return {"Authorization": f"Basic {base64.b64encode((DID_KEY+':').encode()).decode()}",
            "Content-Type": "application/json"}

def make_did_payload(job: Job) -> Dict[str, Any]:
    payload = {"source_url": job.image_url, "config": {"stitch": True}}
    if job.audio_url:
        payload["audio_url"] = job.audio_url
    else:
        if job.voice and job.voice.startswith("eleven:"):
            provider = {"type": "elevenlabs", "voice_id": job.voice.split(":",1)[1]}
        else:
            voice_id = job.voice.split(":",1)[1] if ":" in job.voice else "it-IT-GiuseppeNeural"
            provider = {"type": "microsoft", "voice_id": voice_id}
        payload["script"] = {"type": "text", "input": job.script or "Ciao! Il tuo video è pronto.", "provider": provider}
    return payload

def did_create_talk(job: Job) -> Dict[str, Any]:
    r = requests.post("https://api.d-id.com/talks", headers=did_headers(), json=make_did_payload(job), timeout=90)
    if r.status_code not in (200,201):
        raise HTTPException(r.status_code, f"D-ID create error: {r.text}")
    return r.json()

def did_status(talk_id: str) -> Dict[str, Any]:
    r = requests.get(f"https://api.d-id.com/talks/{talk_id}", headers=did_headers(), timeout=60)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"D-ID status error: {r.text}")
    return r.json()

def poll_and_notify_did(job: Job, talk_id: str, max_wait_sec: int = 600, every_sec: int = 5):
    waited = 0
    while waited <= max_wait_sec:
        s = did_status(talk_id)
        st = s.get("status")
        if st == "done" and s.get("result_url"):
            video_url = s["result_url"]
            html = f"""<p>Ciao! 👋</p><p>Il tuo <b>Video Parlante AI</b> è pronto.</p>
                       <p><a href="{video_url}" target="_blank">Scarica il video</a></p>"""
            send_email(job.to_email, f"Video AI pronto — Ordine {job.order_name or ''}", html)
            return
        if st in ("error","failed"):
            send_email(job.to_email, f"Problema con il tuo Video AI — Ordine {job.order_name or ''}",
                       "<p>Si è verificato un errore. Ti contatteremo a breve.</p>")
            return
        time.sleep(every_sec); waited += every_sec
    send_email(job.to_email, f"Stiamo completando il tuo Video AI — Ordine {job.order_name or ''}",
               "<p>La generazione richiede più tempo del previsto. Ti avviseremo appena pronto.</p>")

# === HeyGen (avatar pronti) ===
def heygen_submit_text(script: str, avatar_id: Optional[str] = None, voice_id: Optional[str] = None) -> str:
    if not HEYGEN_KEY: raise HTTPException(500, "HEYGEN_API_KEY mancante")
    aid = avatar_id or HEYGEN_AVATAR
    if not aid: raise HTTPException(500, "HEYGEN_AVATAR_ID mancante")

    url = "https://api.heygen.com/v1/video.submit"
    headers = {"X-Api-Key": HEYGEN_KEY, "Content-Type": "application/json"}
    data = {
        "avatar_id": aid,
        "script": {"type": "text", "input_text": script, "voice_id": (voice_id or HEYGEN_VOICE_ID)},
        "test": False, "caption": False, "aspect_ratio": "9:16", "resolution": "720p"
    }
    r = requests.post(url, json=data, headers=headers, timeout=120)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"HeyGen submit error: {r.text}")
    return r.json().get("data", {}).get("video_id")

def heygen_submit_audio(audio_url: str, avatar_id: Optional[str] = None) -> str:
    if not HEYGEN_KEY: raise HTTPException(500, "HEYGEN_API_KEY mancante")
    aid = avatar_id or HEYGEN_AVATAR
    if not aid: raise HTTPException(500, "HEYGEN_AVATAR_ID mancante")

    url = "https://api.heygen.com/v1/video.submit"
    headers = {"X-Api-Key": HEYGEN_KEY, "Content-Type": "application/json"}
    data = {"avatar_id": aid, "audio": {"type":"mp3","source":"url","url": audio_url},
            "test": False, "caption": False, "aspect_ratio": "9:16", "resolution":"720p"}
    r = requests.post(url, json=data, headers=headers, timeout=120)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"HeyGen submit error: {r.text}")
    return r.json().get("data", {}).get("video_id")

def heygen_status(video_id: str) -> Dict[str, Any]:
    headers = {"X-Api-Key": HEYGEN_KEY}
    r = requests.get(f"https://api.heygen.com/v1/video.status?video_id={video_id}", headers=headers, timeout=60)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"HeyGen status error: {r.text}")
    return r.json()

# ---------- HMAC Shopify ----------
def verify_shopify_hmac(request: Request, raw_body: bytes):
    if not VERIFY_SHOPIFY_HMAC or not SHOPIFY_WEBHOOK_SECRET:
        return True
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode() == hmac_header

# ---------- ENDPOINTS ----------
@app.get("/api/health")
def health():
    return {"ok": True, "service": "EccomiVideoAutomation", "version": "1.0"}

@app.get("/api/diag/env")
def diag_env():
    return {
        "D_ID_API_KEY": bool(DID_KEY),
        "RESEND_API_KEY": bool(RESEND_KEY),
        "HEYGEN_API_KEY": bool(HEYGEN_KEY),
        "HEYGEN_AVATAR_ID": bool(HEYGEN_AVATAR),
        "HEYGEN_VOICE_ID": HEYGEN_VOICE_ID,
    }

# FOTO → VIDEO (D-ID)
@app.post("/api/jobs/photo")
async def create_job_photo(job: Job, bg: BackgroundTasks):
    talk = did_create_talk(job)
    talk_id = talk.get("id")
    if talk_id:
        bg.add_task(poll_and_notify_did, job, talk_id)
    return {"ok": True, "provider": "d-id", "talk": talk}

# AVATAR HEYGEN (testo)
class HeygenText(BaseModel):
    script: str
    avatar_id: Optional[str] = None
    voice_id: Optional[str] = None

@app.post("/api/heygen/submit")
def heygen_submit_endpoint(body: HeygenText):
    vid = heygen_submit_text(body.script, body.avatar_id, body.voice_id)
    return {"ok": True, "provider": "heygen", "video_id": vid}

# AVATAR HEYGEN (audio url)
class HeygenAudio(BaseModel):
    audio_url: str
    avatar_id: Optional[str] = None

@app.post("/api/heygen/submit-audio")
def heygen_submit_audio_endpoint(body: HeygenAudio):
    vid = heygen_submit_audio(body.audio_url, body.avatar_id)
    return {"ok": True, "provider": "heygen", "video_id": vid}

@app.get("/api/heygen/status")
def heygen_status_endpoint(video_id: str):
    return heygen_status(video_id)

# Shopify: route in base al “Tipo”
@app.post("/api/hooks/shopify")
async def shopify_hook(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if not verify_shopify_hmac(request, raw):
        raise HTTPException(401, "Invalid HMAC")
    payload = json.loads(raw.decode("utf-8"))

    to_email = (payload.get("customer") or {}).get("email") or payload.get("email") or ""
    order_name = payload.get("name") or payload.get("order_number")

    jobs_created = []
    for li in payload.get("line_items", []):
        props = {p.get("name"): p.get("value") for p in li.get("properties", []) if p.get("name")}
        tipo      = (props.get("Tipo") or props.get("Type") or "").lower()  # "foto" | "avatar"
        image_url = props.get("Foto") or props.get("Immagine") or props.get("Image")
        script    = props.get("Testo") or props.get("Script")
        voice_sel = props.get("Voce") or "Uomo"
        audio_url = props.get("Audio")

        if tipo == "avatar":
            voice_id = HEYGEN_VOICE_ID if voice_sel.lower().startswith("uomo") else "it_female_calm"
            if audio_url:
                video_id = heygen_submit_audio(audio_url, None)
            else:
                if not script: continue
                video_id = heygen_submit_text(script, None, voice_id)
            jobs_created.append({"line_item_id": li.get("id"), "provider":"heygen", "video_id": video_id})
            # opzionale: potresti fare polling lato client con /api/heygen/status
        else:
            # default: FOTO → D-ID
            if not image_url or (not script and not audio_url) or not to_email:
                continue
            voice = "ms:it-IT-GiuseppeNeural"
            if str(voice_sel).lower().startswith("don"):
                voice = "ms:it-IT-IsabellaNeural"
            if voice_sel.startswith("eleven:"):
                voice = voice_sel
            job = Job(image_url=image_url, script=script, voice=voice, audio_url=audio_url,
                      to_email=to_email, order_name=str(order_name))
            talk = did_create_talk(job)
            talk_id = talk.get("id")
            if talk_id:
                bg.add_task(poll_and_notify_did, job, talk_id)
            jobs_created.append({"line_item_id": li.get("id"), "provider":"d-id", "talk": talk})

    return {"ok": True, "jobs": jobs_created}
