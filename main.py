import os, hmac, hashlib, base64, time, json, requests
from typing import Optional, Dict, Any
from datetime import datetime
from threading import Lock

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel
import resend

# =========================
# ENV & GLOBALS
# =========================
DID_KEY = os.getenv("D_ID_API_KEY", "")
RESEND_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Eccomi Video <onboarding@resend.dev>")

SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"

HEYGEN_KEY = os.getenv("HEYGEN_API_KEY", "")
HEYGEN_AVATAR = os.getenv("HEYGEN_AVATAR_ID", "")
HEYGEN_VOICE_ID = os.getenv("HEYGEN_VOICE_ID", "it_male_energetic")

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # Bearer <ADMIN_TOKEN> per /admin, /manual e /dashboard

if RESEND_KEY:
    resend.api_key = RESEND_KEY

# Jobs store (in-memory)
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = Lock()

def _jobs_upsert(job_id: str, data: dict):
    if not job_id:
        return
    with JOBS_LOCK:
        now = datetime.utcnow().isoformat() + "Z"
        base = JOBS.get(job_id, {})
        base.update(data)
        base.setdefault("created_at", now)
        base["updated_at"] = now
        JOBS[job_id] = base

def _jobs_list():
    with JOBS_LOCK:
        return sorted(JOBS.values(), key=lambda x: x.get("updated_at",""), reverse=True)

# =========================
# APP
# =========================
app = FastAPI(title="Eccomi Video Automation", version="2.2")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.eccomionline.com", "https://eccomionline.com", "*"],
    allow_methods=["*"],
    allow_headers=["*", "Authorization"],
)

# =========================
# AUTH (Bearer)
# =========================
def require_admin_header(authorization: str = Header(None)):
    if not ADMIN_TOKEN:
        raise HTTPException(500, "ADMIN_TOKEN non configurato")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "Unauthorized")
    return True

# =========================
# MODELS
# =========================
class Job(BaseModel):
    image_url: str
    script: Optional[str] = None
    voice: Optional[str] = "ms:it-IT-GiuseppeNeural"  # "ms:<VOICE>" | "eleven:<VOICE_ID>"
    audio_url: Optional[str] = None
    to_email: Optional[str] = None
    order_name: Optional[str] = None

class HeygenText(BaseModel):
    script: str
    avatar_id: Optional[str] = None
    voice_id: Optional[str] = None
    to_email: Optional[str] = None
    order_name: Optional[str] = None

class HeygenAudio(BaseModel):
    audio_url: str
    avatar_id: Optional[str] = None
    to_email: Optional[str] = None
    order_name: Optional[str] = None

# ordini diretti (senza Shopify)
class ManualPhotoReq(BaseModel):
    image_url: str
    script: Optional[str] = None
    audio_url: Optional[str] = None
    voice: Optional[str] = "ms:it-IT-GiuseppeNeural"
    to_email: Optional[str] = None
    order_name: Optional[str] = "ManualOrder"

class ManualAvatarTextReq(BaseModel):
    script: str
    avatar_id: Optional[str] = None
    voice_id: Optional[str] = None
    to_email: Optional[str] = None
    order_name: Optional[str] = "ManualOrder"

class ManualAvatarAudioReq(BaseModel):
    audio_url: str
    avatar_id: Optional[str] = None
    to_email: Optional[str] = None
    order_name: Optional[str] = "ManualOrder"

# =========================
# EMAIL
# =========================
def send_email(to_email: str, subject: str, html: str):
    if not RESEND_KEY:
        print("⚠️ RESEND_API_KEY mancante: salto invio email")
        return
    try:
        r = resend.Emails.send({"from": FROM_EMAIL, "to": to_email, "subject": subject, "html": html})
        print("✅ Email inviata:", r)
    except Exception as e:
        print("❌ ERRORE invio email:", e)

