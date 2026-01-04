from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import requests
import os

router = APIRouter()

HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
HEYGEN_URL = "https://api.heygen.com/v2/video/generate"


class HeyGenSubmitBody(BaseModel):
    script: str
    avatar_id: str | None = None
    talking_photo_id: str | None = None
    voice_id: str | None = None


@router.post("/api/heygen/submit")
def heygen_submit(body: HeyGenSubmitBody):
    if not HEYGEN_API_KEY:
        raise HTTPException(status_code=500, detail="HEYGEN_API_KEY mancante")

    if not body.avatar_id and not body.talking_photo_id:
        raise HTTPException(
            status_code=400,
            detail="Devi fornire avatar_id oppure talking_photo_id"
        )

    # 🔥 PAYLOAD UFFICIALE HEYGEN (NON SBAGLIARE QUESTO)
    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "talking_photo" if body.talking_photo_id else "avatar",
                    **(
                        {"talking_photo_id": body.talking_photo_id}
                        if body.talking_photo_id
                        else {"avatar_id": body.avatar_id}
                    )
                },
                "voice": {
                    "type": "text",
                    "voice_id": body.voice_id or "it_male_energetic",
                    "input_text": body.script
                }
            }
        ],
        "aspect_ratio": "9:16",
        "resolution": "720p"
    }

    headers = {
        "X-Api-Key": HEYGEN_API_KEY,
        "Content-Type": "application/json"
    }

    response = requests.post(
        HEYGEN_URL,
        json=payload,
        headers=headers,
        timeout=60
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"HeyGen error: {response.text}"
        )

    return response.json()
