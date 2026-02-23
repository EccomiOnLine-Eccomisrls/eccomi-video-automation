import os
import time
import json
import requests
import uuid
import re
from typing import Optional
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from supabase import create_client, Client

# =====================================================
# ENV & CONFIG
# =====================================================
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")

SUPABASE_INPUTS_BUCKET = "inputs"
SUPABASE_VIDEOS_BUCKET = "videos"

# =====================================================
# CLIENT
# =====================================================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def health(): 
    return {"status": "v3.6-FIX-STABLE", "supabase": "ok"}

# =====================================================
# CORE LOGIC
# =====================================================

def runpod_submit(order_id: str):
    # Prendi dati dal DB
    res = supabase.table("video_jobs").select("*").eq("evs_token", order_id).execute()
    if not res.data: return
    row = res.data[0]

    payload = {
        "input": {
            "image_url": row.get("photo_url"),
            "text": row.get("script_text"),
            "gender": row.get("gender"),
            "token": order_id
        }
    }
    if row.get("audio_url"): payload["input"]["audio_url"] = row.get("audio_url")

    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
    url = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run"
    
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=45)
        job_id = r.json().get("id")
        if job_id:
            supabase.table("video_jobs").update({"status": "processing", "runpod_job_id": job_id}).eq("evs_token", order_id).execute()
            print(f"✅ Job inviato a RunPod: {job_id}")
    except Exception as e:
        print(f"❌ Errore invio: {e}")

@app.post("/evs/retry/{evs_token}")
async def evs_retry(evs_token: str, bg: BackgroundTasks):
    bg.add_task(runpod_submit, evs_token)
    return {"ok": True, "status": "retrying"}

@app.get("/video/{token}", response_class=HTMLResponse)
def video_view(token: str):
    download_url = f"{PUBLIC_BASE_URL}/video/{token}/download"
    return HTMLResponse(content=f"<html><body style='background:#0b1b33;color:#fff;text-align:center;padding:50px;'><h1>🎬 Video Pronto</h1><br><a href='{download_url}' style='background:#fff;color:#0b1b33;padding:15px;text-decoration:none;font-weight:bold;border-radius:8px;'>⬇️ SCARICA MP4</a></body></html>")

@app.get("/video/{token}/download")
def video_download(token: str):
    # Link diretto al bucket pubblico di Supabase
    direct_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_VIDEOS_BUCKET}/evs/{token}.mp4"
    return RedirectResponse(direct_url)
