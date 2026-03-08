import os
import hmac
import hashlib
import base64
import time
import json
import uuid
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any

import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from supabase import create_client, Client


# =====================================================
# ENV
# =====================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")  # anon key ok per table+storage se RLS permette; meglio service role lato server
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")  # opzionale ma consigliato

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")

SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SUPABASE_INPUTS_BUCKET = os.getenv("SUPABASE_INPUTS_BUCKET", "inputs")
SUPABASE_VIDEOS_BUCKET = os.getenv("SUPABASE_VIDEOS_BUCKET", "videos")

SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

RUNPOD_POLL_MAX_SECONDS = int(os.getenv("RUNPOD_POLL_MAX_SECONDS", "3600"))
RUNPOD_POLL_INTERVAL_SECONDS = int(os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "8"))

# Reel: opzionale (scarico mp4, genero verticale, carico su Supabase, salvo reel_supabase_url)
MAKE_REEL = os.getenv("MAKE_REEL", "false").lower() == "true"

# Networking hardening
HTTP_TIMEOUT_SHORT = int(os.getenv("HTTP_TIMEOUT_SHORT", "30"))
HTTP_TIMEOUT_LONG = int(os.getenv("HTTP_TIMEOUT_LONG", "600"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))

# =====================================================
# SUPABASE
# =====================================================
supabase: Optional[Client] = None
if SUPABASE_URL and (SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY):
    # Preferisci service role se disponibile
    key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY
    supabase = create_client(SUPABASE_URL, key)
    print("✅ Supabase collegato (key:", "service-role" if SUPABASE_SERVICE_ROLE_KEY else "anon", ")")
else:
    print("⚠️ Supabase NON configurato: manca SUPABASE_URL o KEY")


# =====================================================
# UTILS
# =====================================================
def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def sanitize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # manteniamo ascii safe per edge-tts / worker
    text = text.encode("ascii", "ignore").decode("ascii", "ignore")
    return text.strip()[:4000]


def verify_hmac(request: Request, raw: bytes):
    if not VERIFY_SHOPIFY_HMAC:
        return
    if not SHOPIFY_WEBHOOK_SECRET:
        raise HTTPException(500, "SHOPIFY_WEBHOOK_SECRET missing")
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    received = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Invalid HMAC")


def normalize_plan(plan: Optional[str]) -> str:
    p = (plan or "").strip().lower()
    if p in ["base", "basic"]:
        return "base"
    if p in ["pro"]:
        return "pro"
    if p in ["ultra", "premium"]:
        return "ultra"
    return "base"


def normalize_gender(gender: Optional[str]) -> Optional[str]:
    if not gender:
        return None
    g = gender.strip().lower()
    if g in ["uomo", "male", "m", "maschio"]:
        return "male"
    if g in ["donna", "female", "f", "femmina"]:
        return "female"
    # se arriva già male/female ok, altrimenti None
    if g in ["male", "female"]:
        return g
    return None


def extract_plan_from_order(order_json: dict) -> str:
    """
    Cerca nelle line item properties un campo tipo:
    - Plan / Piano / plan / pacchetto
    - value: base | pro | ultra
    """
    for item in order_json.get("line_items", []):
        for prop in (item.get("properties") or []):
            name = (prop.get("name") or "").strip().lower()
            val = (prop.get("value") or "").strip().lower()
            if name in ["plan", "piano", "pacchetto"] and val:
                return normalize_plan(val)
    return "base"


def http_request_with_retries(method: str, url: str, **kwargs) -> requests.Response:
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = requests.request(method, url, **kwargs)
            return r
        except Exception as e:
            last_err = e
            print(f"⚠️ HTTP error attempt {attempt}/{HTTP_RETRIES} -> {url} -> {repr(e)}")
            time.sleep(1.5 * attempt)
    raise last_err  # type: ignore


# =====================================================
# STORAGE (Inputs)
# =====================================================
def upload_input_to_supabase(token: str, filename: str, content: bytes, content_type: str) -> Optional[str]:
    if not supabase:
        return None
    path = f"{token}/{filename}"
    supabase.storage.from_(SUPABASE_INPUTS_BUCKET).upload(
        path=path,
        file=content,
        file_options={"content-type": content_type, "x-upsert": "true"}
    )
    return supabase.storage.from_(SUPABASE_INPUTS_BUCKET).get_public_url(path)


# =====================================================
# REEL (Optional)
# =====================================================
def ffmpeg_exists() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def create_vertical_reel(input_mp4: str, output_mp4: str):
    # 1080x1920 padding; se vuoi crop diverso lo cambiamo
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_mp4,
        "-vf", "scale=1080:1080,pad=1080:1920:0:420:black",
        "-c:a", "copy",
        output_mp4
    ]
    subprocess.run(cmd, check=True)


