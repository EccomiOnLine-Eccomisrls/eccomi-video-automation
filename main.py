import os
import hmac
import hashlib
import base64
import time
import json
import requests
import uuid
import mimetypes
from typing import Optional, Dict, Any
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse

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

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")  # es: https://eccomi-video-automation.onrender.com
EVS_STORAGE_DIR = os.getenv("EVS_STORAGE_DIR", "data/evs_orders")

# Shopify Admin (per EVASO + email notifica)
SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")  # es: eccomionline.myshopify.com
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

# =====================================================
# STORAGE
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

def now_iso():
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
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


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


def download_to_file(url: str, dest: Path, timeout=120):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)

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
    Usiamo fulfillment_orders (API nuova) per massima compatibilità.
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
            "line_items_by_fulfillment_order": [
                {"fulfillment_order_id": fo_id}
            ],
            "notify_customer": True,
            # tracking_info serve anche per far comparire un link “carino” al cliente
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
    while waited < 1800:
        try:
            s = runpod_status(job_id)
            status = s.get("status", "").upper()
            print("🔎 RUNPOD STATUS:", status)

            if status == "COMPLETED":
                video_url = (
                    s.get("output", {}).get("video_url")
                    or s.get("output", {}).get("url")
                )

                if not video_url:
                    logs = s.get("logs", "")
                    if logs and "http" in logs:
                        import re
                        urls = re.findall(r'https?://\S+', logs)
                        if urls:
                            video_url = urls[0]

                print("🎥 SOURCE VIDEO URL:", video_url)

                # 1) scarica in locale e crea link eccomi
                local_path = EVS_STORAGE / order_id / "video.mp4"
                if video_url:
                    try:
                        download_to_file(video_url, local_path)
                        print("✅ Video salvato in locale:", str(local_path))
                    except Exception as e:
                        print("⚠️ Download locale fallito:", e)

                view_link = f"{PUBLIC_BASE_URL}/video/{order_id}" if PUBLIC_BASE_URL else ""
                download_link = f"{PUBLIC_BASE_URL}/video/{order_id}/download" if PUBLIC_BASE_URL else ""

                update_meta(order_id, {
                    "status": "DONE",
                    "video_url_source": video_url,
                    "video_local": str(local_path) if local_path.exists() else None,
                    "video_view_link": view_link,
                    "video_download_link": download_link
                })

                # 2) supabase update
                shopify_order_id = None
                if supabase:
                    try:
                        supabase.table("video_jobs").update({
                            "status": "done",
                            # mettiamo il link ECcomi dentro video_url (così è quello che userai sempre)
                            "video_url": view_link or video_url,
                            "runpod_job_id": job_id
                        }).eq("evs_token", order_id).execute()
                    except Exception as e:
                        print("⚠️ Supabase update error:", e)

                    # prova a leggere shopify_order_id per fare fulfillment
                    try:
                        row = supabase.table("video_jobs").select("shopify_order_id").eq("evs_token", order_id).limit(1).execute()
                        if row and row.data and len(row.data) > 0:
                            shopify_order_id = row.data[0].get("shopify_order_id")
                    except Exception as e:
                        print("⚠️ Supabase select shopify_order_id error:", e)

                # 3) fulfillment Shopify + email
                if shopify_order_id:
                    msg = (
                        "🎬 Il tuo video EVS è pronto!\n\n"
                        f"✅ Guarda qui:\n{view_link or video_url}\n\n"
                        f"⬇️ Download diretto:\n{download_link or video_url}\n\n"
                        "Grazie per aver scelto Eccomi Online."
                    )
                    create_fulfillment(str(shopify_order_id), msg, (view_link or video_url or ""))
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

        time.sleep(8)
        waited += 8

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

    photo_url = f"{PUBLIC_BASE_URL}/evs/file/{order_id}/photo"
    audio_url = f"{PUBLIC_BASE_URL}/evs/file/{order_id}/audio" if meta.get("has_audio") else None

    payload = {
        "input": {
            "image_url": photo_url,
            "text": meta.get("script_text", ""),
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
        timeout=45
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

    job_id = r.json().get("id")
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

app = FastAPI(title="EVS RunPod Engine v3.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "online", "supabase": bool(supabase)}

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

    meta = {
        "email": email,
        "script_text": script_text,
        "gender": gender,
        "status": "WAITING_PAYMENT",
        "has_audio": has_audio,
        "created_at": now_iso()
    }
    (order_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

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
# SERVE FILES TO RUNPOD
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
# VIDEO VIEW + DOWNLOAD (LINK "ECOMMI")
# =====================================================

@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):
    order_dir = EVS_STORAGE / token
    video_path = order_dir / "video.mp4"
    if not video_path.exists():
        raise HTTPException(404, "Video not ready")

    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download" if PUBLIC_BASE_URL else f"/video/{token}/download"
    stream_url = f"{PUBLIC_BASE_URL}/video/{token}/stream" if PUBLIC_BASE_URL else f"/video/{token}/stream"

    html = f"""
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>EVS — Video pronto</title>
  <style>
    body{{font-family:system-ui,-apple-system,Segoe UI,Roboto; background:#0b1b33; color:#fff; margin:0;}}
    .wrap{{max-width:860px;margin:0 auto;padding:24px;}}
    .card{{background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.12); border-radius:16px; padding:18px;}}
    h1{{margin:0 0 8px 0; font-size:22px;}}
    p{{opacity:.9; line-height:1.4;}}
    .btns{{display:flex; gap:12px; flex-wrap:wrap; margin-top:14px;}}
    a.btn{{display:inline-block; padding:12px 14px; border-radius:12px; text-decoration:none; color:#0b1b33; background:#fff; font-weight:700;}}
    a.btn.secondary{{background:transparent; color:#fff; border:1px solid rgba(255,255,255,.35);}}
    video{{width:100%; border-radius:14px; margin-top:14px; background:#000;}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>🎬 Il tuo video EVS è pronto</h1>
      <p>Puoi guardarlo qui sotto oppure scaricarlo in MP4.</p>
      <div class="btns">
        <a class="btn" href="{download_url}">⬇️ Scarica MP4</a>
        <a class="btn secondary" href="https://eccomionline.com" target="_blank">Eccomi Online</a>
      </div>
      <video controls playsinline src="{stream_url}"></video>
    </div>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=html)

@app.get("/video/{token}/stream")
def video_stream(token: str):
    order_dir = EVS_STORAGE / token
    video_path = order_dir / "video.mp4"
    if not video_path.exists():
        raise HTTPException(404, "Video not ready")
    return Response(content=video_path.read_bytes(), media_type="video/mp4")

@app.get("/video/{token}/download")
def video_download(token: str):
    order_dir = EVS_STORAGE / token
    video_path = order_dir / "video.mp4"
    if not video_path.exists():
        raise HTTPException(404, "Video not ready")

    filename = safe_filename(token)
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return Response(content=video_path.read_bytes(), media_type="video/mp4", headers=headers)

# =====================================================
# RETRY FAILED JOB (SUPABASE BASED)
# =====================================================

@app.post("/evs/retry/{evs_token}")
async def evs_retry(evs_token: str, bg: BackgroundTasks):

    if not supabase:
        raise HTTPException(500, "Supabase not connected")

    # 1️⃣ Recupera dati dal database
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

    # 2️⃣ Aggiorna stato in Supabase
    supabase.table("video_jobs").update({
        "status": "retrying"
    }).eq("evs_token", evs_token).execute()

    print(f"🔁 RETRYING JOB: {evs_token}")

    # 3️⃣ Riavvia RunPod
    bg.add_task(runpod_submit, evs_token, str(order_name), str(email))

    return {
        "ok": True,
        "evs_token": evs_token,
        "status": "retrying"
    }
