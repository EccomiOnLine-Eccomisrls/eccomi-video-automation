from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
import requests
import os

router = APIRouter()

HEYGEN_BASE = "https://api.heygen.com/v2"
CALLBACK_URL = "https://eccomi-video-automation.onrender.com/api/evs/heygen/webhook"

AVATAR_ID = "alex"
AVATAR_STYLE = "normal"
VOICE_ID = "1753e5984bca4125a3e727d5d5e07ee2"


class GenerateAvatarVideoBody(BaseModel):
    text: str


@router.post("/api/evs/heygen/avatar")
def generate_avatar_video(body: GenerateAvatarVideoBody):

    # ✅ LEGGI ENV QUI, NON A LIVELLO GLOBALE
    heygen_api_key = os.getenv("HEYGEN_API_KEY")

    print("HEYGEN_API_KEY runtime =", bool(heygen_api_key))

    if not heygen_api_key:
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
            "X-Api-Key": heygen_api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        json=payload,
        timeout=60
    )

    if r.status_code != 200:
        raise HTTPException(500, f"HeyGen generate error: {r.text}")

    return r.json()


@router.post("/api/evs/heygen/webhook")
async def heygen_webhook(request: Request):
    payload = await request.json()
    print("🎬 WEBHOOK HEYGEN:", payload)
    return {"status": "ok"}
