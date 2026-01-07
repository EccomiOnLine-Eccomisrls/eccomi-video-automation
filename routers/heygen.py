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

# Avatar e voce (quelli che hai verificato funzionanti)
AVATAR_ID = "bc09ef5d3e8641699e451c77ebc9054a"
VOICE_ID = "1753e5984bca4125a3e727d5d5e07ee2"

CALLBACK_URL = "https://eccomi-video-automation.onrender.com/api/evs/heygen/webhook"

if not HEYGEN_API_KEY:
    print("⚠️ HEYGEN_API_KEY mancante")

# ======================================================
# MODELS
# ======================================================

class GenerateAvatarVideoBody(BaseModel):
    text: str


# ======================================================
# GENERATE AVATAR VIDEO
# ======================================================

@router.post("/api/evs/heygen/avatar")
def generate_avatar_video(body: GenerateAvatarVideoBody):
    if not HEYGEN_API_KEY:
        raise HTTPException(status_code=500, detail="HEYGEN_API_KEY mancante")

    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": AVATAR_ID,
                    "avatar_style": "normal"
                },
                "voice": {
                    "type": "text",
                    "voice_id": VOICE_ID,
                    "input_text": body.text
                },
                "background": {
                    "type": "color",
                    "value": "#FFFFFF"
                }
            }
        ],
        "aspect_ratio": "9:16",
        "resolution": "720p",

        # 🔥 CALLBACK ASINCRONO
        "callback_url": CALLBACK_URL
    }

    r = requests.post(
        f"{HEYGEN_BASE}/video/generate",
        headers={
            "X-Api-Key": HEYGEN_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        json=payload,
        timeout=60
    )

    if r.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"HeyGen generate error: {r.text}"
        )

    return r.json()


# ======================================================
# WEBHOOK HEYGEN (CALLBACK)
# ======================================================

@router.post("/api/evs/heygen/webhook")
async def heygen_webhook(request: Request):
    """
    Riceve eventi automatici da HeyGen
    """
    payload = await request.json()

    print("🎬 WEBHOOK HEYGEN RICEVUTO")
    print(payload)

    event_type = payload.get("event_type")
    data = payload.get("data", {})

    if event_type == "video.completed":
        video_id = data.get("video_id")
        video_url = data.get("video_url")
        thumbnail_url = data.get("thumbnail_url")

        print("✅ VIDEO COMPLETATO")
        print("ID:", video_id)
        print("URL:", video_url)

        # TODO:
        # - salva su DB
        # - associa a ordine Shopify
        # - invia WhatsApp / Email
        # - abilita download cliente

    elif event_type == "video.failed":
        print("❌ VIDEO FALLITO")
        print(data)

    return {"status": "ok"}
