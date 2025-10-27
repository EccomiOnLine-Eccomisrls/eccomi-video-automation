import os, hmac, hashlib, base64, time, json, requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
import resend

# ===== ENV =====
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

RESEND_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Eccomi Video <onboarding@resend.dev>")

SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"

HEYGEN_KEY = os.getenv("HEYGEN_API_KEY", "")
HEYGEN_AVATAR = os.getenv("HEYGEN_AVATAR_ID", "")
HEYGEN_VOICE_ID = os.getenv("HEYGEN_VOICE_ID", "it_male_energetic")  # default

# ===== RESEND =====
if RESEND_KEY:
    resend.api_key = RESEND_KEY

# ===== APP =====
app = FastAPI(title="Eccomi Video Automation", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.eccomionline.com", "https://eccomionline.com", "*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def dbg(*args):
    if DEBUG:
        print("DEBUG:", *args, flush=True)

# ===== MODELLI =====
class Job(BaseModel):
    image_url: Optional[str] = None
    script: Optional[str] = None
    voice: Optional[str] = None        # "it_male_energetic" o "heygen:it_male_energetic"
    audio_url: Optional[str] = None    # url mp3 (pubblico)
    to_email: str
    order_name: Optional[str] = None

# ===== UTILS =====
def send_email(to_email: str, subject: str, html: str):
    if not RESEND_KEY:
        dbg("RESEND_API_KEY mancante: non invio email")
        return
    try:
        r = resend.Emails.send({
            "from": FROM_EMAIL,
            "to": to_email,
            "subject": subject,
            "html": html
        })
        dbg("Email inviata:", r)
    except Exception as e:
        print("❌ Email error:", e, flush=True)

def verify_shopify_hmac(request: Request, raw_body: bytes):
    if not VERIFY_SHOPIFY_HMAC or not SHOPIFY_WEBHOOK_SECRET:
        return True
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode() == hmac_header

# ===== HEYGEN =====
def _pick_heygen_voice(v: Optional[str]) -> str:
    if not v:
        return HEYGEN_VOICE_ID
    if v.startswith("heygen:"):
        return v.split(":", 1)[1]
    if ":" in v:
        return HEYGEN_VOICE_ID
    return v

def _req(method: str, url: str, **kw):
    # timeout duro per non bloccare
    kw.setdefault("timeout", 20)
    try:
        dbg(method, url)
        r = requests.request(method, url, **kw)
        dbg("resp", r.status_code, r.text[:300])
        return r
    except requests.Timeout:
        raise HTTPException(504, "Timeout verso provider")
    except Exception as e:
        raise HTTPException(502, f"Errore rete: {e}")

def heygen_submit(script: Optional[str],
                  audio_url: Optional[str],
                  voice_id: Optional[str] = None,
                  avatar_id: Optional[str] = None,
                  test_mode: bool = False) -> str:
    if not HEYGEN_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")
    aid = avatar_id or HEYGEN_AVATAR
    if not aid:
        raise HTTPException(500, "HEYGEN_AVATAR_ID mancante")
    if not audio_url and not script:
        raise HTTPException(422, "Fornisci 'script' oppure 'audio_url'")

    url = "https://api.heygen.com/v1/video.submit"
    headers = {"X-Api-Key": HEYGEN_KEY, "Content-Type": "application/json"}
    data: Dict[str, Any] = {
        "avatar_id": aid,
        "test": bool(test_mode),
        "caption": False,
        "aspect_ratio": "9:16",
        "resolution": "720p",
    }
    if audio_url:
        data["audio"] = {"type": "mp3", "source": "url", "url": audio_url}
    else:
        data["script"] = {"type": "text", "input_text": script, "voice_id": voice_id or HEYGEN_VOICE_ID}

    r = _req("POST", url, json=data, headers=headers)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"HeyGen submit error: {r.text}")
    js = r.json()
    return (js.get("data") or {}).get("video_id") or js.get("video_id") or js.get("request_id") or ""

def heygen_status(video_id: str) -> Dict[str, Any]:
    url = f"https://api.heygen.com/v1/video.status?video_id={video_id}"
    headers = {"X-Api-Key": HEYGEN_KEY}
    r = _req("GET", url, headers=headers)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"HeyGen status error: {r.text}")
    return r.json()

