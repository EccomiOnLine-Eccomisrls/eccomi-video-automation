from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import requests
import os

router = APIRouter()

# =========================
# ENV
# =========================

HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
HEYGEN_BASE_V2 = "https://api.heygen.com/v2"

if not HEYGEN_API_KEY:
    print("⚠️ HEYGEN_API_KEY mancante")

# =========================
# EVS DEFAULTS (STABILI)
# =========================

EVS_AVATAR_ID = "Kristin_public_20240108"
EVS_VOICE_ID = "1753e5984bca4125a3e727d5d5e07ee2"

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


class EVSAvatarBody(BaseModel):
    text: str

# =========================
# CREATE TALKING PHOTO
# =========================

@router.post("/api/heygen/talking-photo")
def create_talking_photo(body: CreateTalkingPhotoBody):
    if not HEYGEN_API_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")

    r = requests.post(
        f"{HEYGEN_BASE_V2}/talking-photo",
        headers={
            # ⚠️ HeyGen qui vuole Bearer
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
# CREATE VIDEO (TALKING PHOTO FLOW)
# =========================

@router.post("/api/heygen/submit")
def heygen_submit(body: HeyGenSubmitBody):
    if not HEYGEN_API_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")

    talking_photo_id = body.talking_photo_id

    # AUTO-CREA TALKING PHOTO SE MANCA
    if not talking_photo_id:
        if not body.image_url:
            raise HTTPException(
                400,
                "Devi fornire image_url oppure talking_photo_id"
            )

        r = requests.post(
            f"{HEYGEN_BASE_V2}/talking-photo",
            headers={
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
        f"{HEYGEN_BASE_V2}/video/generate",
        headers={
            # ⚠️ Qui serve X-Api-Key
            "X-Api-Key": HEYGEN_API_KEY,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60
    )

    if r.status_code != 200:
        raise HTTPException(500, f"HeyGen video error: {r.text}")

    return r.json()

# =========================
# EVS — AVATAR FLOW (STABILE)
# =========================

@router.post("/api/evs/heygen/avatar")
def evs_generate_avatar(body: EVSAvatarBody):
    if not HEYGEN_API_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")

    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": EVS_AVATAR_ID,
                    "avatar_style": "normal"
                },
                "voice": {
                    "type": "text",
                    "voice_id": EVS_VOICE_ID,
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
        f"{HEYGEN_BASE_V2}/video/generate",
        headers={
            "X-Api-Key": HEYGEN_API_KEY,
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60
    )

    if r.status_code != 200:
        raise HTTPException(500, r.text)

    return r.json()

# =========================
# EVS — VIDEO STATUS
# =========================

@router.get("/api/evs/heygen/status/{video_id}")
def evs_video_status(video_id: str):
    if not HEYGEN_API_KEY:
        raise HTTPException(500, "HEYGEN_API_KEY mancante")

    r = requests.get(
        f"{HEYGEN_BASE_V2}/video/status",
        headers={
            "X-Api-Key": HEYGEN_API_KEY
        },
        params={"video_id": video_id},
        timeout=30
    )

    if r.status_code != 200:
        raise HTTPException(500, r.text)

    return r.json()
