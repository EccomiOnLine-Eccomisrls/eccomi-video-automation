import os
import hmac
import hashlib
import base64
import time
import json
import uuid
import subprocess
from datetime import datetime
from typing import Optional

import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse

from supabase import create_client, Client


# =====================================================
# ENV
# =====================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
RUNPOD_PREVIEW_ENDPOINT_ID = os.getenv("RUNPOD_PREVIEW_ENDPOINT_ID", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "ciaoeccomionline@gmail.com")

SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SUPABASE_INPUTS_BUCKET = os.getenv("SUPABASE_INPUTS_BUCKET", "inputs")
SUPABASE_VIDEOS_BUCKET = os.getenv("SUPABASE_VIDEOS_BUCKET", "videos")

RUNPOD_POLL_MAX_SECONDS = int(os.getenv("RUNPOD_POLL_MAX_SECONDS", "3600"))
RUNPOD_POLL_INTERVAL_SECONDS = int(os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "8"))

MAKE_REEL = os.getenv("MAKE_REEL", "false").lower() == "true"

HTTP_TIMEOUT_SHORT = int(os.getenv("HTTP_TIMEOUT_SHORT", "30"))
HTTP_TIMEOUT_LONG = int(os.getenv("HTTP_TIMEOUT_LONG", "600"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))


# =====================================================
# SUPABASE
# =====================================================
supabase: Optional[Client] = None
if SUPABASE_URL and (SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY):
    key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY
    supabase = create_client(SUPABASE_URL, key)
    print("✅ Supabase collegato")
else:
    print("⚠️ Supabase NON configurato")


# =====================================================
# UTILS
# =====================================================
def now_iso() -> str:
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
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    received = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Invalid HMAC")


def normalize_plan(plan: Optional[str]) -> str:
    p = (plan or "").strip().lower()
    if p == "pro":
        return "pro"
    if p in ["ultra", "premium"]:
        return "ultra"
    return "base"


def normalize_gender(gender: Optional[str]) -> str:
    if not gender:
        return "male"
    g = gender.lower()
    if g in ["donna", "female", "f", "femmina"]:
        return "female"
    return "male"


def http_request_with_retries(method: str, url: str, **kwargs):
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            return requests.request(method, url, **kwargs)
        except Exception as e:
            last_err = e
            time.sleep(1.5 * attempt)
    raise last_err

def send_video_ready_email(
    to_email: str,
    token: str,
    watch_url: str,
    download_url: str,
    order_label: str = ""
):
    if not RESEND_API_KEY or not FROM_EMAIL or not to_email:
        print("⚠️ Email non inviata: RESEND_API_KEY / FROM_EMAIL / destinatario mancanti")
        return

    label = order_label or token
    subject = "🎬 Il tuo video EVS è pronto"

    html = f"""
    <div style="font-family:Arial,sans-serif;background:#0b1b33;padding:32px;color:#ffffff">
      <div style="max-width:680px;margin:0 auto;background:#10264a;border-radius:18px;overflow:hidden;border:1px solid rgba(255,255,255,.08)">
        <div style="padding:28px 28px 12px;text-align:center">
          <div style="font-size:18px;opacity:.9;margin-bottom:8px">Eccomi Video Studio</div>
          <h1 style="margin:0;font-size:34px;line-height:1.15">🎬 Il tuo video è pronto</h1>
          <p style="margin:14px 0 0;font-size:16px;opacity:.9">
            Puoi guardarlo online oppure scaricarlo subito.
          </p>
        </div>

        <div style="padding:24px 28px">
          <div style="background:#0d203f;border-radius:14px;padding:18px 18px 8px;border:1px solid rgba(255,255,255,.06)">
            <p style="margin:0 0 10px;font-size:14px;opacity:.8">Ordine</p>
            <p style="margin:0 0 18px;font-size:16px;font-weight:700;word-break:break-word">{label}</p>

            <div style="margin:24px 0;text-align:center">
              <a href="{watch_url}" style="display:inline-block;background:#1f6bff;color:#fff;text-decoration:none;padding:14px 22px;border-radius:12px;font-weight:700;margin:0 8px 10px">
                ▶ Guarda il video
              </a>
              <a href="{download_url}" style="display:inline-block;background:#ffffff;color:#0b1b33;text-decoration:none;padding:14px 22px;border-radius:12px;font-weight:700;margin:0 8px 10px">
                ⬇ Scarica il video
              </a>
            </div>

            <p style="margin:14px 0 0;font-size:14px;opacity:.85">
              Se hai bisogno di assistenza, scrivi a
              <a href="mailto:{SUPPORT_EMAIL}" style="color:#9ec5ff">{SUPPORT_EMAIL}</a>.
            </p>
          </div>
        </div>

        <div style="padding:0 28px 24px;text-align:center;font-size:12px;opacity:.65">
          Eccomi Video Studio — consegna automatica completata con successo
        </div>
      </div>
    </div>
    """

    payload = {
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        print("📩 Resend status:", r.status_code, r.text)
    except Exception as e:
        print("❌ Errore invio email:", e)        


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


# =====================================================
# REEL
# =====================================================
def ffmpeg_exists():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except:
        return False


def create_vertical_reel(input_mp4, output_mp4):

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_mp4,
        "-vf", "scale=1080:1080,pad=1080:1920:0:420:black",
        "-c:a", "copy",
        output_mp4
    ]

    subprocess.run(cmd, check=True)


