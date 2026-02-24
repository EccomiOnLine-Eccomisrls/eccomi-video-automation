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
EVS_STORAGE_DIR = os.getenv("EVS_STORAGE_DIR", "data/evs_orders")

SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

RUNPOD_POLL_MAX_SECONDS = int(os.getenv("RUNPOD_POLL_MAX_SECONDS", "3600"))
RUNPOD_POLL_INTERVAL_SECONDS = int(os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "8"))
RUNPOD_SUBMIT_TIMEOUT = int(os.getenv("RUNPOD_SUBMIT_TIMEOUT", "45"))
VIDEO_DOWNLOAD_TIMEOUT = int(os.getenv("VIDEO_DOWNLOAD_TIMEOUT", "600"))

SUPABASE_INPUTS_BUCKET = os.getenv("SUPABASE_INPUTS_BUCKET", "inputs")
SUPABASE_VIDEOS_BUCKET = os.getenv("SUPABASE_VIDEOS_BUCKET", "videos")
SUPABASE_VIDEO_UPLOAD_RETRIES = int(os.getenv("SUPABASE_VIDEO_UPLOAD_RETRIES", "3"))

# =====================================================
# STORAGE (debug locale)
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
# UTILS (Tutta la tua logica originale)
# =====================================================
def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def load_json(path: Path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}

def update_meta(order_id: str, changes: Dict[str, Any]):
    order_dir = EVS_STORAGE / order_id
    if not order_dir.exists(): order_dir.mkdir(parents=True, exist_ok=True)
    meta_path = order_dir / "meta.json"
    meta = load_json(meta_path)
    meta.update(changes)
    meta["updated_at"] = now_iso()
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

def verify_hmac(request: Request, raw: bytes):
    if not VERIFY_SHOPIFY_HMAC: return
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    received = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Invalid HMAC")

def _extract_first_http_url(text: str) -> Optional[str]:
    if not text: return None
    urls = re.findall(r'https?://\S+', text)
    return urls[0] if urls else None

def sanitize_text(text: str) -> str:
    if not text: return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or (ord(ch) >= 32 and ord(ch) != 127))
    text = text.encode("ascii", "ignore").decode("ascii", "ignore")
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text[:4000]

# =====================================================
# STORAGE & UPLOADS
# =====================================================
def upload_input_to_supabase(token: str, kind: str, content: bytes, content_type: str) -> Optional[str]:
    if not supabase: return None
    try:
        path = f"{token}/photo.png" if kind == "photo" else f"{token}/audio.wav"
        supabase.storage.from_(SUPABASE_INPUTS_BUCKET).upload(
            path=path, file=content, file_options={"content-type": content_type, "x-upsert": "true"}
        )
        return supabase.storage.from_(SUPABASE_INPUTS_BUCKET).get_public_url(path)
    except Exception as e:
        print(f"❌ Errore upload inputs ({kind}):", e)
        return None

def upload_video_to_supabase(order_id: str, source_url: str) -> Optional[str]:
    if not supabase: return None
    file_name = f"{order_id}.mp4"
    for attempt in range(1, SUPABASE_VIDEO_UPLOAD_RETRIES + 1):
        try:
            r = requests.get(source_url, timeout=VIDEO_DOWNLOAD_TIMEOUT)
            r.raise_for_status()
            supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).upload(
                path=file_name, file=r.content, file_options={"content-type": "video/mp4", "x-upsert": "true"}
            )
            return supabase.storage.from_(SUPABASE_VIDEOS_BUCKET).get_public_url(file_name)
        except Exception as e:
            print(f"❌ Upload video fallito (tentativo {attempt}):", e)
            time.sleep(2 ** attempt)
    return None

# =====================================================
# SHOPIFY
# =====================================================
def create_fulfillment(order_id: str, message: str, view_link: str):
    if not SHOP_DOMAIN or not SHOP_ADMIN_TOKEN: return
    try:
        url_fo = f"https://{SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/fulfillment_orders.json"
        headers = {"X-Shopify-Access-Token": SHOP_ADMIN_TOKEN, "Content-Type": "application/json"}
        r_fo = requests.get(url_fo, headers=headers, timeout=30)
        fos = r_fo.json().get("fulfillment_orders", [])
        if not fos: return
        
        fo_id = fos[0].get("id")
        url_f = f"https://{SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/fulfillments.json"
        payload = {
            "fulfillment": {
                "line_items_by_fulfillment_order": [{"fulfillment_order_id": fo_id}],
                "notify_customer": True,
                "tracking_info": {"number": "EVS", "company": "Eccomi Online", "url": view_link},
                "message": message
            }
        }
        requests.post(url_f, headers=headers, json=payload, timeout=30)
    except Exception as e:
        print(f"⚠️ Shopify fulfillment error: {e}")

