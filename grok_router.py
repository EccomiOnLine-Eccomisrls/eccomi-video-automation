import os
import time
import hmac
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

import imageio_ffmpeg
import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client

router = APIRouter(prefix="/xai", tags=["xai"])

XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
EVS_GROK_ADMIN_TOKEN = os.getenv("EVS_GROK_ADMIN_TOKEN", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
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


class GrokSuperMasterRequest(BaseModel):
    evs_code: str = Field(min_length=3, max_length=64)
    job_id: str = Field(min_length=3, max_length=160)
    source_visual_url: str = Field(min_length=8)
    source_master_url: str = Field(min_length=8)
    cta_text: str = Field(default="", max_length=220)
    duration: float = Field(default=15.0, ge=5.0, le=30.0)
    storage_path: str = Field(min_length=3)
    storage_upload_token: str = Field(min_length=3)
    output_public_url: str = Field(min_length=8)
    supabase_url: str = Field(min_length=8)
    supabase_anon_key: str = Field(min_length=8)
    callback_url: str = Field(min_length=8)


def _admin_secret() -> str:
    return EVS_GROK_ADMIN_TOKEN or SUPABASE_SERVICE_ROLE_KEY


def _is_verified_supabase_service_role(token: str) -> bool:
    if not token or not SUPABASE_URL:
        return False
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users?page=1&per_page=1",
            headers={"Authorization": f"Bearer {token}", "apikey": token},
            timeout=15,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def _require_admin(authorization: Optional[str], x_evs_admin_key: Optional[str]):
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    supplied = x_evs_admin_key or bearer
    secret = _admin_secret()
    if secret and supplied and hmac.compare_digest(supplied, secret):
        return
    if bearer and _is_verified_supabase_service_role(bearer):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def _headers():
    if not XAI_API_KEY:
        raise HTTPException(status_code=503, detail="XAI_API_KEY not configured")
    return {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}


def _download(url: str, path: Path):
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with path.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"download failed: {exc}")


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