def upload_video_bytes_to_supabase(object_name: str, content: bytes) -> Optional[str]:
    """
    Carica bytes direttamente su Supabase Storage in videos bucket (root).
    object_name: f"{token}.mp4" oppure f"{token}_reel.mp4"
    """
    if not supabase:
        return None
    supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).upload(
        path=object_name,
        file=content,
        file_options={"content-type": "video/mp4", "x-upsert": "true"}
    )
    return supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).get_public_url(object_name)


# =====================================================
# RUNPOD
# =====================================================
def poll_runpod(token: str, job_id: str):
    """
    Poll sincrono nel background task:
    - quando COMPLETED: legge output.video_url e aggiorna Supabase video_jobs -> done
    - opzionale: genera e carica reel
    """
    if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
        print("❌ RUNPOD env mancanti")
        if supabase:
            supabase.table("video_jobs").update({
                "status": "failed",
                "updated_at": now_iso()
            }).eq("evs_token", token).execute()
        return

    started = time.time()
    waited = 0

    while waited < RUNPOD_POLL_MAX_SECONDS:
        try:
            url = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}"
            r = http_request_with_retries(
                "GET",
                url,
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                timeout=HTTP_TIMEOUT_SHORT
            )
            data = r.json() if r.content else {}
            status = (data.get("status") or "").upper()
            print(f"🔎 RunPod status {token}: {status}")

            # ===============================
            # COMPLETED
            # ===============================
            if status == "COMPLETED":
                output = (data.get("output") or {}) if isinstance(data.get("output"), dict) else {}
                video_url = output.get("video_url") or output.get("url")

                if not video_url:
                    print("❌ COMPLETED ma senza video_url:", output)
                    if supabase:
                        supabase.table("video_jobs").update({
                            "status": "failed",
                            "updated_at": now_iso(),
                            "error": "completed_without_video_url"
                        }).eq("evs_token", token).execute()
                    return

                print("✅ Video ricevuto da RunPod:", video_url)

                # Delivery page
                delivery_page = f"{PUBLIC_BASE_URL}/video/{token}" if PUBLIC_BASE_URL else ""

                reel_public_url = None

                # ===============================
                # OPTIONAL: make reel
                # ===============================
                if MAKE_REEL:
                    if not ffmpeg_exists():
                        print("⚠️ MAKE_REEL=true ma ffmpeg non disponibile su Render -> salto reel")
                    else:
                        try:
                            tmp_mp4 = f"/tmp/{token}.mp4"
                            tmp_reel = f"/tmp/{token}_reel.mp4"

                            rr = http_request_with_retries("GET", video_url, timeout=HTTP_TIMEOUT_LONG)
                            rr.raise_for_status()

                            with open(tmp_mp4, "wb") as f:
                                f.write(rr.content)

                            create_vertical_reel(tmp_mp4, tmp_reel)

                            with open(tmp_reel, "rb") as f:
                                reel_bytes = f.read()

                            reel_name = f"{token}_reel.mp4"
                            reel_public_url = upload_video_bytes_to_supabase(reel_name, reel_bytes)
                            print("✅ Reel caricato:", reel_public_url)
                        except Exception as e:
                            print("⚠️ Errore reel:", repr(e))

# ===============================
# UPDATE SUPABASE TABLE
# ===============================
if supabase:
    payload = {
        "status": "done",
        "video_url": delivery_page or None,
        "video_supabase_url": video_url,
        "runpod_job_id": job_id,
        "updated_at": now_iso(),
        "processing_seconds": int(time.time() - started),
    }

    if reel_public_url:
        payload["reel_supabase_url"] = reel_public_url

    response = supabase.table("video_jobs").update(payload).eq("evs_token", token).execute()

    print("🔧 Supabase update response:", response)

print("✅ Supabase aggiornato:", token)
                return

            # ===============================
            # FAILED / CANCELLED
            # ===============================
            if status in ["FAILED", "CANCELLED"]:
                print("❌ RunPod job fallito:", token, "payload:", data)
                if supabase:
                    supabase.table("video_jobs").update({
                        "status": "failed",
                        "updated_at": now_iso(),
                        "error": f"runpod_{status.lower()}"
                    }).eq("evs_token", token).execute()
                return

        except Exception as e:
            print("⚠️ Polling error:", repr(e))

        time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
        waited += RUNPOD_POLL_INTERVAL_SECONDS

    # timeout
    print("⏰ RunPod polling timeout:", token)
    if supabase:
        supabase.table("video_jobs").update({
            "status": "failed",
            "updated_at": now_iso(),
            "error": "poll_timeout"
        }).eq("evs_token", token).execute()