# =====================================================
# RUNPOD POLLING
# =====================================================
def poll_runpod(token, job_id):

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

            print("RunPod status:", status)

            if status == "COMPLETED":

                output = data.get("output") or {}

                video_url = (
                    output.get("video_url")
                    or output.get("url")
                    or output.get("output")
                )

                if not video_url:
                    print("❌ RunPod completed ma senza video_url")
                    return

                delivery_page = f"{PUBLIC_BASE_URL}/video/{token}"
                download_url = f"{PUBLIC_BASE_URL}/video/{token}/download"

                email_res = supabase.table("video_jobs")\
                    .select("customer_email,shopify_order_name")\
                    .eq("evs_token", token)\
                    .limit(1)\
                    .execute()

                customer_email = ""
                order_label = ""

                if email_res.data:
                    customer_email = email_res.data[0].get("customer_email") or ""
                    order_label = email_res.data[0].get("shopify_order_name") or ""

                payload = {
                    "status": "done",
                    "video_url": delivery_page,
                    "video_supabase_url": video_url,
                    "runpod_job_id": job_id,
                    "processing_seconds": int(time.time() - started),
                    "finished_at": now_iso(),
                    "updated_at": now_iso()
                }

                response = supabase.table("video_jobs")\
                    .update(payload)\
                    .eq("evs_token", token)\
                    .execute()

                print("Supabase update:", response.data)

                if customer_email:
                    send_video_ready_email(
                        to_email=customer_email,
                        token=token,
                        watch_url=delivery_page,
                        download_url=download_url,
                        order_label=order_label
                    )

                return

            if status in ["FAILED", "CANCELLED"]:
                supabase.table("video_jobs").update({
                    "status": "failed",
                    "updated_at": now_iso()
                }).eq("evs_token", token).execute()
                return

        except Exception as e:
            print("Polling error:", e)

        time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
        waited += RUNPOD_POLL_INTERVAL_SECONDS


# =====================================================
# RUNPOD SUBMIT
# =====================================================
def runpod_submit(token):

    res = supabase.table("video_jobs").select("*").eq("evs_token", token).limit(1).execute()

    if not res.data:
        print("Token non trovato")
        return

    row = res.data[0]

    # 🔒 BLOCCO ANTIDUPLICAZIONE
    if row.get("runpod_job_id"):
        print("Job già avviato")
        return

    gender = normalize_gender(row.get("gender"))

    payload = {
        "input": {
            "token": token,
            "plan": normalize_plan(row.get("plan")),
            "image_url": row.get("photo_url"),
            "audio_url": row.get("audio_url"),
            "text": sanitize_text(row.get("script_text")),
            "gender": gender
        }
    }

    try:

        url = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run"

        r = http_request_with_retries(
            "POST",
            url,
            headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
            json=payload,
            timeout=45
        )

        data = r.json() if r.content else {}

        job_id = data.get("id")

        if not job_id:
            print("RunPod job_id mancante")
            return

        supabase.table("video_jobs").update({
            "status": "processing",
            "runpod_job_id": job_id,
            "updated_at": now_iso()
        }).eq("evs_token", token).execute()

        poll_runpod(token, job_id)

    except Exception as e:
        print("RunPod submit error:", e)


# =====================================================
# FASTAPI
# =====================================================
app = FastAPI(title="EVS FINAL")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


@app.get("/")
def health():
    return {"status": "online", "ts": now_iso()}


# =====================================================
# CREATE ORDER
# =====================================================
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

    photo_bytes = await photo.read()

    photo_url = upload_input_to_supabase(
        token,
        "photo.png",
        photo_bytes,
        photo.content_type or "image/png"
    )

    audio_url = None

    if audio and audio.filename:
        audio_bytes = await audio.read()

        audio_url = upload_input_to_supabase(
            token,
            "audio.wav",
            audio_bytes,
            audio.content_type or "audio/wav"
        )

    supabase.table("video_jobs").upsert({
        "evs_token": token,
        "customer_email": email,
        "plan": normalize_plan(plan),
        "status": "waiting_payment",
        "gender": normalize_gender(gender),
        "script_text": sanitize_text(script_text),
        "photo_url": photo_url,
        "audio_url": audio_url,
        "has_audio": bool(audio_url),
        "updated_at": now_iso()
    }, on_conflict="evs_token").execute()

    return JSONResponse({"ok": True, "evs_token": token})