# =========================
# D-ID (Photo → Talking Video)
# =========================
def did_headers():
    if not DID_KEY:
        raise HTTPException(500, "D_ID_API_KEY mancante")
    token = base64.b64encode((DID_KEY + ":").encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

def make_did_payload(job: Job) -> Dict[str, Any]:
    payload = {"source_url": job.image_url, "config": {"stitch": True}}
    if job.audio_url:
        payload["audio_url"] = job.audio_url
    else:
        if (job.voice or "").startswith("eleven:"):
            provider = {"type": "elevenlabs", "voice_id": job.voice.split(":", 1)[1]}
        else:
            voice_id = job.voice.split(":", 1)[1] if ":" in (job.voice or "") else "it-IT-GiuseppeNeural"
            provider = {"type": "microsoft", "voice_id": voice_id}
        payload["script"] = {"type": "text", "input": job.script or "Ciao! Il tuo video è pronto.", "provider": provider}
    return payload

def did_create_talk(job: Job) -> Dict[str, Any]:
    try:
        r = requests.post("https://api.d-id.com/talks", headers=did_headers(), json=make_did_payload(job), timeout=90)
    except requests.RequestException as e:
        raise HTTPException(502, f"D-ID non raggiungibile: {e}") from e
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, f"D-ID create error: {r.text}")
    return r.json()

def did_status(talk_id: str) -> Dict[str, Any]:
    try:
        r = requests.get(f"https://api.d-id.com/talks/{talk_id}", headers=did_headers(), timeout=60)
    except requests.RequestException as e:
        raise HTTPException(502, f"D-ID non raggiungibile: {e}") from e
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"D-ID status error: {r.text}")
    return r.json()

def poll_and_notify_did(job: Job, talk_id: str, max_wait_sec: int = 600, every_sec: int = 5):
    _jobs_upsert(talk_id, {"id": talk_id, "provider": "d-id", "status": "queued",
                           "to_email": job.to_email, "order_name": job.order_name})
    waited = 0
    while waited <= max_wait_sec:
        s = did_status(talk_id)
        st = s.get("status")
        video_url = s.get("result_url")
        _jobs_upsert(talk_id, {"status": st or "", "video_url": video_url, "raw": s})
        if st == "done" and video_url:
            if job.to_email:
                html = (
                    '<p>Ciao! 👋</p><p>Il tuo <b>Video Parlante AI</b> è pronto.</p>'
                    f'<p><a href="{video_url}" target="_blank">Scarica il video</a></p>'
                )
                send_email(job.to_email, f"Video AI pronto — Ordine {job.order_name or ''}", html)
            return
        if st in ("error", "failed"):
            if job.to_email:
                send_email(job.to_email, f"Problema con il tuo Video AI — Ordine {job.order_name or ''}",
                           "<p>Si è verificato un errore. Ti contatteremo a breve.</p>")
            return
        time.sleep(every_sec)
        waited += every_sec
    if job.to_email:
        send_email(job.to_email, f"Stiamo completando il tuo Video AI — Ordine {job.order_name or ''}",
                   "<p>La generazione richiede più tempo del previsto. Ti avviseremo appena pronto.</p>")

# =========================
# HEYGEN (Avatar → Talking Video)
# =========================
def _heygen_headers():
    if not HEYGEN_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")
    return {"X-Api-Key": HEYGEN_KEY, "Content-Type": "application/json"}

def _ensure_avatar(aid: Optional[str]) -> str:
    aid = aid or HEYGEN_AVATAR
    if not aid:
        raise HTTPException(500, "HEYGEN_AVATAR_ID mancante")
    return aid

