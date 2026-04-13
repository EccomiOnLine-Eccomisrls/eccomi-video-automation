import os
import hmac
import hashlib
import base64
import time
import json
import uuid
import subprocess
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
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
RUNPOD_ULTRA_VOICE_ENDPOINT_ID = os.getenv("RUNPOD_ULTRA_VOICE_ENDPOINT_ID", "")
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

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE
)

# =====================================================
# UTILS
# =====================================================
def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def sanitize_text(text: str) -> str:
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)

    replacements = {
        "\r\n": "\n",
        "\r": "\n",
        "\u00a0": " ",   # spazio non separabile
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "…": "...",
        "–": "-",
        "—": "-",
        "&": " e ",
        "%": " per cento ",
        "@": " chiocciola ",
        "|": " ",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # togli emoji
    text = EMOJI_RE.sub(" ", text)

    # togli caratteri di controllo strani, ma lascia testo normale
    text = "".join(
        ch for ch in text
        if unicodedata.category(ch)[0] != "C" or ch in "\n\t "
    )

    # trasforma troppi separatori in pause più naturali
    text = re.sub(r"[\\/]+", ", ", text)
    text = re.sub(r"[_~^*#<>]+", " ", text)

    # riduce spazi multipli
    text = re.sub(r"[ \t]+", " ", text)

    # riduce righe multiple
    text = re.sub(r"\n{2,}", "\n", text)

    # spazi prima della punteggiatura
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)

    # spazi dopo la punteggiatura, se manca
    text = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", text)

    # massimo 3 punti
    text = re.sub(r"\.{4,}", "...", text)

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

    logo_url = os.getenv("ECCOMI_LOGO_URL", "").strip()
    new_order_url = "https://eccomionline.com/products/video-ai-da-foto-parlante"
    site_url = "https://eccomionline.com"

    logo_html = (
        f'<img src="{logo_url}" alt="Eccomi" style="height:64px;max-width:180px;object-fit:contain;display:block;margin:0 auto 16px;">'
        if logo_url else
        '<div style="font-size:22px;font-weight:800;letter-spacing:.02em;margin-bottom:16px;">Eccomi</div>'
    )

    html = f"""
<div style="margin:0;padding:32px 16px;background:#0b1b33;font-family:Arial,sans-serif;color:#ffffff;">
  <div style="max-width:700px;margin:0 auto;background:#10264a;border:1px solid rgba(255,255,255,.08);border-radius:20px;overflow:hidden;">
    
    <div style="padding:28px 24px 10px;text-align:center;">
      {logo_html}
      <div style="font-size:15px;opacity:.88;margin-bottom:8px;">Eccomi Video Studio</div>
      <h1 style="margin:0;font-size:34px;line-height:1.12;">🎬 Il tuo video è pronto</h1>
      <p style="margin:14px 0 0;font-size:16px;line-height:1.5;opacity:.92;">
        Il tuo video è pronto. Guardalo online oppure scaricalo subito.
      </p>
    </div>

    <div style="padding:22px 24px 10px;">
      <div style="background:#0d203f;border:1px solid rgba(255,255,255,.06);border-radius:16px;padding:18px;">
        <div style="font-size:13px;opacity:.72;margin-bottom:8px;">Ordine</div>
        <div style="font-size:24px;font-weight:800;word-break:break-word;margin-bottom:18px;">{label}</div>

        <div style="text-align:center;margin:26px 0 14px;">
          <a href="{watch_url}" style="display:inline-block;background:#2f6dff;color:#ffffff;text-decoration:none;padding:14px 24px;border-radius:12px;font-size:16px;font-weight:700;margin:0 8px 12px;">
            ▶ Guarda il tuo video
          </a>
          <a href="{download_url}" style="display:inline-block;background:#ffffff;color:#0b1b33;text-decoration:none;padding:14px 24px;border-radius:12px;font-size:16px;font-weight:700;margin:0 8px 12px;">
            ⬇ Scarica il video
          </a>
        </div>

        <div style="text-align:center;margin:6px 0 4px;">
          <a href="{new_order_url}" style="display:inline-block;background:#1c3f73;color:#ffffff;text-decoration:none;padding:14px 26px;border-radius:12px;font-size:15px;font-weight:700;border:1px solid rgba(255,255,255,.22);box-shadow:0 8px 20px rgba(0,0,0,.18);">
            ✨ Crea un altro video
          </a>
        </div>

        <p style="margin:18px 0 0;font-size:14px;line-height:1.5;opacity:.86;text-align:center;">
          Il tuo video resta disponibile online e può essere scaricato quando vuoi.
        </p>
        <p style="margin:10px 0 0;font-size:13px;line-height:1.5;opacity:.68;text-align:center;">
          EVS è un servizio digitale di Eccomi OnLine.
        </p>
      </div>
    </div>

    <div style="padding:8px 24px 24px;text-align:center;">
      <p style="margin:10px 0 0;font-size:14px;line-height:1.5;opacity:.82;">
        Se hai bisogno di assistenza, scrivi a
        <a href="mailto:{SUPPORT_EMAIL}" style="color:#9ec5ff;text-decoration:none;">{SUPPORT_EMAIL}</a>
      </p>

      <p style="margin:10px 0 0;font-size:13px;opacity:.66;">
        <a href="{site_url}" style="color:#9ec5ff;text-decoration:none;">eccomionline.com</a>
      </p>

      <p style="margin:14px 0 0;font-size:12px;opacity:.58;">
        Eccomi Video Studio — consegna automatica completata con successo
      </p>
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


def upload_local_file_to_supabase(bucket: str, object_name: str, local_path: str, content_type: str = "application/octet-stream"):
    if not supabase:
        return None

    with open(local_path, "rb") as f:
        file_bytes = f.read()

    supabase.storage.from_(bucket).upload(
        path=object_name,
        file=file_bytes,
        file_options={"content-type": content_type, "x-upsert": "true"}
    )

    return supabase.storage.from_(bucket).get_public_url(object_name)


def create_reel_from_video_url(source_url: str, token: str):
    if not MAKE_REEL:
        print("ℹ️ MAKE_REEL disattivato")
        return None

    if not ffmpeg_exists():
        print("❌ ffmpeg non disponibile")
        return None

    source_path = f"/tmp/{token}_source.mp4"
    reel_path = f"/tmp/{token}_reel.mp4"

    try:
        r = http_request_with_retries(
            "GET",
            source_url,
            stream=True,
            timeout=HTTP_TIMEOUT_LONG
        )

        if r.status_code != 200:
            print("❌ Download video originale fallito per Reel:", r.status_code)
            return None

        with open(source_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        create_vertical_reel(source_path, reel_path)

        if not os.path.exists(reel_path):
            print("❌ Reel non creato")
            return None

        if os.path.getsize(reel_path) < 5000:
            print("❌ Reel troppo piccolo")
            return None

        reel_public_url = upload_local_file_to_supabase(
            SUPABASE_VIDEOS_BUCKET,
            f"{token}_reel.mp4",
            reel_path,
            "video/mp4"
        )

        print("✅ Reel pronto:", reel_public_url)
        return reel_public_url

    except Exception as e:
        print("❌ Errore generazione Reel:", e)
        return None


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
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_mp4
    ]

    subprocess.run(cmd, check=True)


# =====================================================
# ULTRA DUBBING
# =====================================================

def create_ultra_dubbed_audio(token: str, text: str, voice_sample_url: str) -> str:
    if not RUNPOD_API_KEY:
        raise RuntimeError("RUNPOD_API_KEY mancante")

    if not RUNPOD_ULTRA_VOICE_ENDPOINT_ID:
        raise RuntimeError("RUNPOD_ULTRA_VOICE_ENDPOINT_ID mancante")

    clean_text = sanitize_text(text)

    if not clean_text:
        raise RuntimeError("Testo Ultra vuoto")

    if not voice_sample_url:
        raise RuntimeError("voice_sample_url mancante")

    payload = {
        "input": {
            "token": token,
            "text": clean_text,
            "voice_sample_url": voice_sample_url,
            "language": "it"
        }
    }

    submit_url = f"https://api.runpod.ai/v2/{RUNPOD_ULTRA_VOICE_ENDPOINT_ID}/run"

    r = http_request_with_retries(
        "POST",
        submit_url,
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        json=payload,
        timeout=45
    )

    data = r.json() if r.content else {}
    job_id = data.get("id")

    if not job_id:
        raise RuntimeError(f"RunPod Ultra Voice job_id mancante: {data}")

    started = time.time()

    while time.time() - started < RUNPOD_POLL_MAX_SECONDS:
        status_url = f"https://api.runpod.ai/v2/{RUNPOD_ULTRA_VOICE_ENDPOINT_ID}/status/{job_id}"

        sr = http_request_with_retries(
            "GET",
            status_url,
            headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
            timeout=HTTP_TIMEOUT_SHORT
        )

        sdata = sr.json() if sr.content else {}
        status = (sdata.get("status") or "").upper()

        print("ULTRA VOICE status:", status)

        if status == "COMPLETED":
    output = sdata.get("output") or {}

    error_message = output.get("error") or ""
    traceback_text = output.get("traceback") or ""

    if error_message:
        raise RuntimeError(f"Ultra Voice error: {error_message}\n{traceback_text}")

    dubbed_audio_url = (
        output.get("dubbed_audio_url")
        or output.get("audio_url")
        or output.get("url")
        or ""
    )

    if not dubbed_audio_url:
        raise RuntimeError(f"Ultra Voice completed senza dubbed_audio_url: {output}")

    return dubbed_audio_url

        if status in ["FAILED", "CANCELLED", "TIMED_OUT"]:
            raise RuntimeError(f"Ultra Voice job failed: {sdata}")

        time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)

    raise RuntimeError("Timeout generazione audio Ultra")


def ensure_ultra_dubbed_audio(row: dict) -> str:
    token = row.get("evs_token") or ""
    dubbed_audio_url = row.get("dubbed_audio_url") or ""
    script_text = sanitize_text(row.get("script_text") or "")
    voice_sample_url = row.get("voice_sample_url") or ""

    if dubbed_audio_url:
        return dubbed_audio_url

    if not script_text:
        raise RuntimeError("Ultra senza script_text")

    if not voice_sample_url:
        raise RuntimeError("Ultra senza voice_sample_url")

    dubbed_audio_url = create_ultra_dubbed_audio(
        token=token,
        text=script_text,
        voice_sample_url=voice_sample_url
    )

    if not dubbed_audio_url:
        raise RuntimeError("Ultra dubbing URL non restituito")

    supabase.table("video_jobs").update({
        "dubbed_audio_url": dubbed_audio_url,
        "updated_at": now_iso()
    }).eq("evs_token", token).execute()

    return dubbed_audio_url


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

                reel_url = output.get("reel_url")

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
                    "video_reel_url": reel_url,
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

                if reel_url:
                    print("✅ Reel ricevuto da RunPod:", reel_url)
                else:
                    print("ℹ️ Reel non restituito da RunPod")

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

    plan = normalize_plan(row.get("plan"))
    gender = normalize_gender(row.get("gender"))

    runpod_audio_url = row.get("audio_url") or ""
    runpod_text = sanitize_text(row.get("script_text") or "")

    if plan == "ultra":
        try:
            runpod_audio_url = ensure_ultra_dubbed_audio(row)
            runpod_text = ""
        except Exception as e:
            print("❌ Ultra dubbing error:", e)

            supabase.table("video_jobs").update({
                "status": "failed",
                "updated_at": now_iso()
            }).eq("evs_token", token).execute()

            return

    elif runpod_audio_url:
        runpod_text = ""

    payload = {
        "input": {
            "token": token,
            "plan": plan,
            "image_url": row.get("photo_url"),
            "audio_url": runpod_audio_url,
            "text": runpod_text,
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
    voice_sample: Optional[UploadFile] = File(None),
    script_text: str = Form(""),
    gender: Optional[str] = Form(None),
    plan: Optional[str] = Form("base"),
    evs_token: Optional[str] = Form(None),
    voice_clone_consent: Optional[str] = Form(None),
    voice_mode: Optional[str] = Form(None)
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

    voice_sample_url = None

    if voice_sample and voice_sample.filename:
        voice_sample_bytes = await voice_sample.read()

        ext = os.path.splitext(voice_sample.filename)[1].lower() or ".wav"
        safe_name = f"voice_sample{ext}"

        voice_sample_url = upload_input_to_supabase(
            token,
            safe_name,
            voice_sample_bytes,
            voice_sample.content_type or "audio/wav"
        )

    plan_norm = normalize_plan(plan)
    script_text_clean = sanitize_text(script_text)

    clone_consent_bool = str(voice_clone_consent).lower() in ["true", "1", "yes", "on"]

    if plan_norm == "ultra":
        if not script_text_clean:
            raise HTTPException(400, "Ultra richiede un testo")
        if not voice_sample_url:
            raise HTTPException(400, "Ultra richiede un campione voce")
        if not clone_consent_bool:
            raise HTTPException(400, "Devi confermare il diritto di usare questa voce")

    supabase.table("video_jobs").upsert({
        "evs_token": token,
        "customer_email": email,
        "plan": plan_norm,
        "status": "waiting_payment",
        "gender": normalize_gender(gender),
        "script_text": script_text_clean,
        "script_text_original": script_text,
        "photo_url": photo_url,
        "audio_url": audio_url,
        "has_audio": bool(audio_url),
        "voice_sample_url": voice_sample_url,
        "voice_clone_consent": clone_consent_bool,
        "voice_mode": "cloned" if plan_norm == "ultra" else ("audio" if audio_url else "standard"),
        "updated_at": now_iso()
    }, on_conflict="evs_token").execute()

    return JSONResponse({"ok": True, "evs_token": token})

# =====================================================
# AI PREVIEW
# =====================================================

@app.post("/evs/preview")
async def evs_preview(
    photo: UploadFile = File(...),
    gender: Optional[str] = Form(None)
):
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

    preview_gender = normalize_gender(gender or "female")

    payload = {
        "input": {
            "mode": "preview",
            "image_url": photo_url,
            "text": "Ciao, questo è un esempio del tuo video creato con Eccomi Video Studio.",
            "gender": preview_gender,
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
def evs_preview_status(job_id: str):
    try:
        url = f"https://api.runpod.ai/v2/{RUNPOD_PREVIEW_ENDPOINT_ID}/status/{job_id}"

        r = http_request_with_retries(
            "GET",
            url,
            headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
            timeout=30
        )

        data = r.json()

        status = (data.get("status") or "").upper()
        output = data.get("output") or {}

        video_url = (
            output.get("video_url")
            or output.get("video")
            or output.get("url")
            or output.get("output")
            or ""
        )

        if status in ["IN_QUEUE", "IN_PROGRESS", "PROCESSING"]:
            return JSONResponse({"status": "PROCESSING"})

        if status == "COMPLETED" and video_url:
            try:
                test_resp = requests.get(video_url, stream=True, timeout=10)
                ok = test_resp.status_code == 200
                test_resp.close()

                if not ok:
                    return JSONResponse({"status": "PROCESSING"})
            except Exception:
                return JSONResponse({"status": "PROCESSING"})

            return JSONResponse({
                "status": "COMPLETED",
                "video_url": video_url
            })

        if status in ["FAILED", "CANCELLED", "TIMED_OUT"]:
            return JSONResponse({
                "status": "FAILED",
                "error": data.get("error") or "Preview non disponibile"
            })

        return JSONResponse({"status": "PROCESSING"})

    except Exception as e:
        print("Preview status error:", e)
        return JSONResponse({
            "status": "FAILED",
            "error": "Errore controllo stato preview"
        }, status_code=500)

# =====================================================
# SHOPIFY WEBHOOK
# =====================================================

@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):

    raw = await request.body()

    verify_hmac(request, raw)

    data = json.loads(raw.decode("utf-8"))

    financial_status = (data.get("financial_status") or "").lower()
    total_price = str(data.get("total_price") or data.get("current_total_price") or "")

    print("SHOPIFY WEBHOOK financial_status:", financial_status)
    print("SHOPIFY WEBHOOK total_price:", total_price)
    print("SHOPIFY WEBHOOK order_name:", data.get("name"))
    print("SHOPIFY WEBHOOK topic:", request.headers.get("X-Shopify-Topic"))

    if financial_status != "paid" and total_price not in ["0", "0.0", "0.00"]:
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
    new_order_url = "https://eccomionline.com/products/video-ai-da-foto-parlante"
    logo_url = os.getenv("ECCOMI_LOGO_URL", "").strip()
    logo_html = (
        f'<img class="brand-logo" src="{logo_url}" alt="Eccomi OnLine">'
        if logo_url else ""
    )

    order_label = token
    customer_email = ""
    pretty_finished = "Appena generato"
    reel_url = ""

    try:
        row = supabase.table("video_jobs")\
            .select("shopify_order_name,customer_email,finished_at,video_reel_url")\
            .eq("evs_token", token)\
            .limit(1)\
            .execute()

        if row.data:
            order_label = row.data[0].get("shopify_order_name") or token
            customer_email = (row.data[0].get("customer_email") or "").strip().lower()
            finished_at = row.data[0].get("finished_at") or ""
            reel_url = row.data[0].get("video_reel_url") or ""

            if finished_at:
                try:
                    dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                    dt_local = dt.astimezone(ZoneInfo("Europe/Rome"))

                    mesi = [
                        "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
                        "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"
                    ]
                    pretty_finished = f"{dt_local.day} {mesi[dt_local.month - 1]} {dt_local.year} · {dt_local.strftime('%H:%M')}"
                except Exception:
                    pretty_finished = finished_at

    except Exception:
        pass

    reel_button_html = (
        f'<a class="btn btn-secondary" href="{PUBLIC_BASE_URL}/video/{token}/reel">📱 Scarica Reel</a>'
        if reel_url else
        '<span class="btn btn-disabled">📱 Scarica Reel</span>'
    )

    reel_note_html = (
        "La versione social verticale è pronta."
        if reel_url else
        "La versione social verticale sarà disponibile a breve."
    )

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
      overflow:hidden;
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
    .brand-head{{
      display:flex;
      align-items:center;
      gap:12px;
      flex-wrap:wrap;
    }}
    .brand-logo{{
      height:54px;
      width:auto;
      display:block;
      filter:drop-shadow(0 4px 12px rgba(0,0,0,.25));
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
      margin:0 -26px 22px;
      padding:42px 22px 28px;
      background:
        radial-gradient(circle at 50% -10%, rgba(91,131,255,.85), rgba(61,108,255,.65) 38%, transparent 68%);
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
      opacity:.88;
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
      border:none;
    }}
    .btn:hover{{
      transform:translateY(-1px);
    }}
    .btn-primary{{
      background:#3f6fff;
      color:#fff;
      box-shadow:0 10px 24px rgba(31,107,255,.25);
    }}
    .btn-secondary{{
      background:#fff;
      color:#0b1b33;
    }}
    .btn-share{{
  background:#1b1b1b;
  color:#fff;
  border:1px solid rgba(255,255,255,.14);
  box-shadow:0 8px 18px rgba(0,0,0,.18);
}}
    .btn-disabled{{
      background:rgba(255,255,255,.08);
      color:rgba(255,255,255,.65);
      border:1px solid rgba(255,255,255,.10);
      cursor:not-allowed;
      pointer-events:none;
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
    .email-soft{{
      text-transform:none;
      font-weight:600;
      opacity:.92;
    }}
    .reel-note{{
      text-align:center;
      margin-top:10px;
      font-size:13px;
      opacity:.6;
    }}
    .foot{{
      margin-top:20px;
      text-align:center;
      font-size:13px;
      opacity:.66;
    }}
    @media (max-width: 820px){{
      .card{{padding:18px}}
      .hero{{margin:0 -18px 20px;padding:34px 18px 22px}}
      .grid{{grid-template-columns:1fr}}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="top">
        <div class="brand-head">
          {logo_html}
          <div class="brand">Eccomi Video Studio</div>
        </div>
        <div class="badge">✅ Video completato</div>
      </div>

      <div class="hero">
        <h1>🎬 Il tuo video è pronto</h1>
        <p>
          Guardalo online, scaricalo sul tuo dispositivo o crea subito un nuovo video.
        </p>
      </div>

      <div class="video-box">
        <video controls playsinline preload="metadata">
          <source src="{video_stream}" type="video/mp4">
        </video>
      </div>

      <div class="actions">
  <a class="btn btn-secondary" href="{download_url}">⬇ Scarica il video</a>
  {reel_button_html}
  <button class="btn btn-share" id="share-evs-btn" type="button">📤 Consiglia EVS</button>
  <a class="btn btn-primary" href="{new_order_url}">✨ Crea un altro video</a>
</div>

      <div class="reel-note">
        Scarica il tuo contenuto. {reel_note_html}
      </div>

      <div class="grid">
        <div class="info">
          <small>Ordine</small>
          <strong>{order_label}</strong>
        </div>

        <div class="info">
          <small>Email cliente</small>
          <strong class="email-soft">{customer_email or "Non disponibile"}</strong>
        </div>

        <div class="info">
          <small>Completato il</small>
          <strong>{pretty_finished}</strong>
        </div>
      </div>

      <div class="foot">
        EVS è un servizio digitale di Eccomi OnLine · Assistenza: {SUPPORT_EMAIL}
      </div>
    </div>
  </div>
  <script>
  (function () {{
    const btn = document.getElementById("share-evs-btn");
    if (!btn) return;

    const shareData = {{
      title: "Eccomi Video Studio",
      text: "Guarda questo servizio: puoi creare un video AI da foto parlante su Eccomi OnLine.",
      url: "{new_order_url}"
    }};

    btn.addEventListener("click", async function () {{
      try {{
        if (navigator.share) {{
          await navigator.share(shareData);
          return;
        }}

        await navigator.clipboard.writeText(shareData.url);
        btn.textContent = "✅ Link copiato";
        setTimeout(() => {{
          btn.textContent = "📤 Consiglia EVS";
        }}, 1800);

      }} catch (e) {{
        console.log("Share error:", e);
      }}
    }});
  }})();
</script>
</body>
</html>
""")


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


@app.get("/video/{token}/reel")
def video_reel_download(token: str):

    row = supabase.table("video_jobs")\
        .select("video_reel_url")\
        .eq("evs_token", token)\
        .limit(1)\
        .execute()

    if not row.data:
        raise HTTPException(404, "Ordine non trovato")

    reel_url = row.data[0].get("video_reel_url") or ""

    if not reel_url:
        raise HTTPException(404, "Reel non disponibile")

    r = requests.get(reel_url, stream=True, timeout=60)

    if r.status_code != 200:
        raise HTTPException(404, "Reel non trovato")

    headers = {
        "Content-Disposition": f'attachment; filename="ordine-{token}-reel.mp4"'
    }

    return StreamingResponse(
        r.iter_content(chunk_size=8192),
        media_type="video/mp4",
        headers=headers
    )
