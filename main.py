import os
import hmac
import hashlib
import base64
import time
import json
import requests
import uuid
import re
import subprocess

from typing import Optional, Dict, Any
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

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

SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

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
        file_options={
            "content-type": content_type,
            "x-upsert": "true"
        }
    )

    return supabase.storage.from_(SUPABASE_INPUTS_BUCKET).get_public_url(path)

def upload_video_to_supabase(token, source_url):
    if not supabase:
        return None

    r = requests.get(source_url, timeout=600)
    r.raise_for_status()

    file_name = f"{token}.mp4"

    supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).upload(
        path=file_name,
        file=r.content,
        file_options={"content-type": "video/mp4", "x-upsert": "true"}
    )

    return supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).get_public_url(file_name)

def create_vertical_video(token):

    input_file = f"/tmp/{token}.mp4"
    output_file = f"/tmp/{token}_reel.mp4"

    cmd = [
        "ffmpeg",
        "-i", input_file,
        "-vf",
        "scale=1080:1080,pad=1080:1920:0:420:black",
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

            if status == "COMPLETED":

                output = data.get("output", {})
                video_url = output.get("video_url") or output.get("url")

                if video_url:

                    # scarica video RunPod
                    r = requests.get(video_url, timeout=600)
                    r.raise_for_status()

                    local_file = f"/tmp/{token}.mp4"

                    with open(local_file, "wb") as f:
                        f.write(r.content)

                    # crea versione verticale
                    reel_file = create_vertical_video(token)

                    # upload video originale
                    supa_url = upload_video_to_supabase(token, video_url)

                    # upload versione verticale
                    reel_name = f"{token}_reel.mp4"

                    with open(reel_file, "rb") as f:
                        supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).upload(
                            path=reel_name,
                            file=f.read(),
                            file_options={
                                "content-type": "video/mp4",
                                "x-upsert": "true"
                            }
                        )

                    delivery_page = f"{PUBLIC_BASE_URL}/video/{token}"

                    supabase.table("video_jobs").update({
                        "status": "done",
                        "video_url": delivery_page,
                        "video_supabase_url": supa_url,
                        "runpod_job_id": job_id,
                        "updated_at": now_iso()
                    }).eq("evs_token", token).execute()

                    print("✅ Video completato:", token)

                    return

            if status in ["FAILED", "CANCELLED"]:

                print("❌ RunPod job failed:", token)

                supabase.table("video_jobs").update({
                    "status": "failed"
                }).eq("evs_token", token).execute()

                return

        except Exception as e:
            print("⚠️ Polling error:", e)

        time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
        waited += RUNPOD_POLL_INTERVAL_SECONDS



def runpod_submit(tok, name, email):

    print("🚀 runpod_submit start:", tok)

    res = supabase.table("video_jobs") \
        .select("*") \
        .eq("evs_token", tok) \
        .limit(1) \
        .execute()

    if not res.data:
        print("⚠️ Nessuna riga trovata per token:", tok)
        return

    row = res.data[0]

    payload = {
        "input": {
            "image_url": row.get("photo_url"),
            "text": sanitize_text(row.get("script_text")),
            "gender": (row.get("gender") or "male"),
            "token": tok
        }
    }

    if row.get("audio_url"):
        payload["input"]["audio_url"] = row.get("audio_url")

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

    job_id = r.json().get("id")

    if job_id:

        print("🧠 RunPod job avviato:", job_id)

        supabase.table("video_jobs").update({
            "status": "processing",
            "runpod_job_id": job_id,
            "updated_at": now_iso()
        }).eq("evs_token", tok).execute()

        poll_runpod(tok, job_id)

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
    evs_token: Optional[str] = Form(None)
):
    token = (evs_token or "").strip() or str(uuid.uuid4())

    script_text = sanitize_text(script_text)
    has_audio = bool(audio and audio.filename)

    # DEBUG
    print("DEBUG -> email:", email)
    print("DEBUG -> script_text:", script_text)
    print("DEBUG -> gender raw:", gender)
    print("DEBUG -> has_audio:", has_audio)

    # NORMALIZZA GENDER
    if gender:
        g = gender.strip().lower()
        if g in ["uomo", "male", "m", "maschio"]:
            gender = "male"
        elif g in ["donna", "female", "f", "femmina"]:
            gender = "female"
        else:
            gender = None

    # LOGICA
    if not has_audio and not script_text:
        raise HTTPException(status_code=400, detail="Inserisci un testo oppure carica un audio.")

    # ✅ CASO 1: testo senza audio → se non scelto, default male
    if script_text and not has_audio and not gender:
        gender = "male"

    if has_audio:
        script_text = ""
        gender = None

    # UPLOAD FOTO
    photo_bytes = await photo.read()
    if not photo_bytes:
        raise HTTPException(status_code=400, detail="Foto mancante o non valida.")

    photo_url = upload_input_to_supabase(
        token,
        "photo.png",
        photo_bytes,
        photo.content_type or "image/png"
    )

    # UPLOAD AUDIO (se presente)
    audio_url = None
    if has_audio:
        audio_bytes = await audio.read()
        if not audio_bytes or len(audio_bytes) < 100:
            raise HTTPException(status_code=400, detail="File audio non valido o troppo piccolo.")

        audio_url = upload_input_to_supabase(
            token,
            "audio.wav",
            audio_bytes,
            audio.content_type or "audio/wav"
        )

    # DB
    if supabase:
        supabase.table("video_jobs").upsert({
            "evs_token": token,
            "customer_email": email,
            "status": "waiting_payment",
            "gender": gender,
            "script_text": script_text,
            "photo_url": photo_url,
            "audio_url": audio_url,
            "has_audio": bool(audio_url),
            "updated_at": now_iso()
        }).execute()

    return {"ok": True, "evs_token": token, "photo_url": photo_url, "audio_url": audio_url}