def heygen_submit_text(script: str, avatar_id: Optional[str] = None, voice_id: Optional[str] = None) -> str:
    aid = _ensure_avatar(avatar_id)
    payload = {
        "video_inputs": [{
            "avatar_id": aid,
            "voice": {"type": "text", "input_text": script, "voice_id": (voice_id or HEYGEN_VOICE_ID)}
        }],
        "test": False, "caption": False, "aspect_ratio": "9:16", "resolution": "720p"
    }
    try:
        r = requests.post("https://api.heygen.com/v2/video/generate", headers=_heygen_headers(), json=payload, timeout=120)
    except requests.RequestException as e:
        raise HTTPException(502, f"HeyGen non raggiungibile: {e}") from e
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"HeyGen v2 submit error: {r.text}")
    data = r.json().get("data", {})
    vid = data.get("video_id") or data.get("id")
    if not vid:
        raise HTTPException(502, f"HeyGen v2: risposta senza video_id: {r.text}")
    return vid

def heygen_submit_audio(audio_url: str, avatar_id: Optional[str] = None) -> str:
    aid = _ensure_avatar(avatar_id)
    payload = {
        "video_inputs": [{
            "avatar_id": aid,
            "audio": {"type": "mp3", "source": "url", "url": audio_url}
        }],
        "test": False, "caption": False, "aspect_ratio": "9:16", "resolution": "720p"
    }
    try:
        r = requests.post("https://api.heygen.com/v2/video/generate", headers=_heygen_headers(), json=payload, timeout=120)
    except requests.RequestException as e:
        raise HTTPException(502, f"HeyGen non raggiungibile: {e}") from e
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"HeyGen v2 submit-audio error: {r.text}")
    data = r.json().get("data", {})
    vid = data.get("video_id") or data.get("id")
    if not vid:
        raise HTTPException(502, f"HeyGen v2: risposta senza video_id: {r.text}")
    return vid

def heygen_status(video_id: str) -> Dict[str, Any]:
    try:
        r = requests.get(f"https://api.heygen.com/v2/video/status?video_id={video_id}", headers=_heygen_headers(), timeout=60)
    except requests.RequestException as e:
        raise HTTPException(502, f"HeyGen non raggiungibile: {e}") from e
    if r.status_code == 200:
        return r.json()
    # fallback v1
    try:
        r2 = requests.get(f"https://api.heygen.com/v1/video.status?video_id={video_id}", headers=_heygen_headers(), timeout=60)
    except requests.RequestException as e:
        raise HTTPException(502, f"HeyGen non raggiungibile (v1): {e}") from e
    if r2.status_code == 200:
        return r2.json()
    raise HTTPException(502, f"HeyGen status error: v2={r.status_code} {r.text} | v1={r2.status_code} {r2.text}")

def poll_and_notify_heygen(video_id: str, to_email: Optional[str], order_name: Optional[str] = None,
                           every_sec: int = 7, max_wait_sec: int = 1200):
    _jobs_upsert(video_id, {"id": video_id, "provider": "heygen", "status": "queued",
                            "to_email": to_email, "order_name": order_name})
    waited = 0
    while waited <= max_wait_sec:
        try:
            s = heygen_status(video_id)
        except HTTPException as e:
            _jobs_upsert(video_id, {"status": f"error:{e.status_code}", "raw": str(e.detail)})
            time.sleep(every_sec); waited += every_sec
            continue
        data = s.get("data") or s
        status = (data.get("status") or data.get("task_status") or "").lower()
        video_url = (data.get("video") or {}).get("url") or data.get("video_url")
        _jobs_upsert(video_id, {"status": status, "video_url": video_url, "raw": s})
        if status in {"completed", "done", "succeeded"} and video_url:
            if to_email:
                html = (
                    '<p>Ciao! 👋</p><p>Il tuo <b>Video Avatar</b> è pronto.</p>'
                    f'<p><a href="{video_url}" target="_blank">Scarica il video</a></p>'
                )
                send_email(to_email, f"Video Avatar pronto — Ordine {order_name or ''}", html)
            return
        if status in {"failed", "error", "canceled"}:
            if to_email:
                send_email(to_email, f"Problema con il tuo Video Avatar — Ordine {order_name or ''}",
                           "<p>Si è verificato un errore. Ti contatteremo a breve.</p>")
            return
        time.sleep(every_sec); waited += every_sec
    if to_email:
        send_email(to_email, f"Stiamo completando il tuo Video Avatar — Ordine {order_name or ''}",
                   "<p>La generazione richiede più tempo del previsto. Ti avviseremo appena pronto.</p>")

