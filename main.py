import os
import hmac
import hashlib
import base64
import time
import json
import requests
import uuid
import mimetypes
import re
from typing import Optional, Dict, Any
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse, RedirectResponse

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

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")  # es: https://video.eccomionline.com
EVS_STORAGE_DIR = os.getenv("EVS_STORAGE_DIR", "data/evs_orders")

# Shopify Admin (per EVASO + email notifica)
SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")  # es: eccomionline.myshopify.com
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

# ---- RunPod polling / timeouts ----
RUNPOD_POLL_MAX_SECONDS = int(os.getenv("RUNPOD_POLL_MAX_SECONDS", "3600"))  # default 3600
RUNPOD_POLL_INTERVAL_SECONDS = int(os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "8"))  # default 8
RUNPOD_SUBMIT_TIMEOUT = int(os.getenv("RUNPOD_SUBMIT_TIMEOUT", "45"))  # default 45

# Download timeout (video da RunPod ecc.)
VIDEO_DOWNLOAD_TIMEOUT = int(os.getenv("VIDEO_DOWNLOAD_TIMEOUT", "600"))  # default 240

# Supabase bucket videos
SUPABASE_VIDEOS_BUCKET = os.getenv("SUPABASE_VIDEOS_BUCKET", "videos")

# =====================================================
# STORAGE (solo meta / upload iniziali; i VIDEO finali vanno su Supabase)
# =====================================================

EVS_STORAGE = Path(EVS_STORAGE_DIR)
EVS_STORAGE.mkdir(parents=True, exist_ok=True)

# =====================================================
# SUPABASE
# =====================================================

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase collegato")
    except Exception as e:
        print("⚠️ Supabase error:", e)

# =====================================================
# UTILS
# =====================================================

