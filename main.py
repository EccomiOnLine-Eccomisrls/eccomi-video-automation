import os
import hmac
import hashlib
import base64
import time
import json
import requests
import uuid
import subprocess

from typing import Optional
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

from supabase import create_client, Client

# =====================================================
# ENV
# =====================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")

SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SUPABASE_INPUTS_BUCKET = os.getenv("SUPABASE_INPUTS_BUCKET", "inputs")
SUPABASE_VIDEOS_BUCKET = os.getenv("SUPABASE_VIDEOS_BUCKET", "videos")

RUNPOD_POLL_MAX_SECONDS = int(os.getenv("RUNPOD_POLL_MAX_SECONDS", "3600"))
RUNPOD_POLL_INTERVAL_SECONDS = int(os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "8"))

# =====================================================
# SUPABASE
# =====================================================
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Supabase collegato")

# =====================================================
# UTILS
# =====================================================
def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def sanitize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.encode("ascii", "ignore").decode("ascii", "ignore")
    return text.strip()[:4000]

def verify_hmac(request: Request, raw: bytes):
    if not VERIFY_SHOPIFY_HMAC:
        return
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode(),
        raw,
        hashlib.sha256
    ).digest()
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

def extract_plan_from_order(order_json: dict) -> str:
    """
    Cerca nelle line item properties un campo tipo:
    - Plan / Piano / plan
    - value: base | pro | ultra
    """
    for item in order_json.get("line_items", []):
        for prop in (item.get("properties") or []):
            name = (prop.get("name") or "").strip().lower()
            val  = (prop.get("value") or "").strip().lower()
            if name in ["plan", "piano", "pacchetto"] and val:
                return normalize_plan(val)
    return "base"

# =====================================================
# STORAGE
# =====================================================
def upload_input_to_supabase(token, filename, content, content_type):
    if not supabase:
        return None
    path = f"{token}/{filename}"
    supabase.storage.from_(SUPABASE_INPUTS_BUCKET).upload(
        path=path,
        file=content,
        file_options={"content-type": content_type, "x-upsert": "true"}
    )
    return supabase.storage.from_(SUPABASE_INPUTS_BUCKET).get_public_url(path)

def upload_video_to_supabase_object(object_name: str, content: bytes):
    """
    Carica bytes direttamente su Supabase Storage in videos bucket.
    object_name: es. f"{token}.mp4" oppure f"{token}_reel.mp4"
    """
    if not supabase:
        return None
    supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).upload(
        path=object_name,
        file=content,
        file_options={"content-type": "video/mp4", "x-upsert": "true"}
    )
    return supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).get_public_url(object_name)

def create_vertical_video(token):
    input_file = f"/tmp/{token}.mp4"
    output_file = f"/tmp/{token}_reel.mp4"
    cmd = [
        "ffmpeg",
        "-i", input_file,
        "-vf", "scale=1080:1080,pad=1080:1920:0:420:black",
        "-c:a", "copy",
        output_file
    ]
    subprocess.run(cmd, check=True)
    return output_file

# =====================================================
# RUNPOD
# =====================================================
def poll_runpod(token: str, job_id: str):
    waited = 0

    while waited < RUNPOD_POLL_MAX_SECONDS:
        try:
            r = requests.get(
                f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}",
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                timeout=30
            )

            data = r.json()
            status = (data.get("status") or "").upper()

            print(f"🔎 RunPod status {token}: {status}")

            # ===============================
            # JOB COMPLETATO
            # ===============================
            if status == "COMPLETED":

                output = data.get("output", {}) or {}
                video_url = output.get("video_url") or output.get("url")

                if not video_url:
                    print("❌ COMPLETED ma senza video_url:", output)

                    if supabase:
                        supabase.table("video_jobs").update({
                            "status": "failed",
                            "updated_at": now_iso()
                        }).eq("evs_token", token).execute()

                    return

                print("✅ Video ricevuto da RunPod:", video_url)

                delivery_page = f"{PUBLIC_BASE_URL}/video/{token}"

                if supabase:
                    supabase.table("video_jobs").update({
                        "status": "done",
                        "video_url": delivery_page,
                        "video_supabase_url": video_url,
                        "runpod_job_id": job_id,
                        "updated_at": now_iso()
                    }).eq("evs_token", token).execute()

                print("✅ Supabase aggiornato:", token)

                return

            # ===============================
            # JOB FALLITO
            # ===============================
            if status in ["FAILED", "CANCELLED"]:

                print("❌ RunPod job fallito:", token)

                if supabase:
                    supabase.table("video_jobs").update({
                        "status": "failed",
                        "updated_at": now_iso()
                    }).eq("evs_token", token).execute()

                return

        except Exception as e:
            print("⚠️ Polling error:", repr(e))

        time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
        waited += RUNPOD_POLL_INTERVAL_SECONDS