# =========================
# HMAC Shopify
# =========================
def verify_shopify_hmac(request: Request, raw_body: bytes):
    if not VERIFY_SHOPIFY_HMAC or not SHOPIFY_WEBHOOK_SECRET:
        return True
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode() == hmac_header

# =========================
# META & DIAG
# =========================
@app.get("/", tags=["meta"])
def root():
    return {"ok": True, "service": "EccomiVideoAutomation", "version": "2.2",
            "health": "/api/health", "docs": "/docs", "dashboard": "/dashboard"}

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return Response(status_code=204)

@app.get("/api/health")
def health(): return {"ok": True, "service": "EccomiVideoAutomation", "version": "2.2"}

@app.get("/api/diag/env")
def diag_env():
    return {
        "D_ID_API_KEY": bool(DID_KEY),
        "RESEND_API_KEY": bool(RESEND_KEY),
        "FROM_EMAIL": FROM_EMAIL,
        "HEYGEN_API_KEY": bool(HEYGEN_KEY),
        "HEYGEN_AVATAR_ID": bool(HEYGEN_AVATAR),
        "HEYGEN_VOICE_ID": HEYGEN_VOICE_ID,
        "ELEVENLABS_API_KEY": bool(ELEVEN_KEY),
        "ELEVENLABS_VOICE_ID": bool(ELEVEN_VOICE_ID),
        "ADMIN_TOKEN": bool(ADMIN_TOKEN),
    }

# =========================
# PIPELINE D-ID: API
# =========================
@app.post("/api/jobs/photo")
@app.post("/api/jobs")  # compat
async def create_job_photo(job: Job, bg: BackgroundTasks):
    try:
        talk = did_create_talk(job)
    except HTTPException as e:
        if e.status_code == 401:
            raise HTTPException(401, "D-ID non autorizzato: controlla D_ID_API_KEY") from e
        raise
    talk_id = talk.get("id")
    if talk_id:
        _jobs_upsert(talk_id, {"id": talk_id, "provider": "d-id", "status": "submitted",
                               "to_email": job.to_email, "order_name": job.order_name, "raw": talk})
        bg.add_task(poll_and_notify_did, job, talk_id)
    return {"ok": True, "provider": "d-id", "talk_id": talk_id, "raw": talk}

# =========================
# PIPELINE HEYGEN: API
# =========================
@app.post("/api/heygen/submit")
def heygen_submit_endpoint(body: HeygenText, bg: BackgroundTasks):
    vid = heygen_submit_text(body.script, body.avatar_id, body.voice_id)
    _jobs_upsert(vid, {"id": vid, "provider": "heygen", "status": "submitted",
                       "to_email": body.to_email, "order_name": body.order_name})
    if body.to_email:
        bg.add_task(poll_and_notify_heygen, vid, body.to_email, body.order_name)
    return {"ok": True, "provider": "heygen", "video_id": vid}

@app.post("/api/heygen/submit-audio")
def heygen_submit_audio_endpoint(body: HeygenAudio, bg: BackgroundTasks):
    vid = heygen_submit_audio(body.audio_url, body.avatar_id)
    _jobs_upsert(vid, {"id": vid, "provider": "heygen", "status": "submitted",
                       "to_email": body.to_email, "order_name": body.order_name})
    if body.to_email:
        bg.add_task(poll_and_notify_heygen, vid, body.to_email, body.order_name)
    return {"ok": True, "provider": "heygen", "video_id": vid}

@app.get("/api/heygen/status")
def heygen_status_endpoint(video_id: str = Query(..., min_length=4)):
    if video_id in {"INSERISCI_ID", "QUI_IL_VIDEO_ID"}:
        raise HTTPException(400, "video_id è un placeholder: usa l’ID reale restituito da /api/heygen/submit")
    return heygen_status(video_id)

