import logging
import traceback
import requests
import os
from fastapi import APIRouter, HTTPException
from typing import Optional

router = APIRouter()

HEYGEN_BASE = "https://api.heygen.com/v2"

@router.post("/api/evs/heygen/avatar")
async def create_avatar(payload: dict):
    try:
        logging.info("📩 Payload ricevuto nel router HeyGen")
        logging.info(payload)

        # 1. Recupero API KEY da Render
        heygen_api_key = os.getenv("HEYGEN_API_KEY")
        if not heygen_api_key:
            logging.error("❌ HEYGEN_API_KEY mancante nelle variabili d'ambiente")
            raise Exception("HEYGEN_API_KEY non presente in ambiente")

        # 2. Recupero testo dal payload del comando
        text = payload.get("text")
        if not text:
            raise Exception("Campo 'text' mancante nel payload")

        # 3. Gestione ID AVATAR (Dinamico)
        # Ordine di priorità: Payload comando > Variabile Render > Josh (Fallback)
        avatar_id = payload.get("avatar_id") or os.getenv("HEYGEN_AVATAR_ID", "josh_lite_20230714")
        
        # 4. Gestione ID VOCE (Dinamico)
        # Ordine di priorità: Payload comando > Variabile Render > Voce default (Fallback)
        voice_id = payload.get("voice_id") or os.getenv("HEYGEN_VOICE_ID", "1753e5984bca4125a3e727d5d5e07ee2")

        logging.info(f"👤 Configurazione: Avatar={avatar_id}, Voice={voice_id}")

        # Costruzione del corpo della richiesta per HeyGen V2
        body = {
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": avatar_id,
                        "avatar_style": "normal"
                    },
                    "voice": {
                        "type": "text",
                        "voice_id": voice_id,
                        "input_text": text
                    }
                }
            ],
            "aspect_ratio": "9:16",
            "resolution": "720p"
        }

        # Chiamata API a HeyGen
        r = requests.post(
            f"{HEYGEN_BASE}/video/generate",
            headers={
                "X-Api-Key": heygen_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            json=body,
            timeout=60
        )

        logging.info(f"📡 Risposta HeyGen - Status: {r.status_code}")
        
        if r.status_code != 200:
            error_detail = r.text
            logging.error(f"❌ Errore HeyGen API: {error_detail}")
            raise Exception(f"HeyGen error: {error_detail}")

        return r.json()

    except Exception as e:
        logging.error("❌ ERRORE CRITICO NELL'INVIO VIDEO")
        logging.error(str(e))
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/evs/heygen/list-avatars")
async def list_avatars():
    try:
        heygen_api_key = os.getenv("HEYGEN_API_KEY")
        if not heygen_api_key:
            return {"error": "API KEY mancante su Render!"}

        r = requests.get(
            f"{HEYGEN_BASE}/avatar/list",
            headers={"X-Api-Key": heygen_api_key},
            timeout=30
        )
        
        # Se HeyGen risponde con un errore (es. 401), leggiamolo come testo
        if r.status_code != 200:
            return {"status": r.status_code, "msg": r.text}
            
        return r.json()
    except Exception as e:
        return {"error": str(e)}