def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def load_json(path: Path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def update_meta(order_id: str, changes: Dict[str, Any]):
    order_dir = EVS_STORAGE / order_id
    if not order_dir.exists():
        return
    meta_path = order_dir / "meta.json"
    meta = load_json(meta_path)
    meta.update(changes)
    meta["updated_at"] = now_iso()
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


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


def safe_filename(token: str) -> str:
    return f"eccomi-evs-{token}.mp4"


def _extract_first_http_url(text: str) -> Optional[str]:
    if not text:
        return None
    urls = re.findall(r'https?://\S+', text)
    return urls[0] if urls else None


# =====================================================
# TEXT SANITIZATION (anti emoji / caratteri strani)
# =====================================================
def sanitize_text(text: str) -> str:
    """
    Rende il testo 'safe' per pipeline esterne.
    - normalizza newline
    - rimuove caratteri di controllo
    - rimuove emoji/simboli non-ASCII (anti crash)
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or (ord(ch) >= 32 and ord(ch) != 127))
    text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")
    text = text.encode("ascii", "ignore").decode("ascii", "ignore")
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    if len(text) > 4000:
        text = text[:4000].rstrip()

    return text


# =====================================================
# SUPABASE VIDEO UPLOAD / URL
# =====================================================
def supabase_public_video_url(token: str) -> str:
    if not supabase:
        raise RuntimeError("Supabase not connected")
    file_name = f"{token}.mp4"
    return supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).get_public_url(file_name)


def upload_video_to_supabase(order_id: str, source_url: str) -> Optional[str]:
    """
    Scarica il video (URL temporaneo RunPod) e lo salva su Supabase bucket videos.
    Ritorna l'URL pubblico.
    """
    if not supabase:
        print("❌ Supabase non configurato")
        return None

    try:
        print(f"⬇️ Download video da sorgente: {source_url}")
        r = requests.get(source_url, timeout=VIDEO_DOWNLOAD_TIMEOUT)
        r.raise_for_status()

        file_name = f"{order_id}.mp4"

        print(f"☁️ Upload su Supabase bucket '{SUPABASE_VIDEOS_BUCKET}': {file_name}")
        supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).upload(
            path=file_name,
            file=r.content,
            file_options={
                "content-type": "video/mp4",
                "x-upsert": "true"
            }
        )

        public_url = supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).get_public_url(file_name)
        print("✅ Upload completato:", public_url)
        return public_url

    except Exception as e:
        print("❌ Errore upload Supabase:", e)
        return None


def delete_expired_videos(days: int = 10):
    """
    Cancella i file nel bucket 'videos' più vecchi di X giorni.
    Endpoint manuale /admin/cleanup.
    """
    if not supabase:
        return

    try:
        files = supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).list()
        now = datetime.utcnow()

        for f in files or []:
            # created_at è ISO con Z
            created_at = (f.get("created_at") or "").replace("Z", "")
            if not created_at:
                continue

            try:
                created = datetime.fromisoformat(created_at)
            except Exception:
                continue

            age_days = (now - created).days
            if age_days > days:
                name = f.get("name")
                if name:
                    print("🗑️ Cancello:", name)
                    supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).remove([name])

    except Exception as e:
        print("Errore cleanup:", e)


# =====================================================
# SHOPIFY: FULFILL + NOTIFICA
# =====================================================
def shopify_headers():
    return {
        "X-Shopify-Access-Token": SHOP_ADMIN_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_fulfillment_orders(order_id: str) -> list:
    url = f"https://{SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/fulfillment_orders.json"
    r = requests.get(url, headers=shopify_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("fulfillment_orders", []) or []


def create_fulfillment(order_id: str, message: str, view_link: str):
    """
    Crea un fulfillment e notifica il cliente (email Shopify).
    """
    if not SHOP_DOMAIN or not SHOP_ADMIN_TOKEN:
        print("⚠️ Shopify ENV missing (SHOP_DOMAIN/SHOP_ADMIN_TOKEN) -> skip fulfillment")
        return

    fos = get_fulfillment_orders(order_id)
    if not fos:
        print("⚠️ No fulfillment_orders found -> skip fulfillment")
        return

    fo_id = fos[0].get("id")
    if not fo_id:
        print("⚠️ fulfillment_order_id missing -> skip fulfillment")
        return

    url = f"https://{SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/fulfillments.json"
    payload = {
        "fulfillment": {
            "line_items_by_fulfillment_order": [{"fulfillment_order_id": fo_id}],
            "notify_customer": True,
            "tracking_info": {
                "number": "EVS",
                "company": "Eccomi Online",
                "url": view_link
            },
            "message": message
        }
    }

    r = requests.post(url, headers=shopify_headers(), json=payload, timeout=30)
    print("✅ Shopify fulfillment status:", r.status_code)
    print("Shopify fulfillment response:", r.text)


# =====================================================
# RUNPOD
# =====================================================
def runpod_status(job_id: str):
    r = requests.get(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
        timeout=30
    )
    return r.json()


def poll_runpod(order_id: str, job_id: str):
    waited = 0
    while waited < RUNPOD_POLL_MAX_SECONDS:
        try:
            s = runpod_status(job_id)
            status = (s.get("status", "") or "").upper()
            print("🔎 RUNPOD STATUS:", status)

            if status == "COMPLETED":
                video_url = (
                    (s.get("output", {}) or {}).get("video_url")
                    or (s.get("output", {}) or {}).get("url")
                )

                if not video_url:
                    logs = s.get("logs", "")
                    video_url = _extract_first_http_url(logs)

                print("🎥 SOURCE VIDEO URL:", video_url)

                # 1) Upload finale su Supabase
                supa_url = upload_video_to_supabase(order_id, video_url) if video_url else None

                # 2) Link pagina brand (solo download) — NON Render
                delivery_page = f"{PUBLIC_BASE_URL}/video/{order_id}" if PUBLIC_BASE_URL else f"/video/{order_id}"
                direct_download = f"{PUBLIC_BASE_URL}/video/{order_id}/download" if PUBLIC_BASE_URL else f"/video/{order_id}/download"

                update_meta(order_id, {
                    "status": "DONE",
                    "video_url_source": video_url,
                    "video_supabase_url": supa_url,
                    "delivery_page": delivery_page,
                    "download_link": direct_download
                })

                # 3) Supabase table update
                shopify_order_id = None
                if supabase:
                    try:
                        supabase.table("video_jobs").update({
                            "status": "done",
                            # qui salviamo sempre il link "pulito" (tuo dominio)
                            "video_url": delivery_page,
                            "runpod_job_id": job_id
                        }).eq("evs_token", order_id).execute()
                    except Exception as e:
                        print("⚠️ Supabase update error:", e)

                    try:
                        row = supabase.table("video_jobs").select("shopify_order_id").eq("evs_token", order_id).limit(1).execute()
                        if row and row.data and len(row.data) > 0:
                            shopify_order_id = row.data[0].get("shopify_order_id")
                    except Exception as e:
                        print("⚠️ Supabase select shopify_order_id error:", e)

                # 4) Fulfillment Shopify + email (solo download)
                if shopify_order_id:
                    msg = (
                        "🎬 Il tuo video EVS è pronto!\n\n"
                        "⬇️ Scaricalo da qui:\n"
                        f"{delivery_page}\n\n"
                        "Grazie per aver scelto Eccomi Online."
                    )
                    create_fulfillment(str(shopify_order_id), msg, delivery_page)
                else:
                    print("⚠️ shopify_order_id non disponibile -> skip fulfillment")

                return

            if status in ["FAILED", "CANCELLED"]:
                print("❌ RUNPOD FAILED")
                update_meta(order_id, {"status": "GPU_FAILED"})
                if supabase:
                    try:
                        supabase.table("video_jobs").update({"status": "failed"}).eq("evs_token", order_id).execute()
                    except Exception as e:
                        print("⚠️ Supabase failed update error:", e)
                return

        except Exception as e:
            print("⚠️ Polling error:", e)

        time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
        waited += RUNPOD_POLL_INTERVAL_SECONDS

    print("⏰ RUNPOD POLL TIMEOUT")
    update_meta(order_id, {"status": "POLL_TIMEOUT"})
    if supabase:
        try:
            supabase.table("video_jobs").update({"status": "timeout"}).eq("evs_token", order_id).execute()
        except Exception as e:
            print("⚠️ Supabase timeout update error:", e)


def runpod_submit(order_id: str, order_name: str, email: str):
    print("🚀 RUNPOD SUBMIT START:", order_id)
    print("Order name:", order_name)
    print("Email:", email)

    if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
        print("❌ RUNPOD ENV MISSING")
        update_meta(order_id, {"status": "RUNPOD_ENV_MISSING"})
        return

    order_dir = EVS_STORAGE / order_id
    meta = load_json(order_dir / "meta.json")

    raw_text = meta.get("script_text", "") or ""
    cleaned_text = sanitize_text(raw_text)

    if cleaned_text != raw_text:
        update_meta(order_id, {
            "script_text_original": raw_text,
            "script_text": cleaned_text,
            "script_text_sanitized": True
        })

    photo_url = f"{PUBLIC_BASE_URL}/evs/file/{order_id}/photo"
    audio_url = f"{PUBLIC_BASE_URL}/evs/file/{order_id}/audio" if meta.get("has_audio") else None

    payload = {
        "input": {
            "image_url": photo_url,
            "text": cleaned_text,
            "gender": meta.get("gender", "male"),
        }
    }
    if audio_url:
        payload["input"]["audio_url"] = audio_url

    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
        headers=headers,
        json=payload,
        timeout=RUNPOD_SUBMIT_TIMEOUT
    )

    print("RunPod response status:", r.status_code)
    print("RunPod response body:", r.text)

    if not r.ok:
        update_meta(order_id, {"status": "RUNPOD_SUBMIT_FAILED"})
        if supabase:
            try:
                supabase.table("video_jobs").update({"status": "submit_failed"}).eq("evs_token", order_id).execute()
            except Exception as e:
                print("⚠️ Supabase submit_failed update error:", e)
        return

    job_id = (r.json() or {}).get("id")
    if not job_id:
        update_meta(order_id, {"status": "NO_JOB_ID"})
        return

    update_meta(order_id, {"status": "PROCESSING_GPU", "runpod_id": job_id, "shopify_order": order_name})
    if supabase:
        try:
            supabase.table("video_jobs").update({"status": "processing", "runpod_job_id": job_id}).eq("evs_token", order_id).execute()
        except Exception as e:
            print("⚠️ Supabase processing update error:", e)

    poll_runpod(order_id, job_id)


# =====================================================
# FASTAPI
# =====================================================
app = FastAPI(title="EVS RunPod Engine v3.2 (Supabase Storage + Download Only)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health():
    return {
        "status": "online",
        "supabase": bool(supabase),
        "poll_max_seconds": RUNPOD_POLL_MAX_SECONDS,
        "poll_interval_seconds": RUNPOD_POLL_INTERVAL_SECONDS
    }


# =====================================================
# UPLOAD FILES (prima del pagamento)
# =====================================================
@app.post("/evs/order")
async def receive_order(
    email: str = Form(...),
    photo: UploadFile = File(...),
    audio: Optional[UploadFile] = File(None),
    script_text: str = Form(""),
    gender: str = Form("male"),
):
    token = str(uuid.uuid4())
    order_dir = EVS_STORAGE / token
    order_dir.mkdir(parents=True, exist_ok=True)

    (order_dir / "photo.png").write_bytes(await photo.read())

    has_audio = False
    if audio:
        has_audio = True
        (order_dir / "audio.wav").write_bytes(await audio.read())

    cleaned = sanitize_text(script_text or "")

    meta = {
        "email": email,
        "script_text": cleaned,
        "script_text_original": script_text,
        "script_text_sanitized": (cleaned != (script_text or "")),
        "gender": gender,
        "status": "WAITING_PAYMENT",
        "has_audio": has_audio,
        "created_at": now_iso()
    }
    (order_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"evs_token": token}


# =====================================================
# SHOPIFY WEBHOOK (NON BLOCCANTE + SUPABASE UPSERT)
# =====================================================
@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    verify_hmac(request, raw)
    payload = json.loads(raw.decode("utf-8"))

    order_name = payload.get("name", "")
    order_id = payload.get("id", "")
    email = payload.get("email", "")

    tokens = []
    for item in payload.get("line_items", []):
        for prop in item.get("properties", []):
            if prop.get("name") == "EVS Token":
                tokens.append(prop.get("value"))

    if not tokens:
        print("⚠️ NO EVS TOKEN FOUND - IGNORING ORDER")
        return {"ignored": "no_evs_token"}

    for tok in tokens:
        print(f"🚀 PROCESSING TOKEN: {tok}")

        update_meta(tok, {"status": "PAID", "shopify_order_id": str(order_id)})

        if supabase:
            try:
                supabase.table("video_jobs").upsert({
                    "evs_token": tok,
                    "customer_email": email,
                    "status": "paid",
                    "shopify_order_id": str(order_id),
                }).execute()
                print(f"✅ Supabase UPSERT OK: {tok}")
            except Exception as e:
                print(f"❌ Supabase UPSERT ERROR: {e}")

        bg.add_task(runpod_submit, tok, order_name, email)

    return {"ok": True}


# =====================================================
# SERVE FILES TO RUNPOD (photo/audio)
# =====================================================
@app.get("/evs/file/{order_id}/{kind}")
def serve_file(order_id: str, kind: str):
    order_dir = EVS_STORAGE / order_id

    if kind == "photo":
        path = order_dir / "photo.png"
    elif kind == "audio":
        path = order_dir / "audio.wav"
    else:
        raise HTTPException(400, "Invalid kind")

    if not path.exists():
        raise HTTPException(404, "File not found")

    ctype, _ = mimetypes.guess_type(str(path))
    return Response(content=path.read_bytes(), media_type=ctype or "application/octet-stream")


# =====================================================
# VIDEO PAGE (SOLO DOWNLOAD, NO PLAYER)
# =====================================================
@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):
    if not supabase:
        raise HTTPException(500, "Storage not available")

    # Button -> /download (che fa redirect al file supabase)
    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download" if PUBLIC_BASE_URL else f"/video/{token}/download"

    html = f"""
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>EVS — Download</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto; background:#0b1b33; color:#fff; margin:0;}}
    .wrap{{max-width:720px;margin:0 auto;padding:28px;}}
    .card{{background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.12); border-radius:18px; padding:22px; text-align:center;}}
    h1{{margin:0 0 10px 0; font-size:22px;}}
    p{{opacity:.9; line-height:1.45; margin:0 0 16px 0;}}
    a.btn{{display:inline-block; padding:14px 18px; border-radius:14px; text-decoration:none; color:#0b1b33; background:#fff; font-weight:800;}}
    .note{{margin-top:14px; font-size:13px; opacity:.75;}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>🎬 Il tuo video è pronto</h1>
      <p>Premi il pulsante qui sotto per scaricare il file MP4.</p>
      <a class="btn" href="{download_url}">⬇️ Scarica MP4</a>
      <div class="note">Per sicurezza, salva il file sul tuo dispositivo. Il link potrebbe scadere dopo alcuni giorni.</div>
    </div>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=html)


@app.get("/video/{token}/download")
def video_download(token: str):
    if not supabase:
        raise HTTPException(500, "Storage not available")

    # Redirect al file pubblico su Supabase
    try:
        url = supabase_public_video_url(token)
    except Exception:
        raise HTTPException(404, "Video not ready")

    return RedirectResponse(url=url, status_code=302)


# =====================================================
# ADMIN CLEANUP (MANUALE)
# =====================================================
@app.post("/admin/cleanup")
def cleanup():
    delete_expired_videos(days=10)
    return {"ok": True, "deleted_older_than_days": 10}


# =====================================================
# RETRY FAILED JOB (SUPABASE BASED)
# =====================================================
@app.post("/evs/retry/{evs_token}")
async def evs_retry(evs_token: str, bg: BackgroundTasks):
    if not supabase:
        raise HTTPException(500, "Supabase not connected")

    result = supabase.table("video_jobs") \
        .select("*") \
        .eq("evs_token", evs_token) \
        .single() \
        .execute()

    if not result.data:
        raise HTTPException(404, "Token not found")

    row = result.data
    email = row.get("customer_email", "")
    order_name = row.get("shopify_order_id", "EVS")

    update_meta(evs_token, {"status": "RETRYING"})

    supabase.table("video_jobs").update({
        "status": "retrying"
    }).eq("evs_token", evs_token).execute()

    print(f"🔁 RETRYING JOB: {evs_token}")

    bg.add_task(runpod_submit, evs_token, str(order_name), str(email))

    return {"ok": True, "evs_token": evs_token, "status": "retrying"}