# =====================================================
# AI PREVIEW
# =====================================================

@app.post("/evs/preview")
async def evs_preview(photo: UploadFile = File(...)):

    token = str(uuid.uuid4())

    photo_bytes = await photo.read()

    photo_url = upload_input_to_supabase(
        token,
        "preview_photo.png",
        photo_bytes,
        photo.content_type or "image/png"
    )

    if not photo_url:
        raise HTTPException(500, "Upload foto fallito")

    payload = {
        "input": {
            "mode": "preview",
            "image_url": photo_url,
            "text": "Preview video",
            "gender": "male",
            "plan": "base"
        }
    }

    try:

        url = f"https://api.runpod.ai/v2/{RUNPOD_PREVIEW_ENDPOINT_ID}/run"

        r = http_request_with_retries(
            "POST",
            url,
            headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
            json=payload,
            timeout=45
        )

        data = r.json()

        job_id = data.get("id")

        if not job_id:
            raise HTTPException(500, f"RunPod job_id mancante: {data}")

        return JSONResponse({
            "job_id": job_id
        })

    except Exception as e:
        print("Preview start error:", e)
        raise HTTPException(500, "Errore avvio preview AI")


# =====================================================
# PREVIEW STATUS
# =====================================================

@app.get("/evs/preview-status/{job_id}")
def preview_status(job_id: str):

    try:

        url = f"https://api.runpod.ai/v2/{RUNPOD_PREVIEW_ENDPOINT_ID}/status/{job_id}"

        r = http_request_with_retries(
            "GET",
            url,
            headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
            timeout=HTTP_TIMEOUT_SHORT
        )

        j = r.json()

        status = (j.get("status") or "").upper()

        if status != "COMPLETED":
            return {
                "status": status
            }

        output = j.get("output") or {}

        video_url = (
            output.get("video_url")
            or output.get("video")
            or output.get("url")
            or output.get("output")
        )

        if not video_url:
            return {
                "status": "COMPLETED",
                "video_url": None
            }

        return {
            "status": "COMPLETED",
            "video_url": video_url
        }

    except Exception as e:
        print("Preview status error:", e)
        raise HTTPException(500, "Errore controllo preview")

# =====================================================
# SHOPIFY WEBHOOK
# =====================================================

@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):

    raw = await request.body()

    verify_hmac(request, raw)

    data = json.loads(raw.decode("utf-8"))

    if (data.get("financial_status") or "").lower() != "paid":
        return {"ok": True}

    for item in data.get("line_items", []):

        for prop in item.get("properties", []):

            name = prop.get("name", "").lower()

            if "evs" in name and "token" in name:

                tok = prop.get("value")

                res = supabase.table("video_jobs")\
                    .select("status")\
                    .eq("evs_token", tok)\
                    .limit(1)\
                    .execute()

                if not res.data:
                    continue

                current_status = res.data[0]["status"]

                if current_status in ["processing", "done"]:
                    continue

                order_id = str(data.get("id") or "")
                order_name = data.get("name") or ""

                supabase.table("video_jobs").update({
                    "status": "processing",
                    "shopify_order_id": order_id,
                    "shopify_order_name": order_name,
                    "updated_at": now_iso()
                }).eq("evs_token", tok).execute()

                bg.add_task(runpod_submit, tok)

    return {"ok": True}