# =====================================================
# RUNPOD ENGINE (Chiamate API pure)
# =====================================================
def poll_runpod(order_id: str, job_id: str):
    waited = 0
    while waited < RUNPOD_POLL_MAX_SECONDS:
        try:
            r = requests.get(
                f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}",
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}, timeout=30
            )
            s = r.json()
            status = (s.get("status", "")).upper()
            print(f"🔎 Status {order_id}: {status}")

            if status == "COMPLETED":
                output = s.get("output", {})
                video_url = output.get("video_url") or output.get("url")
                if not video_url and isinstance(output, list) and len(output)>0:
                    video_url = output[0] if isinstance(output[0], str) else output[0].get("video_url")
                
                if not video_url:
                    video_url = _extract_first_http_url(s.get("logs", ""))

                if video_url:
                    supa_url = upload_video_to_supabase(order_id, video_url)
                    delivery_page = f"{PUBLIC_BASE_URL}/video/{order_id}"
                    
                    if supabase:
                        supabase.table("video_jobs").update({
                            "status": "done" if supa_url else "upload_failed",
                            "video_url": delivery_page,
                            "video_supabase_url": supa_url,
                            "video_url_source": video_url,
                            "runpod_job_id": job_id,
                            "updated_at": now_iso()
                        }).eq("evs_token", order_id).execute()

                        # Recupero Shopify ID per fulfill
                        row = supabase.table("video_jobs").select("shopify_order_id").eq("evs_token", order_id).single().execute()
                        if row.data and row.data.get("shopify_order_id"):
                            msg = f"🎬 Il tuo video EVS è pronto!\n Scaricalo qui: {delivery_page}"
                            create_fulfillment(str(row.data["shopify_order_id"]), msg, delivery_page)
                    return

            if status in ["FAILED", "CANCELLED"]:
                if supabase: supabase.table("video_jobs").update({"status": "failed"}).eq("evs_token", order_id).execute()
                return
        except Exception as e:
            print("⚠️ Polling error:", e)
        
        time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
        waited += RUNPOD_POLL_INTERVAL_SECONDS

def runpod_submit(tok, name, email):
    if not supabase: return
    res = supabase.table("video_jobs").select("*").eq("evs_token", tok).limit(1).execute()
    if not res.data: return
    row = res.data

    payload = {
        "input": {
            "image_url": row.get("photo_url"),
            "text": sanitize_text(row.get("script_text")),
            "gender": row.get("gender", "male"),
            "token": order_id
        }
    }
    if row.get("audio_url"): payload["input"]["audio_url"] = row.get("audio_url")

    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
    r = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", headers=headers, json=payload, timeout=45)
    
    job_id = r.json().get("id")
    if job_id:
        supabase.table("video_jobs").update({"status": "processing", "runpod_job_id": job_id}).eq("evs_token", order_id).execute()
        poll_runpod(order_id, job_id)

# =====================================================
# FASTAPI APP (Ripristinata v3.5)
# =====================================================
app = FastAPI(title="EVS RunPod Engine v3.5")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def health(): return {"status": "online", "v": "3.5-FIXED"}

@app.post("/evs/order")
async def receive_order(
    email: str = Form(...),
    photo: UploadFile = File(...),
    audio: Optional[UploadFile] = File(None),
    script_text: str = Form(""),
    gender: str = Form("male"),
    evs_token: Optional[str] = Form(None),   # ✅ NEW: token riusabile
):
    # ✅ Se il client ci rimanda un token, riusiamo quello.
    token = (evs_token or "").strip() or str(uuid.uuid4())

    # Upload inputs (sempre x-upsert=true dentro upload_input_to_supabase)
    photo_bytes = await photo.read()
    photo_url = upload_input_to_supabase(token, "photo", photo_bytes, "image/png")

    audio_url = None
    if audio:
        audio_bytes = await audio.read()
        audio_url = upload_input_to_supabase(token, "audio", audio_bytes, "audio/wav")

    if supabase:
        # ✅ UPSERT: se esiste aggiorna, se non esiste crea.
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

    return {"ok": True, "evs_token": token}

@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):

    raw = await request.body()
    verify_hmac(request, raw)

    data = json.loads(raw.decode("utf-8"))

    print("==== WEBHOOK PAYLOAD ====")
    print(json.dumps(data, indent=2))
    print("==== END PAYLOAD ====")

    for item in data.get("line_items", []):
        for prop in item.get("properties", []):
            print("Property found:", prop)

            name = prop.get("name", "").strip().lower()

            if "evs" in name and "token" in name:
                tok = prop.get("value")

                if supabase:
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
    return HTMLResponse(content=f"<html><body style='background:#0b1b33;color:#fff;text-align:center;padding:50px;'><h1>🎬 Video Pronto</h1><br><a href='{download_url}' style='background:#fff;color:#0b1b33;padding:15px;text-decoration:none;font-weight:bold;border-radius:10px;'>⬇️ Scarica MP4</a></body></html>")

@app.get("/video/{token}/download")
def video_download(token: str):
    url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/{token}.mp4"
    return RedirectResponse(url=url)

@app.post("/evs/retry/{evs_token}")
async def evs_retry(evs_token: str, bg: BackgroundTasks):
    if not supabase:
        raise HTTPException(500, "Supabase not connected")

    # Togliamo .single() per evitare il crash se il token è errato
    result = supabase.table("video_jobs") \
        .select("*") \
        .eq("evs_token", evs_token) \
        .execute()

    if not result.data or len(result.data) == 0:
        # Invece di crashare, rispondiamo con un errore chiaro
        return JSONResponse(status_code=404, content={"ok": False, "error": "Token non trovato nel database"})

    row = result.data[0]
    email = row.get("customer_email", "")
    order_name = row.get("shopify_order_name") or row.get("shopify_order_id") or "EVS"

    # Procediamo con il retry
    supabase.table("video_jobs").update({
        "status": "retrying",
        "updated_at": now_iso()
    }).eq("evs_token", evs_token).execute()

    print(f"🔁 RETRYING JOB: {evs_token}")
    bg.add_task(runpod_submit, evs_token, str(order_name), str(email))

    return {"ok": True, "evs_token": evs_token, "status": "retrying"}