@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):

    raw = await request.body()
    verify_hmac(request, raw)

    data = json.loads(raw.decode("utf-8"))
    financial_status = data.get("financial_status")

    for item in data.get("line_items", []):
        for prop in item.get("properties", []):

            name = prop.get("name", "").lower()

            if "evs" in name and "token" in name:

                tok = prop.get("value")

                if financial_status == "paid":

                    # 🔎 Leggi stato attuale (SAFE)
                    res = supabase.table("video_jobs") \
                        .select("status") \
                        .eq("evs_token", tok) \
                        .limit(1) \
                        .execute()

                    if not res.data:
                        print("⚠️ Token non trovato in video_jobs:", tok)
                        return {"ok": True}

                    current_status = (res.data[0].get("status") or "").lower()

                    # 🛑 Anti doppio avvio
                    if current_status in ["processing", "done"]:
                        print("⛔ Job già avviato o completato:", tok)
                        return {"ok": True}

                    # 🔄 Aggiorna a processing
                    supabase.table("video_jobs").update({
                        "status": "processing",
                        "shopify_order_id": str(data.get("id")),
                        "updated_at": now_iso()
                    }).eq("evs_token", tok).execute()

                    # 🚀 Avvia RunPod
                    bg.add_task(runpod_submit, tok, data.get("name"), data.get("email"))

    return {"ok": True}