def runpod_submit(tok, name, email):

    print("🚀 runpod_submit start:", tok)

    if not supabase:
        print("❌ Supabase non configurato")
        return

    res = supabase.table("video_jobs").select("*").eq("evs_token", tok).limit(1).execute()

    if not res.data:
        print("⚠️ Nessuna riga trovata per token:", tok)
        return

    row = res.data[0]

    plan = normalize_plan(row.get("plan"))

    payload = {
        "input": {
            "image_url": row.get("photo_url"),
            "text": sanitize_text(row.get("script_text")),
            "gender": (row.get("gender") or "male"),
            "audio_url": row.get("audio_url"),
            "token": tok,
            "plan": plan
        }
    }

    headers = {
        "Authorization": f"Bearer {RUNPOD_API_KEY}",
        "Content-Type": "application/json"
    }

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
        headers=headers,
        json=payload,
        timeout=45
    )

    job_id = (r.json() or {}).get("id")

    if job_id:

        print("🧠 RunPod job avviato:", job_id)

        supabase.table("video_jobs").update({
            "status": "processing",
            "runpod_job_id": job_id,
            "updated_at": now_iso()
        }).eq("evs_token", tok).execute()

        poll_runpod(tok, job_id)

    else:

        print("❌ RunPod submit senza job_id:", r.text)

        supabase.table("video_jobs").update({
            "status": "failed",
            "updated_at": now_iso()
        }).eq("evs_token", tok).execute()

# =====================================================
# FASTAPI
# =====================================================
app = FastAPI(title="EVS FINAL")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def health():
    return {"status": "online"}

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
    token = (evs_token or "").strip() or str(uuid.uuid4())
    plan = normalize_plan(plan)

    script_text = sanitize_text(script_text)
    has_audio = bool(audio and audio.filename)

    # NORMALIZZA GENDER
    if gender:
        g = gender.strip().lower()
        if g in ["uomo", "male", "m", "maschio"]:
            gender = "male"
        elif g in ["donna", "female", "f", "femmina"]:
            gender = "female"
        else:
            gender = None

    if not has_audio and not script_text:
        raise HTTPException(status_code=400, detail="Inserisci un testo oppure carica un audio.")

    # testo senza audio → default male
    if script_text and not has_audio and not gender:
        gender = "male"

    if has_audio:
        script_text = ""
        gender = None

    # UPLOAD FOTO
    photo_bytes = await photo.read()
    if not photo_bytes:
        raise HTTPException(status_code=400, detail="Foto mancante o non valida.")

    photo_url = upload_input_to_supabase(token, "photo.png", photo_bytes, photo.content_type or "image/png")

    # UPLOAD AUDIO
    audio_url = None
    if has_audio:
        audio_bytes = await audio.read()
        if not audio_bytes or len(audio_bytes) < 100:
            raise HTTPException(status_code=400, detail="File audio non valido o troppo piccolo.")
        audio_url = upload_input_to_supabase(token, "audio.wav", audio_bytes, audio.content_type or "audio/wav")

    if supabase:
        supabase.table("video_jobs").upsert({
            "evs_token": token,
            "customer_email": email,
            "plan": plan,
            "status": "waiting_payment",
            "gender": gender,
            "script_text": script_text,
            "photo_url": photo_url,
            "audio_url": audio_url,
            "has_audio": bool(audio_url),
            "updated_at": now_iso()
        }).execute()

    return {"ok": True, "evs_token": token, "plan": plan, "photo_url": photo_url, "audio_url": audio_url}

@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    verify_hmac(request, raw)

    data = json.loads(raw.decode("utf-8"))
    financial_status = data.get("financial_status")

    if financial_status != "paid":
        return {"ok": True}

    detected_plan = extract_plan_from_order(data)

    for item in data.get("line_items", []):
        for prop in (item.get("properties") or []):
            name = (prop.get("name", "") or "").lower()
            if "evs" in name and "token" in name:
                tok = prop.get("value")

                # anti doppio avvio
                res = supabase.table("video_jobs").select("status").eq("evs_token", tok).limit(1).execute()
                if not res.data:
                    print("⚠️ Token non trovato in video_jobs:", tok)
                    return {"ok": True}

                current_status = (res.data[0].get("status") or "").lower()
                if current_status in ["processing", "done"]:
                    print("⛔ Job già avviato o completato:", tok)
                    return {"ok": True}

                # aggiorna job con plan + processing
                supabase.table("video_jobs").update({
                    "status": "processing",
                    "plan": detected_plan,
                    "shopify_order_id": str(data.get("id")),
                    "updated_at": now_iso()
                }).eq("evs_token", tok).execute()

                bg.add_task(runpod_submit, tok, data.get("name"), data.get("email"))

    return {"ok": True}

@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):
    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download"
    video_stream = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"
    reel_stream  = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}_reel.mp4"

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
/* (tuo CSS invariato) */
</style>
</head>
<body>
  <div class="container">
    <h1>🎬 Il tuo video è pronto</h1>

    <div class="video-wrap">
      <video controls autoplay playsinline preload="metadata">
        <source src="{video_stream}" type="video/mp4">
      </video>
    </div>

    <div class="actions">
      <a class="btn btn-download" href="{download_url}" download="eccomi-video-{token}.mp4">⬇ Scarica MP4</a>
      <a class="btn btn-download" href="{reel_stream}" download="eccomi-reel-{token}.mp4">📱 Scarica versione Reel</a>
      <a class="btn btn-whatsapp" href="https://api.whatsapp.com/send?text=Guarda%20questo%20video!%20{PUBLIC_BASE_URL}/video/{token}">📲 WhatsApp</a>
    </div>
  </div>
</body>
</html>
""")

@app.get("/video/{token}/download")
def video_download(token: str):
    video_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"
    return RedirectResponse(url=video_url, status_code=302)
