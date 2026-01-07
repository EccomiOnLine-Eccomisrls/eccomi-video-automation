from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import requests
import os

router = APIRouter()

HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
HEYGEN_BASE = "https://api.heygen.com/v2"

AVATAR_ID = "bc09ef5d3e8641699e451c77ebc9054a"
VOICE_ID = "1753e5984bca4125a3e727d5d5e07ee2"  # ok quello che stai usando

class GenerateAvatarVideoBody(BaseModel):
    text: str

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
        "resolution": "720p"
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
        raise HTTPException(500, f"HeyGen error: {r.text}")

    return r.json()