# =========================
# SHOPIFY WEBHOOK
# =========================
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
            voice_id = HEYGEN_VOICE_ID if str(voice_sel).lower().startswith("uomo") else "it_female_calm"
            if audio_url:
                video_id = heygen_submit_audio(audio_url, None)
            else:
                if not script:
                    continue
                video_id = heygen_submit_text(script, None, voice_id)
            _jobs_upsert(video_id, {"id": video_id, "provider": "heygen", "status": "submitted",
                                    "to_email": to_email, "order_name": str(order_name)})
            bg.add_task(poll_and_notify_heygen, video_id, to_email, str(order_name))
            jobs_created.append({"line_item_id": li.get("id"), "provider": "heygen", "video_id": video_id})
        else:
            if not image_url or (not script and not audio_url) or not to_email:
                continue
            voice = "ms:it-IT-GiuseppeNeural"
            if str(voice_sel).lower().startswith("don"): voice = "ms:it-IT-IsabellaNeural"
            if str(voice_sel).startswith("eleven:"):     voice = voice_sel
            job = Job(image_url=image_url, script=script, voice=voice, audio_url=audio_url,
                      to_email=to_email, order_name=str(order_name))
            talk = did_create_talk(job)
            talk_id = talk.get("id")
            if talk_id:
                _jobs_upsert(talk_id, {"id": talk_id, "provider": "d-id", "status": "submitted",
                                       "to_email": to_email, "order_name": str(order_name), "raw": talk})
                bg.add_task(poll_and_notify_did, job, talk_id)
            jobs_created.append({"line_item_id": li.get("id"), "provider": "d-id", "talk_id": talk_id})

    return {"ok": True, "jobs": jobs_created}

# =========================
# ADMIN API (Bearer)
# =========================
@app.get("/api/admin/jobs")
def admin_jobs(_: bool = Depends(require_admin_header)):
    return {"ok": True, "jobs": _jobs_list()}

@app.get("/api/admin/jobs/{job_id}")
def admin_job_detail(job_id: str, _: bool = Depends(require_admin_header)):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j: raise HTTPException(404, "Job not found")
    return {"ok": True, "job": j}

@app.post("/api/admin/resend-email/{job_id}")
def admin_resend_email(job_id: str, _: bool = Depends(require_admin_header)):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j: raise HTTPException(404, "Job not found")
    if not j.get("to_email"): raise HTTPException(400, "Job senza email")
    if not j.get("video_url"): raise HTTPException(400, "Video non pronto")
    video_url = j["video_url"]
    html = (
        '<p>Ciao! 👋</p><p>Il tuo video è pronto.</p>'
        f'<p><a href="{video_url}" target="_blank">Scarica</a></p>'
    )
    send_email(j["to_email"], f"Video pronto — Ordine {j.get('order_name','')}", html)
    return {"ok": True, "resent": True}