def _extract_video_url(status_json: Dict[str, Any]) -> Optional[str]:
    data = status_json.get("data") or {}
    return data.get("video_url") or data.get("download_url") or data.get("url")

def poll_and_notify_heygen(job: Job, video_id: str, max_wait_sec: int = 900, every_sec: int = 5):
    waited = 0
    while waited <= max_wait_sec:
        st = heygen_status(video_id)
        data = st.get("data") or {}
        status = data.get("status")
        dbg("poll", video_id, status)
        if status in ("completed", "succeeded", "success"):
            url = _extract_video_url(st)
            if url:
                html = f"""
                <p>Ciao! 👋</p>
                <p>Il tuo <b>Video Parlante AI</b> è pronto.</p>
                <p><a href="{url}" target="_blank">Scarica/guarda il video qui</a></p>
                <p>Grazie da Eccomi OnLine!</p>
                """
                send_email(job.to_email, f"Video AI pronto — Ordine {job.order_name or ''}", html)
            return
        if status in ("failed", "error"):
            send_email(job.to_email,
                       f"Problema con il tuo Video AI — Ordine {job.order_name or ''}",
                       "<p>Si è verificato un errore durante la generazione. Ti contatteremo a breve.</p>")
            return
        time.sleep(every_sec)
        waited += every_sec
    send_email(job.to_email,
               f"Stiamo completando il tuo Video AI — Ordine {job.order_name or ''}",
               "<p>La generazione richiede più tempo del previsto. Ti avviseremo non appena sarà pronto.</p>")

# ===== ENDPOINTS =====
@app.get("/api/health")
def health():
    return {"ok": True, "service": "EccomiVideoAutomation", "version": "1.0"}

@app.get("/api/diag/env")
def diag_env():
    # non mostro segreti, solo presenza
    return {
        "HEYGEN_API_KEY": bool(HEYGEN_KEY),
        "HEYGEN_AVATAR_ID": bool(HEYGEN_AVATAR),
        "HEYGEN_VOICE_ID": HEYGEN_VOICE_ID,
        "RESEND_API_KEY": bool(RESEND_KEY),
        "FROM_EMAIL": FROM_EMAIL,
        "DEBUG": DEBUG,
    }

@app.post("/api/diag/heygen-submit")
def diag_heygen_submit(body: Dict[str, Any]):
    # chiamata di test (test_mode=True) per verificare subito la key
    text = (body.get("text") or "Ciao dal test HeyGen!")[:200]
    voice = _pick_heygen_voice(body.get("voice"))
    vid = heygen_submit(script=text, audio_url=None, voice_id=voice, avatar_id=body.get("avatar_id") or HEYGEN_AVATAR, test_mode=True)
    return {"ok": True, "video_id": vid}

@app.post("/api/jobs")
async def create_job(job: Job, bg: BackgroundTasks):
    voice_id = _pick_heygen_voice(job.voice)
    video_id = heygen_submit(
        script=job.script,
        audio_url=job.audio_url,
        voice_id=voice_id,
        avatar_id=HEYGEN_AVATAR
    )
    if not video_id:
        raise HTTPException(500, "HeyGen non ha restituito un video_id")
    dbg("submitted", video_id)
    bg.add_task(poll_and_notify_heygen, job, video_id)
    return {"ok": True, "provider": "heygen", "video_id": video_id}

@app.get("/api/video/status")
def video_status(video_id: str):
    return heygen_status(video_id)

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
        voice_sel  = props.get("Voce") or None
        audio_url  = props.get("Audio")

        if not to_email or (not script and not audio_url):
            continue

        job = Job(image_url=image_url, script=script, voice=voice_sel, audio_url=audio_url,
                  to_email=to_email, order_name=str(order_name))

        v_id = heygen_submit(script=job.script, audio_url=job.audio_url,
                             voice_id=_pick_heygen_voice(job.voice), avatar_id=HEYGEN_AVATAR)
        if v_id:
            bg.add_task(poll_and_notify_heygen, job, v_id)
        jobs_created.append({"line_item_id": li.get("id"), "video_id": v_id})

    return {"ok": True, "provider": "heygen", "jobs": jobs_created}
