import os
import hmac
import hashlib
import base64
import time
import json
import requests
import uuid
import re

from typing import Optional, Dict, Any
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

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
def upload_input_to_supabase(token, kind, content, content_type):
    if not supabase:
        return None

    path = f"{token}/{kind}.dat"

    supabase.storage.from_(SUPABASE_INPUTS_BUCKET).upload(
        path=path,
        file=content,
        file_options={"content-type": content_type, "x-upsert": "true"}
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
                    supa_url = upload_video_to_supabase(token, video_url)
                    delivery_page = f"{PUBLIC_BASE_URL}/video/{token}"

                    supabase.table("video_jobs").update({
                        "status": "done",
                        "video_url": delivery_page,
                        "video_supabase_url": supa_url,
                        "runpod_job_id": job_id,
                        "updated_at": now_iso()
                    }).eq("evs_token", token).execute()

                    return

            if status in ["FAILED", "CANCELLED"]:
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
            "gender": row.get("gender", "male"),
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
    gender: str = Form("male"),
    evs_token: Optional[str] = Form(None)
):
    token = (evs_token or "").strip() or str(uuid.uuid4())

    # =============================
    # Upload FOTO su Supabase
    # =============================
    photo_bytes = await photo.read()

    photo_url = None
    if photo_bytes and len(photo_bytes) > 0:
        photo_url = upload_input_to_supabase(
            token,
            "photo.png",
            photo_bytes,
            photo.content_type or "image/png"
        )

    # =============================
    # Upload AUDIO su Supabase
    # =============================
    audio_url = None

    if audio is not None:
        audio_bytes = await audio.read()

        if audio_bytes and len(audio_bytes) > 0:
            audio_url = upload_input_to_supabase(
                token,
                "audio.wav",
                audio_bytes,
                "audio/wav"
            )

    # =============================
    # Salvataggio DB
    # =============================
    if supabase:
        supabase.table("video_jobs").upsert({
            "evs_token": token,
            "customer_email": email,
            "status": "waiting_payment",
            "gender": gender,
            "script_text": sanitize_text(script_text),
            "photo_url": photo_url,
            "audio_url": audio_url,
            "has_audio": bool(audio_url),
            "updated_at": now_iso()
        }).execute()

    # =============================
    # RESPONSE
    # =============================
    return {
        "ok": True,
        "evs_token": token,
        "photo_url": photo_url,
        "audio_url": audio_url
    }

    return {"ok": True, "evs_token": token}

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

                    supabase.table("video_jobs").update({
                        "status": "paid",
                        "shopify_order_id": str(data.get("id")),
                        "updated_at": now_iso()
                    }).eq("evs_token", tok).execute()

                    bg.add_task(runpod_submit, tok, data.get("name"), data.get("email"))

    return {"ok": True}

@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):
    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download"
    return HTMLResponse(f"""
        <html>
        <body style='background:#0b1b33;color:#fff;text-align:center;padding:50px;'>
        <h1>🎬 Video Pronto</h1>
        <br>
        <a href='{download_url}' style='background:#fff;color:#0b1b33;padding:15px;text-decoration:none;font-weight:bold;border-radius:10px;'>
        ⬇️ Scarica MP4
        </a>
        </body>
        </html>
    """)

@app.get("/video/{token}/download")
def video_download(token: str):
    url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"
    return RedirectResponse(url=url)
