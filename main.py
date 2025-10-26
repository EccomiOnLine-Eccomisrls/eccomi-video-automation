import os, hmac, hashlib, base64, time, json, requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
import resend

# ==== ENV ====
DID_KEY = os.getenv("D_ID_API_KEY", "")
RESEND_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Eccomi Video <onboarding@resend.dev>")
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"

# ==== RESEND ====
if RESEND_KEY:
    resend.api_key = RESEND_KEY

# ==== APP ====
app = FastAPI(title="Eccomi Video Automation", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.eccomionline.com","https://eccomionline.com","*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==== MODELLI ====
class Job(BaseModel):
    image_url: str
    script: Optional[str] = None
    voice: Optional[str] = "ms:it-IT-GiuseppeNeural"  # "ms:<VOICE>" oppure "eleven:<VOICE_ID>"
    audio_url: Optional[str] = None
    to_email: str
    order_name: Optional[str] = None

# ==== UTILS ====
def did_headers():
    if not DID_KEY:
        raise HTTPException(500, "D_ID_API_KEY mancante")
    token = base64.b64encode((DID_KEY + ":").encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

def send_email(to_email: str, subject: str, html: str):
    if not RESEND_KEY:
        print("⚠️ RESEND_API_KEY mancante: salto invio email")
        return
    try:
        r = resend.Emails.send({
            "from": FROM_EMAIL,
            "to": to_email,
            "subject": subject,
            "html": html
        })
        print("✅ Email inviata:", r)
    except Exception as e:
        print("❌ ERRORE invio email:", e)

def make_did_payload(job: Job) -> Dict[str, Any]:
    payload = {"source_url": job.image_url, "config": {"stitch": True}}
    if job.audio_url:
        payload["audio_url"] = job.audio_url
    else:
        # TTS provider
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

def poll_and_notify(job: Job, talk_id: str, max_wait_sec: int = 600, every_sec: int = 5):
    """Poll finché pronto, poi invia email col link video."""
    waited = 0
    while waited <= max_wait_sec:
        status = did_status(talk_id)
        st = status.get("status")
        if st == "done" and status.get("result_url"):
            video_url = status["result_url"]
            html = f"""
            <p>Ciao! 👋</p>
            <p>Il tuo <b>Video Parlante AI</b> è pronto.</p>
            <p><a href="{video_url}" target="_blank">Scarica il video qui</a></p>
            <p>Grazie da Eccomi OnLine!</p>
            """
            send_email(job.to_email, f"Video AI pronto — Ordine {job.order_name or ''}", html)
            return
        if st in ("error","failed"):
            send_email(job.to_email, f"Problema con il tuo Video AI — Ordine {job.order_name or ''}",
                       "<p>Si è verificato un errore durante la generazione. Ti contatteremo a breve.</p>")
            return
        time.sleep(every_sec)
        waited += every_sec
    # timeout
    send_email(job.to_email, f"Stiamo completando il tuo Video AI — Ordine {job.order_name or ''}",
               "<p>La generazione richiede più tempo del previsto. Ti avviseremo non appena sarà pronto.</p>")

def verify_shopify_hmac(request: Request, raw_body: bytes):
    if not VERIFY_SHOPIFY_HMAC or not SHOPIFY_WEBHOOK_SECRET:
        return True
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode() == hmac_header

# ==== ENDPOINTS ====
@app.get("/api/health")
def health():
    return {"ok": True, "service": "EccomiVideoAutomation", "version": "1.0"}

@app.post("/api/jobs")
async def create_job(job: Job, bg: BackgroundTasks):
    talk = did_create_talk(job)
    talk_id = talk.get("id")
    if talk_id:
        bg.add_task(poll_and_notify, job, talk_id)
    return {"ok": True, "talk": talk}

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
        image_url  = props.get("Foto") or props.get("Immagine") or props.get("Image")
        script     = props.get("Testo") or props.get("Script")
        voice_sel  = props.get("Voce") or "Uomo"
        audio_url  = props.get("Audio")

        voice = "ms:it-IT-GiuseppeNeural"  # default Uomo
        if voice_sel.lower().startswith("don"):
            voice = "ms:it-IT-IsabellaNeural"
        if voice_sel.startswith("eleven:"):
            voice = voice_sel

        if not image_url or (not script and not audio_url) or not to_email:
            continue

        job = Job(image_url=image_url, script=script, voice=voice, audio_url=audio_url, to_email=to_email, order_name=str(order_name))
        talk = did_create_talk(job)
        talk_id = talk.get("id")
        if talk_id:
            bg.add_task(poll_and_notify, job, talk_id)
        jobs_created.append({"line_item_id": li.get("id"), "talk": talk})

    return {"ok": True, "jobs": jobs_created}
