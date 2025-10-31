import os, hmac, hashlib, base64, time, json, requests
from typing import Optional, Dict, Any, List
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
FROM_EMAIL = os.getenv("FROM_EMAIL", "Eccomi Video <info@eccomionline.com>")

HEYGEN_KEY = os.getenv("HEYGEN_API_KEY", "")
HEYGEN_AVATAR = os.getenv("HEYGEN_AVATAR_ID", "")
HEYGEN_VOICE_ID = os.getenv("HEYGEN_VOICE_ID", "it_male_energetic")

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")  # es. eccomionline.myshopify.com
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")
SHOPIFY_API_VER = os.getenv("SHOPIFY_API_VER", "2025-10")

DATA_FILE = os.getenv("DATA_FILE", "/mnt/data/jobs.json")

if RESEND_KEY:
    resend.api_key = RESEND_KEY

# =========================
# STORAGE (persistenza leggera)
# =========================
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = Lock()

def _now_iso():
    return datetime.utcnow().isoformat() + "Z"

def _storage_load():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception as e:
        print("⚠️ storage load error:", e)
    return {}

def _storage_save():
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(JOBS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("⚠️ storage save error:", e)

def _jobs_upsert(job_id: str, data: dict):
    if not job_id:
        return
    with JOBS_LOCK:
        base = JOBS.get(job_id, {})
        base.update(data)
        base.setdefault("created_at", _now_iso())
        base["updated_at"] = _now_iso()
        JOBS[job_id] = base
        _storage_save()

def _jobs_list():
    with JOBS_LOCK:
        return sorted(JOBS.values(), key=lambda x: x.get("updated_at",""), reverse=True)

# carica a boot
JOBS.update(_storage_load())

# =========================
# APP
# =========================
app = FastAPI(title="Eccomi Video Automation", version="2.3")
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

# Manual orders (senza Shopify)
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
    r = requests.post("https://api.d-id.com/talks", headers=did_headers(), json=make_did_payload(job), timeout=90)
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, f"D-ID create error: {r.text}")
    return r.json()

def did_status(talk_id: str) -> Dict[str, Any]:
    r = requests.get(f"https://api.d-id.com/talks/{talk_id}", headers=did_headers(), timeout=60)
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
    r = requests.post("https://api.heygen.com/v2/video/generate", headers=_heygen_headers(), json=payload, timeout=120)
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
    r = requests.post("https://api.heygen.com/v2/video/generate", headers=_heygen_headers(), json=payload, timeout=120)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"HeyGen v2 submit-audio error: {r.text}")
    data = r.json().get("data", {})
    vid = data.get("video_id") or data.get("id")
    if not vid:
        raise HTTPException(502, f"HeyGen v2: risposta senza video_id: {r.text}")
    return vid

def heygen_status(video_id: str) -> Dict[str, Any]:
    r = requests.get(f"https://api.heygen.com/v2/video/status?video_id={video_id}", headers=_heygen_headers(), timeout=60)
    if r.status_code == 200:
        return r.json()
    r2 = requests.get(f"https://api.heygen.com/v1/video.status?video_id={video_id}", headers=_heygen_headers(), timeout=60)
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
# META & DIAG
# =========================
@app.get("/", tags=["meta"])
def root():
    return {"ok": True, "service": "EccomiVideoAutomation", "version": "2.3",
            "health": "/api/health", "docs": "/docs", "dashboard": "/dashboard"}

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return Response(status_code=204)

@app.get("/api/health")
def health(): return {"ok": True, "service": "EccomiVideoAutomation", "version": "2.3"}

@app.get("/api/diag/env")
def diag_env():
    return {
        "RESEND_API_KEY": bool(RESEND_KEY), "FROM_EMAIL": FROM_EMAIL,
        "D_ID_API_KEY": bool(DID_KEY),
        "HEYGEN_API_KEY": bool(HEYGEN_KEY), "HEYGEN_AVATAR_ID": bool(HEYGEN_AVATAR),
        "ELEVENLABS_API_KEY": bool(ELEVEN_KEY),
        "ADMIN_TOKEN": bool(ADMIN_TOKEN),
        "SHOP_DOMAIN": SHOP_DOMAIN, "SHOP_ADMIN_TOKEN": bool(SHOP_ADMIN_TOKEN),
        "DATA_FILE": DATA_FILE,
    }

# =========================
# PIPELINE D-ID: API
# =========================
@app.post("/api/jobs/photo")
@app.post("/api/jobs")  # compat
def create_job_photo(job: Job, bg: BackgroundTasks):
    talk = did_create_talk(job)
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
    html = (
        '<p>Ciao! 👋</p><p>Il tuo video è pronto.</p>'
        f'<p><a href="{j["video_url"]}" target="_blank">Scarica</a></p>'
    )
    send_email(j["to_email"], f"Video pronto — Ordine {j.get('order_name','')}", html)
    return {"ok": True, "resent": True}