@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):
    # link download (rimane uguale)
    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download"

    # stream diretto (apre e riproduce)
    video_stream = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"

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
<meta property="og:image" content="{video_stream}">

  <style>
    :root {{
      --bg1:#173b86;
      --bg2:#0b1b33;
      --card: rgba(255,255,255,0.06);
      --stroke: rgba(255,255,255,0.10);
      --text: #ffffff;
      --muted: rgba(255,255,255,0.72);
      --shadow: rgba(0,0,0,0.65);
      --brand: #e62e2d;
    }}

    * {{ box-sizing:border-box; }}

    body {{
      margin:0;
      font-family: Arial, Helvetica, sans-serif;
      color: var(--text);
      background:
        radial-gradient(1100px 500px at 50% 0%, rgba(23,59,134,0.95) 0%, rgba(11,27,51,1) 55%),
        radial-gradient(900px 420px at 10% 20%, rgba(230,46,45,0.20) 0%, rgba(0,0,0,0) 60%),
        radial-gradient(900px 420px at 90% 35%, rgba(62,224,140,0.12) 0%, rgba(0,0,0,0) 55%),
        linear-gradient(180deg, var(--bg1) 0%, var(--bg2) 60%);
      min-height:100vh;
    }}

    .container {{
      max-width: 980px;
      margin: 0 auto;
      padding: 36px 16px 44px;
      text-align:center;
    }}

    .brand {{
      display:flex;
      align-items:center;
      justify-content:center;
      gap:10px;
      margin-bottom: 10px;
    }}

    .brand-badge {{
      width:10px;height:10px;border-radius:999px;
      background: var(--brand);
      box-shadow: 0 0 18px rgba(230,46,45,0.6);
    }}

    .brand-text {{
      font-size: 13px;
      letter-spacing: .2px;
      color: var(--muted);
    }}

    h1 {{
      margin: 10px 0 8px;
      font-size: 30px;
      line-height: 1.15;
    }}

    .sub {{
      margin: 0 auto 22px;
      max-width: 720px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
    }}

    .video-wrap {{
      background: var(--card);
      border: 1px solid var(--stroke);
      border-radius: 18px;
      overflow: hidden;
      box-shadow: 0 30px 80px var(--shadow);
      backdrop-filter: blur(10px);
    }}

    video {{
      width:100%;
      height:auto;
      display:block;
      background:#000;
    }}

    .note {{
      margin-top: 14px;
      font-size: 12.5px;
      color: var(--muted);
    }}

    .actions {{
      margin-top: 22px;
      display:flex;
      flex-wrap:wrap;
      justify-content:center;
      gap:12px;
    }}

    .btn {{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:8px;
      padding: 13px 18px;
      border-radius: 12px;
      font-weight: 700;
      text-decoration:none;
      border: 1px solid rgba(255,255,255,0.12);
      transition: transform .20s ease, box-shadow .20s ease, opacity .20s ease;
      will-change: transform;
      user-select:none;
      min-width: 170px;
    }}

    .btn:hover {{
      transform: translateY(-2px);
      box-shadow: 0 14px 28px rgba(0,0,0,0.35);
    }}

    .btn:active {{
      transform: translateY(0px);
      opacity: .95;
    }}

    .btn-download {{
      background: #ffffff;
      color: #0b1b33;
    }}

    .btn-whatsapp {{
      background: #25D366;
      color: #0b1b33;
    }}

    .btn-instagram {{
      background: #E1306C;
      color: #ffffff;
    }}

    .btn-tiktok {{
      background: #111111;
      color: #ffffff;
    }}

    .btn-create {{
      background: #2d6cdf;
      color: #ffffff;
      border: 1px solid rgba(255,255,255,0.16);
      min-width: 220px;
    }}

    .footer {{
      margin-top: 30px;
      color: rgba(255,255,255,0.55);
      font-size: 13px;
      line-height: 1.4;
    }}

    @media (max-width: 520px) {{
      h1 {{ font-size: 26px; }}
      .btn {{ min-width: 100%; }}
    }}
  </style>
</head>

<body>
  <div class="container">
    <div class="brand">
      <span class="brand-badge"></span>
      <div class="brand-text">Creato con Eccomi Video Studio</div>
    </div>

    <h1>🎬 Il tuo video è pronto</h1>
    <div class="sub">
      Puoi guardarlo subito qui sotto, scaricarlo in MP4 oppure condividerlo.
    </div>

    <div class="video-wrap">
      <video controls autoplay playsinline preload="metadata">
        <source src="{video_stream}" type="video/mp4">
      </video>
    </div>

    <div class="note">Video generato con Intelligenza Artificiale • MP4</div>

    <div class="actions">
      <a class="btn btn-download" href="{download_url}" download="eccomi-video-{token}.mp4">
⬇ Scarica MP4
</a>

      <a class="btn btn-whatsapp"
         href="https://api.whatsapp.com/send?text=Guarda%20questo%20video!%20{PUBLIC_BASE_URL}/video/{token}">
         📲 WhatsApp
      </a>

    <div class="actions" style="margin-top:14px;">
      <a class="btn btn-create" href="https://eccomionline.com/products/video-ai-da-foto-parlante">
        ✨ Crea un altro video
      </a>
    </div>

    <div class="footer">
      Vuoi creare anche tu un video parlante da una foto?<br>
      👉 eccomionline.com
    </div>
  </div>
</body>
</html>
""")

@app.get("/video/{token}/download")
def video_download(token: str):

    video_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"

    return RedirectResponse(
        url=video_url,
        status_code=302
    )
