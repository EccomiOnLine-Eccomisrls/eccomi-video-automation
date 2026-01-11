import logging
import traceback
import requests
import os
from fastapi import HTTPException

HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
HEYGEN_BASE = "https://api.heygen.com/v2"

@app.post("/api/evs/heygen/avatar")
async def create_avatar(payload: dict):
    try:
        logging.info("📩 Payload ricevuto")
        logging.info(payload)

        if not HEYGEN_API_KEY:
            raise Exception("HEYGEN_API_KEY non presente in ambiente")

        text = payload.get("text")
        if not text:
            raise Exception("Campo 'text' mancante nel payload")

        body = {
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": "alex",
                        "avatar_style": "normal"
                    },
                    "voice": {
                        "type": "text",
                        "voice_id": "1753e5984bca4125a3e727d5d5e07ee2",
                        "input_text": text
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
            json=body,
            timeout=60
        )

        logging.info(f"📡 HeyGen status: {r.status_code}")
        logging.info(r.text)

        if r.status_code != 200:
            raise Exception(f"HeyGen error: {r.text}")

        return r.json()

    except Exception as e:
        logging.error("❌ ERRORE CRITICO EVS")
        logging.error(str(e))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
