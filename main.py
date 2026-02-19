import os
import hmac
import hashlib
import base64
import time
import json
import requests
import uuid
import mimetypes

from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse

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

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
EVS_STORAGE_DIR = os.getenv("EVS_STORAGE_DIR", "data/evs_orders")

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


# =====================================================
# RUNPOD
# =====================================================

def runpod_submit(order_id: str, order_name: str, email: str):

    if not RUNPOD_API_KEY or not RUNPOD_ENDPOINT_ID:
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

    if not r.ok:
        update_meta(order_id, {"status": "RUNPOD_SUBMIT_FAILED"})
        return

    job_id = r.json().get("id")
    if not job_id:
        update_meta(order_id, {"status": "NO_JOB_ID"})
        return

    update_meta(order_id, {
        "status": "PROCESSING_GPU",
        "runpod_id": job_id,
        "shopify_order": order_name
    })

    poll_runpod(order_id, job_id)


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

        s = runpod_status(job_id)
        status = s.get("status", "").upper()

        if status == "COMPLETED":
            video_url = (
                s.get("output", {}).get("video_url")
                or s.get("output", {}).get("url")
            )

            update_meta(order_id, {
                "status": "DONE",
                "video_url": video_url
            })

            if supabase:
                supabase.table("video_jobs").update({
                    "status": "done",
                    "video_url": video_url
                }).eq("evs_token", order_id).execute()

            return

        if status in ["FAILED", "CANCELLED"]:
            update_meta(order_id, {"status": "GPU_FAILED"})
            return

        time.sleep(8)
        waited += 8

    update_meta(order_id, {"status": "POLL_TIMEOUT"})


# =====================================================
# FASTAPI
# =====================================================

app = FastAPI(title="EVS RunPod Engine v3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "online"}


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

    photo_path = order_dir / "photo.png"
    photo_path.write_bytes(await photo.read())

    has_audio = False
    if audio:
        has_audio = True
        audio_path = order_dir / "audio.wav"
        audio_path.write_bytes(await audio.read())

    meta = {
        "email": email,
        "script_text": script_text,
        "gender": gender,
        "status": "WAITING_PAYMENT",
        "has_audio": has_audio,
        "created_at": now_iso()
    }

    (order_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    return {"evs_token": token}


# =====================================================
# SHOPIFY WEBHOOK (NON BLOCCANTE)
# =====================================================

@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):

    raw = await request.body()
    verify_hmac(request, raw)

    payload = json.loads(raw)

    if payload.get("financial_status") != "paid":
        return {"ignored": "not_paid"}

    order_name = payload.get("name")
    email = payload.get("email")

    tokens: List[str] = []

for item in payload.get("line_items", []):
    for prop in item.get("properties", []):
        if prop.get("name") in ["EVS Token", "EVS Order ID"]:
            tokens.append(prop.get("value"))

for tok in tokens:
    update_meta(tok, {"status": "PAID"})
    bg.add_task(runpod_submit, tok, order_name, email)

return {"processed": tokens}


# =====================================================
# SERVE FILES TO RUNPOD
# =====================================================

@app.get("/evs/file/{order_id}/{kind}")
def serve_file(order_id: str, kind: str):

    order_dir = EVS_STORAGE / order_id
    meta = load_json(order_dir / "meta.json")

    if kind == "photo":
        path = order_dir / "photo.png"
    elif kind == "audio":
        path = order_dir / "audio.wav"
    else:
        raise HTTPException(400)

    if not path.exists():
        raise HTTPException(404)

    ctype, _ = mimetypes.guess_type(str(path))
    return Response(content=path.read_bytes(), media_type=ctype or "application/octet-stream")