def runpod_submit(token: str):
    """
    Legge job su Supabase e lancia RunPod /run.
    Poi avvia polling sincrono.
    """
    print("🚀 runpod_submit start:", token)

    if not supabase:
        print("❌ Supabase non configurato")
        return

    res = supabase.table("video_jobs").select("*").eq("evs_token", token).limit(1).execute()
    if not res.data:
        print("⚠️ Nessuna riga trovata per token:", token)
        return

    row = res.data[0]
    plan = normalize_plan(row.get("plan"))
    gender = row.get("gender") or "male"

    payload = {
        "input": {
            "token": token,
            "plan": plan,
            "image_url": row.get("photo_url"),
            "audio_url": row.get("audio_url"),
            "text": sanitize_text(row.get("script_text")),
            "gender": gender
        }
    }

    # se audio c'è, text può essere vuoto e gender irrilevante, ok.
    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}

    try:
        url = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run"
        r = http_request_with_retries("POST", url, headers=headers, json=payload, timeout=45)

        data = r.json() if r.content else {}
        job_id = (data or {}).get("id")

        if not job_id:
            print("❌ RunPod submit senza job_id:", r.status_code, r.text)
            supabase.table("video_jobs").update({
                "status": "failed",
                "updated_at": now_iso(),
                "error": "runpod_submit_no_job_id"
            }).eq("evs_token", token).execute()
            return

        print("🧠 RunPod job avviato:", job_id)

        supabase.table("video_jobs").update({
            "status": "processing",
            "runpod_job_id": job_id,
            "updated_at": now_iso()
        }).eq("evs_token", token).execute()

        poll_runpod(token, job_id)

    except Exception as e:
        print("❌ RunPod submit exception:", repr(e))
        supabase.table("video_jobs").update({
            "status": "failed",
            "updated_at": now_iso(),
            "error": "runpod_submit_exception"
        }).eq("evs_token", token).execute()


# =====================================================
# FASTAPI
# =====================================================
app = FastAPI(title="EVS FINAL (Eccomi Video Studio)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


@app.get("/")
def health():
    return {"status": "online", "ts": now_iso()}


@app.post("/evs/order")
async def receive_order(
    email: str = Form(...),
    photo: UploadFile = File(...),
    audio: Optional[UploadFile] = File(None),
    script_text: str = Form(""),
    gender: Optional[str] = Form(None),
    plan: Optional[str] = Form("base"),
    evs_token: Optional[str] = Form(None)
):
    """
    Crea token + salva input su Supabase Storage + upsert su tabella video_jobs.
    NON avvia runpod qui: parte dopo pagamento via webhook Shopify.
    """
    if not supabase:
        raise HTTPException(500, "Supabase non configurato")

    token = (evs_token or "").strip() or str(uuid.uuid4())
    plan = normalize_plan(plan)

    script_text = sanitize_text(script_text)
    has_audio = bool(audio and audio.filename)

    gender_norm = normalize_gender(gender)

    # regola: deve esserci testo oppure audio
    if not has_audio and not script_text:
        raise HTTPException(status_code=400, detail="Inserisci un testo oppure carica un audio.")

    # testo senza audio -> default male
    if script_text and not has_audio and not gender_norm:
        gender_norm = "male"

    # se audio presente, ignora text e gender
    if has_audio:
        script_text = ""
        gender_norm = None

    # upload foto
    photo_bytes = await photo.read()
    if not photo_bytes:
        raise HTTPException(status_code=400, detail="Foto mancante o non valida.")

    photo_url = upload_input_to_supabase(
        token=token,
        filename="photo.png",
        content=photo_bytes,
        content_type=photo.content_type or "image/png"
    )

    if not photo_url:
        raise HTTPException(500, "Upload foto fallito su Supabase")

    # upload audio opzionale
    audio_url = None
    if has_audio:
        audio_bytes = await audio.read()
        if not audio_bytes or len(audio_bytes) < 100:
            raise HTTPException(status_code=400, detail="File audio non valido o troppo piccolo.")

        audio_url = upload_input_to_supabase(
            token=token,
            filename="audio.wav",
            content=audio_bytes,
            content_type=audio.content_type or "audio/wav"
        )

    # DB upsert
    supabase.table("video_jobs").upsert({
        "evs_token": token,
        "customer_email": email,
        "plan": plan,
        "status": "waiting_payment",
        "gender": gender_norm,
        "script_text": script_text,
        "photo_url": photo_url,
        "audio_url": audio_url,
        "has_audio": bool(audio_url),
        "updated_at": now_iso()
    }).execute()

    return JSONResponse({"ok": True, "evs_token": token, "plan": plan, "photo_url": photo_url, "audio_url": audio_url})


@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):
    """
    Riceve ordine Shopify -> se paid -> avvia runpod_submit(token)
    Cerca token in line item properties (deve contenere "evs" e "token" nel nome proprietà)
    """
    if not supabase:
        raise HTTPException(500, "Supabase non configurato")

    raw = await request.body()
    verify_hmac(request, raw)

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    financial_status = (data.get("financial_status") or "").lower()
    if financial_status != "paid":
        return {"ok": True}

    detected_plan = extract_plan_from_order(data)
    order_id = str(data.get("id") or "")

    # per ogni line_item, cerca EVS Token
    launched_any = False

    for item in data.get("line_items", []):
        props = item.get("properties") or []
        for prop in props:
            name = (prop.get("name", "") or "").lower()
            val = (prop.get("value", "") or "").strip()

            if ("evs" in name) and ("token" in name) and val:
                tok = val
                launched_any = True

                # leggi stato attuale
                res = supabase.table("video_jobs").select("status").eq("evs_token", tok).limit(1).execute()
                if not res.data:
                    print("⚠️ Token non trovato in video_jobs:", tok)
                    continue

                current_status = (res.data[0].get("status") or "").lower()
                if current_status in ["processing", "done"]:
                    print("⛔ Job già avviato o completato:", tok)
                    continue

                # aggiorna a processing + plan
                supabase.table("video_jobs").update({
                    "status": "processing",
                    "plan": detected_plan,
                    "shopify_order_id": order_id,
                    "updated_at": now_iso()
                }).eq("evs_token", tok).execute()

                # avvia background runpod
                bg.add_task(runpod_submit, tok)

    return {"ok": True, "launched": launched_any}


