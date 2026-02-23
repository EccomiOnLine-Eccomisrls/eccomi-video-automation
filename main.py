import os
import hmac
import hashlib
import base64
import time
import json
import requests
import uuid
import re

from typing import Optional
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

# =====================================================
# ENV & CONFIG
# =====================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-01")

SUPABASE_INPUTS_BUCKET = os.getenv("SUPABASE_INPUTS_BUCKET", "inputs")
SUPABASE_VIDEOS_BUCKET = os.getenv("SUPABASE_VIDEOS_BUCKET", "videos")

RUNPOD_POLL_MAX_SECONDS = int(os.getenv("RUNPOD_POLL_MAX_SECONDS", "3600"))
RUNPOD_POLL_INTERVAL_SECONDS = int(os.getenv("RUNPOD_POLL_INTERVAL_SECONDS", "8"))

# =====================================================
# SUPABASE CLIENT (Lazy Import per evitare errori)
# =====================================================
from supabase import create_client, Client
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase collegato (v3.6 - FIX)")
    except Exception as e:
        print("⚠️ Supabase error:", e)

# =====================================================
# UTILS
# =====================================================
def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def sanitize_text(text: str) -> str:
    if not text: return ""
    text = text.encode("ascii", "ignore").decode("ascii", "ignore")
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text[:4000]

def verify_hmac(request: Request, raw: bytes):
    if not VERIFY_SHOPIFY_HMAC: return
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    received = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Invalid HMAC")

# =====================================================
# STORAGE & LOGIC
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
        print(f"❌ Error upload inputs: {e}")
        return None

def get_supabase_video_url(token: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/evs/{token}.mp4"

def create_fulfillment(order_id: str, view_link: str):
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
                "message": "Il tuo video EVS è pronto! Scaricalo qui."
            }
        }
        requests.post(url_f, headers=headers, json=payload, timeout=30)
        print(f"✅ Shopify Fulfillment inviato per ordine {order_id}")
    except Exception as e:
        print(f"⚠️ Shopify error: {e}")

# =====================================================
# POLLING RUNPOD (v3.6)
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
            
            if status == "COMPLETED":
                # Con la v3.6 il video è già su Supabase, verifichiamo solo l'URL
                output = s.get("output", {})
                final_video_url = output.get("video_url") 

                if final_video_url:
                    delivery_page = f"{PUBLIC_BASE_URL}/video/{order_id}"
                    if supabase:
                        supabase.table("video_jobs").update({
                            "status": "done",
                            "video_url": delivery_page,
                            "video_supabase_url": final_video_url,
                            "updated_at": now_iso()
                        }).eq("evs_token", order_id).execute()
                        
                        row = supabase.table("video_jobs").select("shopify_order_id").eq("evs_token", order_id).single().execute()
                        shopify_order_id = row.data.get("shopify_order_id") if row.data else None
                        if shopify_order_id:
                            create_fulfillment(str(shopify_order_id), delivery_page)
                    return

            if status in ["FAILED", "CANCELLED"]:
                if supabase:
                    supabase.table("video_jobs").update({"status": "failed", "updated_at": now_iso()}).eq("evs_token", order_id).execute()
                return
        except Exception as e:
            print(f"⚠️ Polling error: {e}")
        
        time.sleep(RUNPOD_POLL_INTERVAL_SECONDS)
        waited += RUNPOD_POLL_INTERVAL_SECONDS

def runpod_submit(order_id: str):
    if not supabase: return
    row_res = supabase.table("video_jobs").select("*").eq("evs_token", order_id).execute()
    if not row_res.data: return
    row = row_res.data[0]

    payload = {
        "input": {
            "image_url": row.get("photo_url"),
            "text": row.get("script_text"),
            "gender": row.get("gender"),
            "token": order_id
        }
    }
    if row.get("audio_url"): payload["input"]["audio_url"] = row.get("audio_url")

    r = requests.post(
        f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}, json=payload, timeout=45
    )
    job_id = r.json().get("id")
    if job_id:
        supabase.table("video_jobs").update({"status": "processing", "runpod_job_id": job_id}).eq("evs_token", order_id).execute()
        poll_runpod(order_id, job_id)

# =====================================================
# FASTAPI APP
# =====================================================
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def health(): return {"status": "v3.6-fix-online"}

@app.post("/evs/order")
async def receive_order(
    email: str = Form(...), photo: UploadFile = File(...), audio: Optional[UploadFile] = File(None),
    script_text: str = Form(""), gender: str = Form("male")
):
    token = str(uuid.uuid4())
    p_bytes = await photo.read()
    p_url = upload_input_to_supabase(token, "photo", p_bytes, "image/png")
    a_url = None
    if audio:
        a_bytes = await audio.read()
        a_url = upload_input_to_supabase(token, "audio", a_bytes, "audio/wav")

    if supabase:
        supabase.table("video_jobs").insert({
            "evs_token": token, "customer_email": email, "status": "waiting_payment",
            "gender": gender, "script_text": sanitize_text(script_text),
            "photo_url": p_url, "audio_url": a_url, "has_audio": bool(a_url)
        }).execute()
    return {"ok": True, "evs_token": token}

@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    verify_hmac(request, raw)
    data = json.loads(raw.decode("utf-8"))
    tokens = []
    for item in data.get("line_items", []):
        for prop in item.get("properties", []):
            if prop.get("name") == "EVS Token": tokens.append(prop.get("value"))
    for tok in tokens:
        if supabase:
            supabase.table("video_jobs").upsert({
                "evs_token": tok, "status": "paid",
                "shopify_order_id": str(data.get("id")),
                "shopify_order_name": data.get("name"),
                "updated_at": now_iso()
            }, on_conflict="evs_token").execute()
        bg.add_task(runpod_submit, tok)
    return {"ok": True}

@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):
    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download"
    return HTMLResponse(content=f"""
        <html><body style="background:#0b1b33;color:#fff;text-align:center;font-family:sans-serif;padding:50px;">
        <h1>🎬 Video Pronto</h1><br><a href="{download_url}" style="background:#fff;color:#0b1b33;padding:15px 25px;border-radius:10px;text-decoration:none;font-weight:bold;">⬇️ SCARICA MP4</a>
        </body></html>""")

@app.get("/video/{token}/download")
def video_download(token: str):
    return RedirectResponse(get_supabase_video_url(token))

@app.post("/evs/retry/{evs_token}")
async def evs_retry(evs_token: str, bg: BackgroundTasks):
    bg.add_task(runpod_submit, evs_token)
    return {"ok": True, "status": "retrying"}
