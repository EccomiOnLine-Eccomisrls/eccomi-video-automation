import os
import time
import hmac
from typing import Optional

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/xai", tags=["xai"])

XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
EVS_GROK_ADMIN_TOKEN = os.getenv("EVS_GROK_ADMIN_TOKEN", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
XAI_VIDEO_MODEL = os.getenv("XAI_VIDEO_MODEL", "grok-imagine-video-1.5").strip()
XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1").rstrip("/")


class GrokVideoRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    image_url: Optional[str] = None
    reference_image_urls: Optional[list[str]] = None
    duration: int = Field(default=5, ge=1, le=15)
    aspect_ratio: str = "9:16"
    resolution: str = "720p"
    generate_audio: bool = False
    poll: bool = True
    poll_timeout_seconds: int = Field(default=240, ge=10, le=600)


def _admin_secret() -> str:
    return EVS_GROK_ADMIN_TOKEN or SUPABASE_SERVICE_ROLE_KEY


def _require_admin(authorization: Optional[str], x_evs_admin_key: Optional[str]):
    secret = _admin_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="EVS admin secret not configured")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    supplied = x_evs_admin_key or bearer
    if not supplied or not hmac.compare_digest(supplied, secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _headers():
    if not XAI_API_KEY:
        raise HTTPException(status_code=503, detail="XAI_API_KEY not configured")
    return {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}


def _run_generation(body: GrokVideoRequest):
    payload = {
        "model": XAI_VIDEO_MODEL,
        "prompt": body.prompt,
        "duration": body.duration,
        "aspect_ratio": body.aspect_ratio,
        "resolution": body.resolution,
        "generate_audio": body.generate_audio,
        "storage_options": {"filename": f"evs-grok-{int(time.time())}.mp4", "public_url": True},
    }
    if body.reference_image_urls:
        payload["reference_images"] = [{"url": url} for url in body.reference_image_urls if url]
    elif body.image_url:
        payload["image"] = {"url": body.image_url}

    try:
        response = requests.post(f"{XAI_BASE_URL}/videos/generations", headers=_headers(), json=payload, timeout=60)
    except requests.RequestException as exc:
        print(f"GROK_VIDEO_START_NETWORK_ERROR {exc}", flush=True)
        raise HTTPException(status_code=502, detail=f"xAI request failed: {exc}")
    if response.status_code >= 400:
        print(f"GROK_VIDEO_START_PROVIDER_ERROR status={response.status_code} body={response.text[:1500]}", flush=True)
        raise HTTPException(status_code=502, detail={"provider_status": response.status_code, "provider_body": response.text[:2000]})

    started = response.json()
    request_id = started.get("request_id")
    if not request_id:
        raise HTTPException(status_code=502, detail={"error": "xAI request_id missing", "provider": started})
    print(f"GROK_VIDEO_STARTED request_id={request_id} model={XAI_VIDEO_MODEL} duration={body.duration}", flush=True)
    if not body.poll:
        return {"ok": True, "provider": "xai", "request_id": request_id, "status": "submitted"}

    deadline = time.time() + body.poll_timeout_seconds
    while time.time() < deadline:
        try:
            polled = requests.get(f"{XAI_BASE_URL}/videos/{request_id}", headers={"Authorization": f"Bearer {XAI_API_KEY}"}, timeout=30)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"xAI polling failed: {exc}")
        if polled.status_code >= 400:
            raise HTTPException(status_code=502, detail={"provider_status": polled.status_code, "provider_body": polled.text[:2000]})
        result = polled.json()
        status = str(result.get("status", "")).lower()
        if status == "done":
            video = result.get("video") or {}
            usage = result.get("usage") or {}
            file_output = video.get("file_output") or result.get("file_output") or {}
            video_url = video.get("url")
            public_url = file_output.get("public_url") or result.get("public_url")
            print(f"GROK_VIDEO_DONE request_id={request_id} video_url={video_url} public_url={public_url} duration={video.get('duration')} usage={usage}", flush=True)
            return {"ok": True, "provider": "xai", "model": result.get("model") or XAI_VIDEO_MODEL, "request_id": request_id, "status": "done", "video_url": video_url, "public_url": public_url, "duration": video.get("duration"), "usage": usage, "raw": result}
        if status in {"failed", "expired", "cancelled", "canceled"}:
            raise HTTPException(status_code=502, detail={"error": f"xAI generation {status}", "provider": result})
        time.sleep(5)

    return {"ok": True, "provider": "xai", "request_id": request_id, "status": "processing"}


@router.get("/status")
def xai_status():
    return {"ok": True, "xai_configured": bool(XAI_API_KEY), "admin_auth_configured": bool(_admin_secret()), "model": XAI_VIDEO_MODEL}


@router.post("/video/generate")
def generate_video(body: GrokVideoRequest, authorization: Optional[str] = Header(default=None), x_evs_admin_key: Optional[str] = Header(default=None)):
    _require_admin(authorization, x_evs_admin_key)
    return _run_generation(body)