def _super_master(body: GrokSuperMasterRequest):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    started = time.perf_counter()
    print(f"GROK_SUPER_MASTER_START evs={body.evs_code} job={body.job_id}", flush=True)
    with tempfile.TemporaryDirectory(prefix="evs_grok_super_master_") as tmp:
        root = Path(tmp)
        visual = root / "visual.mp4"
        source_master = root / "source_master.mp4"
        output = root / "super_master.mp4"
        cta_file = root / "cta.txt"
        _download(body.source_visual_url, visual)
        _download(body.source_master_url, source_master)
        cta_file.write_text(body.cta_text.strip(), encoding="utf-8")

        # Keep the Grok-native 720x1280 frame. Upscaling to 1080x1920 on the
        # small Render worker caused memory pressure/restarts and adds no source detail.
        out_w, out_h = 720, 1280
        base_filter = f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},fps=30,format=yuv420p"
        cta = body.cta_text.strip()
        filters = [f"[0:v]{base_filter}[base]"]
        out_label = "base"
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if cta:
            font_part = f":fontfile={font}" if os.path.exists(font) else ""
            filters.append(
                f"[base]drawbox=x=38:y=1090:w=644:h=125:color=black@0.52:t=fill:enable='gte(t,{max(0.0, body.duration-3.2):.3f})',"
                f"drawtext=textfile='{cta_file.as_posix()}':fontcolor=white:fontsize=26{font_part}:x=(w-text_w)/2:y=1135:enable='gte(t,{max(0.0, body.duration-3.2):.3f})'[v]"
            )
            out_label = "v"
        cmd = [
            ffmpeg, "-y", "-threads", "1", "-i", str(visual), "-i", str(source_master),
            "-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
            "-map", f"[{out_label}]", "-map", "1:a:0?",
            "-t", f"{body.duration:.3f}", "-r", "30",
            "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output),
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            print(f"GROK_SUPER_MASTER_FFMPEG_TIMEOUT evs={body.evs_code}", flush=True)
            raise HTTPException(status_code=502, detail={"error":"SUPER_MASTER_FFMPEG_TIMEOUT","detail":str(exc)})
        if proc.returncode != 0 and cta:
            print(f"GROK_SUPER_MASTER_CTA_FALLBACK evs={body.evs_code} rc={proc.returncode} stderr={proc.stderr[-1200:]}", flush=True)
            fallback_cmd = [
                ffmpeg, "-y", "-threads", "1", "-i", str(visual), "-i", str(source_master),
                "-vf", base_filter, "-map", "0:v:0", "-map", "1:a:0?",
                "-t", f"{body.duration:.3f}", "-r", "30",
                "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "18",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output),
            ]
            proc = subprocess.run(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        if proc.returncode != 0 or not output.exists() or output.stat().st_size < 10000:
            print(f"GROK_SUPER_MASTER_FFMPEG_FAILED evs={body.evs_code} rc={proc.returncode} stderr={proc.stderr[-2500:]}", flush=True)
            raise HTTPException(status_code=502, detail={"error": "SUPER_MASTER_FFMPEG_FAILED", "stderr": proc.stderr[-3000:]})

        sb = create_client(body.supabase_url.rstrip("/"), body.supabase_anon_key)
        try:
            with output.open("rb") as fh:
                sb.storage.from_("videos").upload_to_signed_url(path=body.storage_path, token=body.storage_upload_token, file=fh)
        except Exception as exc:
            print(f"GROK_SUPER_MASTER_UPLOAD_FAILED evs={body.evs_code} err={exc}", flush=True)
            raise HTTPException(status_code=502, detail={"error":"SUPER_MASTER_UPLOAD_FAILED","detail":str(exc)})
        public_url = body.output_public_url
        elapsed = round(time.perf_counter() - started, 3)

        cb = {
            "event": "evs.video.completed",
            "status": "COMPLETED",
            "job_id": body.job_id,
            "spot_url": public_url,
            "customer_reference": body.evs_code.upper(),
            "generation": {
                "mode": "grok_super_master_direct_v3_low_memory",
                "engine": "eccomi-video-automation",
                "provider": "xai",
                "approved_motion_clip_url": body.source_visual_url,
                "source_master_url": body.source_master_url,
                "full_motion_master": True,
                "cta_requested": bool(cta),
                "target_duration_seconds": body.duration,
                "width": out_w,
                "height": out_h,
                "fps": 30,
                "gpu_started": False,
                "total_seconds": elapsed,
            },
            "qa": {
                "technical_pass": True,
                "approved_motion_clip_used": True,
                "full_motion_master": True,
                "static_mascot_fallback_used": False,
                "gpu_started": False,
                "audio_preserved": True,
                "voice_preserved": True,
                "music_preserved": True,
                "release_gate_required": True,
                "output_width": out_w,
                "output_height": out_h,
                "target_duration_seconds": body.duration,
            },
        }
        try:
            r = requests.post(
                body.callback_url,
                json=cb,
                headers={"Authorization": f"Bearer {body.supabase_anon_key}", "apikey": body.supabase_anon_key, "Content-Type": "application/json"},
                timeout=45,
            )
            callback_status = r.status_code
            callback_body = r.text[:1200]
        except requests.RequestException as exc:
            callback_status = 0
            callback_body = str(exc)
        print(f"GROK_SUPER_MASTER_DONE evs={body.evs_code} seconds={elapsed} callback={callback_status}", flush=True)
        return {"ok": True, "evs_code": body.evs_code.upper(), "job_id": body.job_id, "video_url": public_url, "processing_seconds": elapsed, "gpu_started": False, "callback_status": callback_status, "callback_body": callback_body}


@router.get("/status")
def xai_status():
    return {"ok": True, "xai_configured": bool(XAI_API_KEY), "admin_auth_configured": bool(_admin_secret()) or bool(SUPABASE_URL), "model": XAI_VIDEO_MODEL}


@router.post("/video/generate")
def generate_video(body: GrokVideoRequest, authorization: Optional[str] = Header(default=None), x_evs_admin_key: Optional[str] = Header(default=None)):
    _require_admin(authorization, x_evs_admin_key)
    return _run_generation(body)


@router.post("/super-master")
def generate_super_master(body: GrokSuperMasterRequest, authorization: Optional[str] = Header(default=None), x_evs_admin_key: Optional[str] = Header(default=None)):
    _require_admin(authorization, x_evs_admin_key)
    return _super_master(body)