# =====================================================
# VIDEO PAGE
# =====================================================
@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):

    video_stream = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"
    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download"

    order_label = token

    try:
        row = supabase.table("video_jobs")\
            .select("shopify_order_name,customer_email,finished_at")\
            .eq("evs_token", token)\
            .limit(1)\
            .execute()

        if row.data:
            order_label = row.data[0].get("shopify_order_name") or token
            customer_email = row.data[0].get("customer_email") or ""
            finished_at = row.data[0].get("finished_at") or ""
        else:
            customer_email = ""
            finished_at = ""
    except Exception:
        customer_email = ""
        finished_at = ""

    return HTMLResponse(f"""
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Il tuo video è pronto</title>
  <style>
    *{{box-sizing:border-box}}
    body{{
      margin:0;
      font-family:Arial,sans-serif;
      background:
        radial-gradient(circle at top, rgba(31,107,255,.22), transparent 35%),
        linear-gradient(180deg,#07162b 0%, #0b1b33 50%, #10264a 100%);
      color:#fff;
    }}
    .wrap{{
      min-height:100vh;
      display:flex;
      align-items:center;
      justify-content:center;
      padding:24px 16px;
    }}
    .card{{
      width:100%;
      max-width:1040px;
      background:rgba(255,255,255,.04);
      border:1px solid rgba(255,255,255,.08);
      border-radius:28px;
      padding:26px;
      box-shadow:0 18px 60px rgba(0,0,0,.30);
      backdrop-filter:blur(8px);
    }}
    .top{{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:16px;
      margin-bottom:20px;
      flex-wrap:wrap;
    }}
    .brand{{
      font-size:14px;
      letter-spacing:.08em;
      text-transform:uppercase;
      opacity:.72;
    }}
    .badge{{
      display:inline-block;
      padding:8px 12px;
      border-radius:999px;
      background:rgba(31,107,255,.18);
      border:1px solid rgba(31,107,255,.35);
      font-size:13px;
      font-weight:700;
    }}
    .hero{{
      text-align:center;
      margin-bottom:20px;
    }}
    .hero h1{{
      margin:0 0 10px;
      font-size:clamp(30px,5vw,52px);
      line-height:1.05;
    }}
    .hero p{{
      margin:0 auto;
      max-width:760px;
      font-size:16px;
      opacity:.85;
      line-height:1.5;
    }}
    .video-box{{
      border-radius:22px;
      overflow:hidden;
      background:#050b14;
      border:1px solid rgba(255,255,255,.08);
      box-shadow:inset 0 0 0 1px rgba(255,255,255,.02);
    }}
    video{{
      width:100%;
      display:block;
      max-height:72vh;
      background:#000;
    }}
    .actions{{
      display:flex;
      justify-content:center;
      gap:12px;
      flex-wrap:wrap;
      margin-top:22px;
    }}
    .btn{{
      display:inline-block;
      text-decoration:none;
      padding:14px 22px;
      border-radius:14px;
      font-weight:700;
      font-size:16px;
      transition:.18s ease;
    }}
    .btn:hover{{
      transform:translateY(-1px);
    }}
    .btn-primary{{
      background:#1f6bff;
      color:#fff;
      box-shadow:0 10px 24px rgba(31,107,255,.25);
    }}
    .btn-secondary{{
      background:#fff;
      color:#0b1b33;
    }}
    .grid{{
      display:grid;
      grid-template-columns:repeat(3,1fr);
      gap:14px;
      margin-top:24px;
    }}
    .info{{
      background:rgba(255,255,255,.035);
      border:1px solid rgba(255,255,255,.07);
      border-radius:18px;
      padding:16px;
    }}
    .info small{{
      display:block;
      opacity:.65;
      margin-bottom:8px;
      font-size:12px;
      text-transform:uppercase;
      letter-spacing:.06em;
    }}
    .info strong{{
      display:block;
      font-size:15px;
      line-height:1.45;
      word-break:break-word;
    }}
    .foot{{
      margin-top:20px;
      text-align:center;
      font-size:13px;
      opacity:.66;
    }}
    @media (max-width: 820px){{
      .card{{padding:18px}}
      .grid{{grid-template-columns:1fr}}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="top">
        <div class="brand">Eccomi Video Studio</div>
        <div class="badge">✅ Video completato</div>
      </div>

      <div class="hero">
        <h1>🎬 Il tuo video è pronto</h1>
        <p>
          Guardalo online, scaricalo sul tuo dispositivo e conservalo come contenuto personale,
          messaggio speciale o video da condividere.
        </p>
      </div>

      <div class="video-box">
        <video controls playsinline preload="metadata">
          <source src="{video_stream}" type="video/mp4">
        </video>
      </div>

      <div class="actions">
        <a class="btn btn-primary" href="{video_stream}" target="_blank">▶ Guarda il video</a>
        <a class="btn btn-secondary" href="{download_url}">⬇ Scarica il video</a>
      </div>

      <div class="grid">
        <div class="info">
          <small>Ordine</small>
          <strong>{order_label}</strong>
        </div>

        <div class="info">
          <small>Email cliente</small>
          <strong>{customer_email or "Non disponibile"}</strong>
        </div>

        <div class="info">
          <small>Completato il</small>
          <strong>{finished_at or "Appena generato"}</strong>
        </div>
      </div>

      <div class="foot">
        Eccomi Video Studio · Se hai bisogno di assistenza scrivi a {SUPPORT_EMAIL}
      </div>
    </div>
  </div>
</body>
</html>
""")


# =====================================================
# DOWNLOAD
# =====================================================
@app.get("/video/{token}/download")
def video_download(token: str):

    url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"

    r = requests.get(url, stream=True, timeout=60)

    if r.status_code != 200:
        raise HTTPException(404, "Video non trovato")

    headers = {
        "Content-Disposition": f'attachment; filename="ordine-{token}.mp4"'
    }

    return StreamingResponse(
        r.iter_content(chunk_size=8192),
        media_type="video/mp4",
        headers=headers
    )
