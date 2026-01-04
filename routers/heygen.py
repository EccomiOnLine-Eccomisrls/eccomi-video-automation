from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import requests
import os

router = APIRouter()

# =========================
# ENV
# =========================

HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
HEYGEN_BASE = "https://api.heygen.com/v2"

if not HEYGEN_API_KEY:
    print("⚠️ HEYGEN_API_KEY mancante")

# =========================
# MODELS
# =========================

class CreateTalkingPhotoBody(BaseModel):
    image_url: str


class HeyGenSubmitBody(BaseModel):
    script: str
    image_url: str | None = None
    talking_photo_id: str | None = None
    voice_id: str | None = "it_male_energetic"


# =========================
# CREATE TALKING PHOTO
# =========================

@router.post("/api/heygen/talking-photo")
def create_talking_photo(body: CreateTalkingPhotoBody):
    if not HEYGEN_API_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")

    r = requests.post(
        f"{HEYGEN_BASE}/talking-photo",
        headers={
            # ⚠️ QUI SERVE BEARER
            "Authorization": f"Bearer {HEYGEN_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "image_url": body.image_url
        },
        timeout=30
    )

    if r.status_code != 200:
        raise HTTPException(500, f"Talking photo error: {r.text}")

    return r.json()


# =========================
# CREATE VIDEO (AUTO)
# =========================

@router.post("/api/heygen/submit")
def heygen_submit(body: HeyGenSubmitBody):
    if not HEYGEN_API_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")

    talking_photo_id = body.talking_photo_id

    # 🔥 AUTO-CREA TALKING PHOTO SE MANCA
    if not talking_photo_id:
        if not body.image_url:
            raise HTTPException(
                400,
                "Devi fornire image_url oppure talking_photo_id"
            )

        r = requests.post(
            f"{HEYGEN_BASE}/talking-photo",
            headers={
                # ⚠️ QUI SERVE BEARER
                "Authorization": f"Bearer {HEYGEN_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "image_url": body.image_url
            },
            timeout=30
        )

        if r.status_code != 200:
            raise HTTPException(500, f"Talking photo error: {r.text}")

        data = r.json()
        talking_photo_id = data["data"]["talking_photo_id"]

    # =========================
    # GENERATE VIDEO
    # =========================

    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "talking_photo",
                    "talking_photo_id": talking_photo_id
                },
                "voice": {
                    "type": "text",
                    "voice_id": body.voice_id,
                    "input_text": body.script
                }
            }
        ],
        "aspect_ratio": "9:16",
        "resolution": "720p"
    }

    r = requests.post(
        f"{HEYGEN_BASE}/video/generate",
        headers={
            # ⚠️ QUI SERVE X-Api-Key (HeyGen è incoerente)
            "X-Api-Key": HEYGEN_API_KEY,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60
    )

    if r.status_code != 200:
        raise HTTPException(500, f"HeyGen video error: {r.text}")

    return r.json()