@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):
    """
    Pagina consegna: riproduce token.mp4 da Supabase bucket videos.
    Se c'è _reel.mp4 e MAKE_REEL true, il link esiste.
    """
    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download" if PUBLIC_BASE_URL else f"/video/{token}/download"

    video_stream = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"
    reel_stream = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}_reel.mp4"

    return HTMLResponse(f"""
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <title>Eccomi Video Studio</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">

  <meta property="og:title" content="🎬 Video creato con Eccomi Video Studio">
  <meta property="og:description" content="Guarda questo video AI creato da una foto.">
  <meta property="og:type" content="video.other">
  <meta property="og:url" content="{PUBLIC_BASE_URL}/video/{token}">
  <meta property="og:image" content="{reel_stream}">
  <meta property="og:video" content="{video_stream}">
  <meta property="og:video:type" content="video/mp4">

  <style>
    body {{
      margin:0;
      font-family: Arial, Helvetica, sans-serif;
      background:#0b1b33;
      color:#fff;
      padding:24px 14px;
    }}
    .container {{
      max-width: 980px;
      margin: 0 auto;
      text-align:center;
    }}
    .card {{
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 16px;
      overflow:hidden;
      margin-top:14px;
    }}
    video {{
      width:100%;
      height:auto;
      display:block;
      background:#000;
    }}
    .actions {{
      display:flex;
      flex-wrap:wrap;
      justify-content:center;
      gap:10px;
      margin-top:16px;
    }}
    a.btn {{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      padding:12px 16px;
      border-radius: 12px;
      background:#fff;
      color:#0b1b33;
      text-decoration:none;
      font-weight:700;
      min-width: 180px;
    }}
    @media (max-width:520px) {{
      a.btn {{ min-width: 100%; }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <h1>🎬 Il tuo video è pronto</h1>

    <div class="card">
      <video controls autoplay playsinline preload="metadata">
        <source src="{video_stream}" type="video/mp4">
      </video>
    </div>

    <div class="actions">
      <a class="btn" href="{download_url}" download="eccomi-video-{token}.mp4">⬇ Scarica MP4</a>
      <a class="btn" href="{reel_stream}" download="eccomi-reel-{token}.mp4">📱 Scarica Reel</a>
      <a class="btn" href="https://api.whatsapp.com/send?text=Guarda%20questo%20video!%20{PUBLIC_BASE_URL}/video/{token}">📲 WhatsApp</a>
    </div>

    <div style="margin-top:18px;opacity:.75;font-size:13px;">
      Creato con Eccomi Video Studio • eccomionline.com
    </div>
  </div>
</body>
</html>
""")


@app.get("/video/{token}/download")
def video_download(token: str):
    video_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"
    return RedirectResponse(url=video_url, status_code=302)
