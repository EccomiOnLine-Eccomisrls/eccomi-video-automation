from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
import requests
import os

router = APIRouter()

# ======================================================
# CONFIG
# ======================================================

HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
HEYGEN_BASE = "https://api.heygen.com/v2"

CALLBACK_URL = "https://eccomi-video-automation.onrender.com/api/evs/heygen/webhook"

VOICE_ID = "1753e5984bca4125a3e727d5d5e07ee2"

if not HEYGEN_API_KEY:
    print("⚠️ HEYGEN_API_KEY mancante")

# ======================================================
# MODELS
# ======================================================

class GenerateVideoBody(BaseModel):
    text: str
    image_url: str  # OBBLIGATORIO (foto volto)


# ======================================================
# GENERATE VIDEO (TALKING PHOTO – API SAFE)
# ======================================================

@router.post("/api/evs/heygen/video")
def generate_video(body: GenerateVideoBody):
    if not HEYGEN_API_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")

    # 1️⃣ CREA TALKING PHOTO
    tp = requests.post(
        f"{HEYGEN_BASE}/talking-photo",
        headers={
            "Authorization": f"Bearer {HEYGEN_API_KEY}",
            "Content-Type": "application/json"
        },
        json={"image_url": body.image_url},
        timeout=30
    )

    if tp.status_code != 200:
        raise HTTPException(500, f"Talking photo error: {tp.text}")

    talking_photo_id = tp.json()["data"]["talking_photo_id"]

    # 2️⃣ GENERA VIDEO
    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "talking_photo",
                    "talking_photo_id": talking_photo_id
                },
                "voice": {
                    "type": "text",
                    "voice_id": VOICE_ID,
                    "input_text": body.text
                }
            }
        ],
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "callback_url": CALLBACK_URL
    }

    r = requests.post(
        f"{HEYGEN_BASE}/video/generate",
        headers={
            "X-Api-Key": HEYGEN_API_KEY,
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60
    )

    if r.status_code != 200:
        raise HTTPException(500, f"HeyGen generate error: {r.text}")

    return r.json()


# ======================================================
# WEBHOOK
# ======================================================

@router.post("/api/evs/heygen/webhook")
async def heygen_webhook(request: Request):
    payload = await request.json()

    print("🎬 WEBHOOK HEYGEN")
    print(payload)

    event = payload.get("event_type")
    data = payload.get("data", {})

    if event == "video.completed":
        print("✅ VIDEO COMPLETATO")
        print("ID:", data.get("video_id"))
        print("URL:", data.get("video_url"))

    elif event == "video.failed":
        print("❌ VIDEO FALLITO")
        print(data)

    return {"status": "ok"}
