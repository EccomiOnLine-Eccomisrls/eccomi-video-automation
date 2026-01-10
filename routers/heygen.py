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

# ✅ AVATAR API-SAFE (ORA)
AVATAR_ID = "alex"
AVATAR_STYLE = "normal"

# 🔊 Voce testata
VOICE_ID = "1753e5984bca4125a3e727d5d5e07ee2"

# 🔁 Callback asincrono EVS
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
        raise HTTPException(500, "HEYGEN_API_KEY mancante")

    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": AVATAR_ID,
                    "avatar_style": AVATAR_STYLE
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
        raise HTTPException(500, f"HeyGen generate error: {r.text}")

    return r.json()

# ======================================================
# WEBHOOK HEYGEN
# ======================================================

@router.post("/api/evs/heygen/webhook")
async def heygen_webhook(request: Request):
    payload = await request.json()

    print("🎬 WEBHOOK HEYGEN RICEVUTO")
    print(payload)

    event_type = payload.get("event_type")
    data = payload.get("data", {})

    if event_type == "video.completed":
        print("✅ VIDEO COMPLETATO")
        print("ID:", data.get("video_id"))
        print("URL:", data.get("video_url"))

        # QUI EVS FA LA MAGIA
        # - associa ordine
        # - notifica cliente
        # - abilita download

    elif event_type == "video.failed":
        print("❌ VIDEO FALLITO")
        print(data)

    return {"status": "ok"}