# =========================
# SHOPIFY: CREATE PRODUCT
# =========================
def _shop_headers():
    if not (SHOP_DOMAIN and SHOP_ADMIN_TOKEN):
        raise HTTPException(500, "SHOP_DOMAIN/SHOP_ADMIN_TOKEN mancanti")
    return {
        "X-Shopify-Access-Token": SHOP_ADMIN_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def shopify_create_product(title: str, body_html: str, price: float, image_url: Optional[str] = None,
                           published: bool = True, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    payload = {
        "product": {
            "title": title,
            "body_html": body_html,
            "tags": ", ".join(tags or ["EccomiVideo"]),
            "status": "active" if published else "draft",
            "variants": [{"price": f"{price:.2f}"}]
        }
    }
    if image_url:
        payload["product"]["images"] = [{"src": image_url}]
    url = f"https://{SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VER}/products.json"
    r = requests.post(url, headers=_shop_headers(), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, f"Shopify create product error: {r.text}")
    return r.json()

@app.post("/api/admin/publish/{job_id}")
def admin_publish(job_id: str, price: float = 19.0, published: bool = True,
                  _: bool = Depends(require_admin_header)):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
    if not j: raise HTTPException(404, "Job not found")
    if not j.get("video_url"): raise HTTPException(400, "Video non pronto")

    title = f"Video AI — {j.get('provider','')}".strip()
    desc = []
    if j.get("order_name"): desc.append(f"<p><b>Ordine:</b> {j['order_name']}</p>")
    if j.get("to_email"):   desc.append(f"<p><b>Email cliente:</b> {j['to_email']}</p>")
    desc.append(f'<p><a href="{j["video_url"]}" target="_blank">Scarica il video</a></p>')
    body_html = "\n".join(desc)

    img = j.get("thumbnail") or None  # puoi valorizzarlo in futuro
    res = shopify_create_product(title=title, body_html=body_html, price=price, image_url=img, published=published)
    prod = (res or {}).get("product") or {}
    handle = prod.get("handle"); pid = prod.get("id")
    url = f"https://www.eccomionline.com/products/{handle}" if handle else ""
    _jobs_upsert(job_id, {"shopify_product_id": pid, "shopify_url": url})
    return {"ok": True, "product_id": pid, "product_url": url}

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
function askToken(){
  let tk = new URLSearchParams(location.search).get("token");
  if(!tk) tk = sessionStorage.getItem("eccomi_admin_token") || "";
  if(!tk){ tk = prompt("Inserisci ADMIN_TOKEN"); }
  if(!tk){ document.body.innerHTML = "<p>Token mancante.</p>"; return null; }
  sessionStorage.setItem("eccomi_admin_token", tk);
  return tk;
}
const token = askToken(); if(!token) throw new Error("No token");

// toggle campi
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

async function load(){
  const r = await fetch('/api/admin/jobs', { headers:{ Authorization:`Bearer ${token}` }});
  if(!r.ok){ document.body.innerHTML = "<p>Unauthorized</p>"; return; }
  const data = await r.json();
  const tbody = document.querySelector("#jobs tbody");
  tbody.innerHTML = "";
  for(const j of (data.jobs||[])){
    const tr = document.createElement("tr");
    const v = j.video_url ? `<a href="${j.video_url}" target="_blank">apri</a>` : "";
    const pub = j.video_url ? `<button class="btn" onclick="publish('${j.id}')">Pubblica</button>` : `<button class="btn" disabled>Pubblica</button>`;
    const resend = j.video_url ? `<button class="btn" onclick="resend('${j.id}')">Re-invia email</button>` : `<button class="btn" disabled>Re-invia email</button>`;
    tr.innerHTML = `
      <td><code>${j.id||""}</code><br><small>${j.updated_at||""}</small></td>
      <td>${j.provider||""}</td>
      <td><span class="badge">${j.status||""}</span></td>
      <td>${v}</td>
      <td>${j.to_email||""}</td>
      <td>${j.order_name||""}</td>
      <td>${pub} ${resend} ${j.shopify_url?('<a class="btn" target="_blank" href="'+j.shopify_url+'">Prodotto</a>'):''}</td>
    `;
    tbody.appendChild(tr);
  }
}
document.getElementById("refresh").onclick = (e)=>{ e.preventDefault(); load(); };
setInterval(load, 6000); load();

async function resend(id){
  const r = await fetch(`/api/admin/resend-email/${encodeURIComponent(id)}`, {
    method:"POST", headers:{ Authorization:`Bearer ${token}` }
  });
  alert(r.ok ? "Email inviata" : "Errore reinvio");
}

async function publish(id){
  const prezzo = prompt("Prezzo prodotto (€)", "19");
  if(!prezzo) return;
  const r = await fetch(`/api/admin/publish/${encodeURIComponent(id)}?price=${encodeURIComponent(prezzo)}`, {
    method:"POST", headers:{ Authorization:`Bearer ${token}` }
  });
  const data = await r.json().catch(()=> ({}));
  if(r.ok){
    alert("Pubblicato ✅");
    load();
  }else{
    alert("Errore pubblicazione: " + (data.detail || r.status));
  }
}

window.resend = resend;
window.publish = publish;

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
    url = "/api/jobs/photo";
    body = { image_url, voice, script: script||undefined, audio_url: audio_url||undefined,
             to_email: to_email||undefined, order_name };
  } else if(t === 'hg-text'){
    const script = document.getElementById('hg_script').value.trim();
    if(!script){ setMsg("Script obbligatorio", false); return; }
    url = "/api/heygen/submit";
    body = { script,
             avatar_id: (document.getElementById('hg_avatar').value||undefined),
             voice_id: (document.getElementById('hg_voice').value||undefined),
             to_email: to_email||undefined, order_name };
  } else { // hg-audio
    const audio_url = document.getElementById('hg_audio_url').value.trim();
    if(!audio_url){ setMsg("Audio URL obbligatorio", false); return; }
    url = "/api/heygen/submit-audio";
    body = { audio_url,
             avatar_id: (document.getElementById('hg_avatar2').value||undefined),
             to_email: to_email||undefined, order_name };
  }

  const r = await fetch(url, {
    method:"POST",
    headers:{ "Content-Type":"application/json" },
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