# =========================
# DASHBOARD + CREATOR PANEL
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return """
<!doctype html><html lang="it"><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Eccomi Video — Dashboard</title>
<style>
body{font-family:system-ui,Inter,sans-serif;margin:24px;max-width:1100px}
h1{margin:0 0 12px}
section{margin:16px 0;padding:12px;border:1px solid #e5e7eb;border-radius:8px;background:#fafafa}
label{display:block;margin:6px 0 2px}
input,select,textarea{width:100%;padding:8px;border:1px solid #ddd;border-radius:6px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.btn{padding:.45rem .8rem;border:1px solid #ddd;border-radius:.4rem;background:white;cursor:pointer}
.btn:disabled{opacity:.5;cursor:not-allowed}
.badge{padding:.2rem .5rem;border-radius:.4rem;background:#eee}
table{border-collapse:collapse;width:100%;margin-top:12px}
td,th{border:1px solid #e5e7eb;padding:8px;font-size:14px;vertical-align:top}
tr:hover{background:#fafafa}
small{color:#666}
hr{border:none;border-top:1px solid #eee;margin:12px 0}
</style>

<h1>Eccomi Video — Dashboard</h1>
<p><small>Auto-refresh 6s · <a href="#" id="refresh">Aggiorna ora</a></small></p>

<section id="creator">
  <h3>Crea nuovo lavoro</h3>
  <div class="grid3">
    <div>
      <label>Tipo</label>
      <select id="type">
        <option value="did-photo">Foto parlante (D-ID)</option>
        <option value="hg-text">Avatar Heygen (testo)</option>
        <option value="hg-audio">Avatar Heygen (audio URL)</option>
      </select>
    </div>
    <div>
      <label>Ordine (facoltativo)</label>
      <input id="order_name" placeholder="es. Ordine #1234">
    </div>
    <div>
      <label>Email destinatario (facoltativo per test)</label>
      <input id="to_email" placeholder="es. info@eccomionline.com">
    </div>
  </div>

  <div id="did-fields">
    <div class="grid">
      <div>
        <label>Image URL (obbligatorio)</label>
        <input id="image_url" placeholder="https://...jpg/png">
      </div>
      <div>
        <label>Voce (ms:&lt;VOICE&gt; / eleven:&lt;VOICE_ID&gt;)</label>
        <input id="voice" value="ms:it-IT-GiuseppeNeural">
      </div>
    </div>
    <label>Script (obbligatorio se non passi audio_url)</label>
    <textarea id="script" rows="3" placeholder="Testo da pronunciare"></textarea>
    <label>Audio URL (opzionale; se presente ignora Script)</label>
    <input id="audio_url" placeholder="https://...mp3">
  </div>

  <div id="hg-text-fields" style="display:none">
    <label>Script (obbligatorio)</label>
    <textarea id="hg_script" rows="3" placeholder="Testo per l'avatar Heygen"></textarea>
    <div class="grid">
      <div>
        <label>Avatar ID (facoltativo: usa default)</label>
        <input id="hg_avatar">
      </div>
      <div>
        <label>Voice ID (facoltativo)</label>
        <input id="hg_voice" placeholder="it_male_energetic">
      </div>
    </div>
  </div>

  <div id="hg-audio-fields" style="display:none">
    <div class="grid">
      <div>
        <label>Audio URL (obbligatorio)</label>
        <input id="hg_audio_url" placeholder="https://...mp3">
      </div>
      <div>
        <label>Avatar ID (facoltativo: usa default)</label>
        <input id="hg_avatar2">
      </div>
    </div>
  </div>

  <hr/>
  <button class="btn" id="create">Crea lavoro</button>
  <span id="msg" style="margin-left:8px"></span>
</section>

<table id="jobs"><thead>
<tr><th>ID</th><th>Provider</th><th>Status</th><th>Video</th><th>Email</th><th>Ordine</th><th>Azioni</th></tr>
</thead><tbody></tbody></table>

<script>
// ==== Token handling ====
function askToken(){
  let tk = new URLSearchParams(location.search).get("token");
  if(!tk) tk = sessionStorage.getItem("eccomi_admin_token") || "";
  if(!tk){ tk = prompt("Inserisci ADMIN_TOKEN"); }
  if(!tk){ document.body.innerHTML = "<p>Token mancante.</p>"; return null; }
  sessionStorage.setItem("eccomi_admin_token", tk);
  return tk;
}
const token = askToken();
if(!token) throw new Error("No token");

// ==== UI toggle per i campi ====
const selType = document.getElementById('type');
const didBox = document.getElementById('did-fields');
const hgTextBox = document.getElementById('hg-text-fields');
const hgAudioBox = document.getElementById('hg-audio-fields');
function updateFields(){
  const t = selType.value;
  didBox.style.display = (t==='did-photo')?'block':'none';
  hgTextBox.style.display = (t==='hg-text')?'block':'none';
  hgAudioBox.style.display = (t==='hg-audio')?'block':'none';
}
selType.onchange = updateFields; updateFields();

// ==== Loader tabella ====
async function load(){
  const r = await fetch('/api/admin/jobs', { headers:{ Authorization:`Bearer ${token}` }});
  if(!r.ok){ document.body.innerHTML = "<p>Unauthorized</p>"; return; }
  const data = await r.json();
  const tbody = document.querySelector("#jobs tbody");
  tbody.innerHTML = "";
  for(const j of (data.jobs||[])){
    const tr = document.createElement("tr");
    const v = j.video_url ? `<a href="${j.video_url}" target="_blank">apri</a>` : "";
    tr.innerHTML = `
      <td><code>${j.id||""}</code><br><small>${j.updated_at||""}</small></td>
      <td>${j.provider||""}</td>
      <td><span class="badge">${j.status||""}</span></td>
      <td>${v}</td>
      <td>${j.to_email||""}</td>
      <td>${j.order_name||""}</td>
      <td><button class="btn" ${j.video_url?"":"disabled"} onclick="resend('${j.id}')">Re-invia email</button></td>
    `;
    tbody.appendChild(tr);
  }
}
document.getElementById("refresh").onclick = (e)=>{ e.preventDefault(); load(); };
setInterval(load, 6000); load();

// ==== Azione: re-invio email ====
async function resend(id){
  const r = await fetch(`/api/admin/resend-email/${encodeURIComponent(id)}`, {
    method:"POST", headers:{ Authorization:`Bearer ${token}` }
  });
  alert(r.ok ? "Email inviata" : "Errore reinvio");
}
window.resend = resend;

// ==== Creator Panel: submit ====
function setMsg(text, ok=true){
  const el = document.getElementById('msg');
  el.textContent = text;
  el.style.color = ok ? "green" : "crimson";
}
document.getElementById('create').onclick = async ()=>{
  setMsg("Invio in corso...", true);
  const t = selType.value;
  const order_name = document.getElementById('order_name').value || "ManualOrder";
  const to_email = document.getElementById('to_email').value || "";

  let url, body;
  if(t === 'did-photo'){
    const image_url = document.getElementById('image_url').value.trim();
    const voice = document.getElementById('voice').value.trim() || "ms:it-IT-GiuseppeNeural";
    const script = document.getElementById('script').value.trim();
    const audio_url = document.getElementById('audio_url').value.trim();
    if(!image_url){ setMsg("Image URL obbligatorio", false); return; }
    if(!script && !audio_url){ setMsg("Scrivi uno script o passa un audio_url", false); return; }
    url = "/api/manual/photo";
    body = { image_url, voice, script: script||undefined, audio_url: audio_url||undefined,
             to_email: to_email||undefined, order_name };
  } else if(t === 'hg-text'){
    const script = document.getElementById('hg_script').value.trim();
    if(!script){ setMsg("Script obbligatorio", false); return; }
    url = "/api/manual/avatar-text";
    body = { script,
             avatar_id: (document.getElementById('hg_avatar').value||undefined),
             voice_id: (document.getElementById('hg_voice').value||undefined),
             to_email: to_email||undefined, order_name };
  } else { // hg-audio
    const audio_url = document.getElementById('hg_audio_url').value.trim();
    if(!audio_url){ setMsg("Audio URL obbligatorio", false); return; }
    url = "/api/manual/avatar-audio";
    body = { audio_url,
             avatar_id: (document.getElementById('hg_avatar2').value||undefined),
             to_email: to_email||undefined, order_name };
  }

  const r = await fetch(url, {
    method:"POST",
    headers:{ "Content-Type":"application/json", Authorization:`Bearer ${token}` },
    body: JSON.stringify(body)
  });
  const data = await r.json().catch(()=> ({}));
  if(!r.ok){
    setMsg("Errore: " + (data.detail || r.status), false);
    return;
  }
  setMsg("Creato! Aggiorno tabella…", true);
  load();
};
</script>
</html>
"""
