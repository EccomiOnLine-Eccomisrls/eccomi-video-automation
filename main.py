import os, hmac, hashlib, base64, time, json, requests, uuid
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
import mimetypes

from supabase import create_client, Client

# =========================
# ENV & GLOBALS
# =========================
# Database & GPU
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY", "")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "")

# Shopify
SHOP_DOMAIN = os.getenv("SHOP_DOMAIN", "")
SHOP_ADMIN_TOKEN = os.getenv("SHOP_ADMIN_TOKEN", "")
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
VERIFY_SHOPIFY_HMAC = os.getenv("VERIFY_SHOPIFY_HMAC", "false").lower() == "true"

# Storage Locale (Cache ordini)
EVS_STORAGE_DIR = os.getenv("EVS_STORAGE_DIR", "data/evs_orders")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # es: https://eccomi-video-automation.onrender.com

EVS_STORAGE = Path(EVS_STORAGE_DIR)
EVS_STORAGE.mkdir(parents=True, exist_ok=True)

# Inizializza Supabase (opzionale)
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("✅ Supabase collegato")
    except Exception as e:
        supabase = None
        print("⚠️ Supabase init error:", e)

# =========================
# UTILS
# =========================
def _now_iso():
    return datetime.utcnow().isoformat() + "Z"

def _safe_json_load(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def evs_update_meta(order_id: str, changes: Dict[str, Any]):
    order_dir = EVS_STORAGE / order_id
    if not order_dir.exists():
        return
    meta_path = order_dir / "meta.json"
    meta = _safe_json_load(meta_path)
    meta.update(changes)
    meta["order_id"] = order_id
    meta["updated_at"] = _now_iso()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

def _verify_shopify_hmac(request: Request, raw_body: bytes):
    if not VERIFY_SHOPIFY_HMAC:
        return
    if not SHOPIFY_WEBHOOK_SECRET:
        raise HTTPException(500, "SHOPIFY_WEBHOOK_SECRET mancante")
    received = request.headers.get("X-Shopify-Hmac-Sha256") or ""
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    if not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Webhook HMAC non valido")

def _require_env(name: str, value: str):
    if not value:
        raise HTTPException(500, f"ENV mancante: {name}")

# =========================
# CORE LOGIC: RUNPOD
# =========================
def evs_launch_runpod(order_id: str, order_name: str, email: str):
    """
    Allineato al tuo handler RunPod:
      input.image_url  (obbligatorio)
      input.text       (opzionale -> TTS se audio non c'è)
      input.gender     (male/female)
      input.audio_url  (opzionale)
    """
    _require_env("PUBLIC_BASE_URL", PUBLIC_BASE_URL)
    _require_env("RUNPOD_API_KEY", RUNPOD_API_KEY)
    _require_env("RUNPOD_ENDPOINT_ID", RUNPOD_ENDPOINT_ID)

    order_dir = EVS_STORAGE / order_id
    if not order_dir.exists():
        return

    meta_path = order_dir / "meta.json"
    meta = _safe_json_load(meta_path)

    # URL pubblici che RunPod deve scaricare
    photo_url = f"{PUBLIC_BASE_URL.rstrip('/')}/evs/file/{order_id}/photo"
    audio_exists = bool(meta.get("has_audio"))
    audio_url = f"{PUBLIC_BASE_URL.rstrip('/')}/evs/file/{order_id}/audio" if audio_exists else None

    # Testo + gender dal meta (default)
    script_text = (meta.get("script_text") or "").strip() or "Ciao! Il tuo video è pronto."
    gender = (meta.get("gender") or "male").strip().lower()
    if gender not in ("male", "female"):
        gender = "male"

    # 1) Registra su Supabase (opzionale)
    db_id = None
    if supabase:
        try:
            res = supabase.table("video_jobs").insert({
                "shopify_order_id": order_name,
                "customer_email": email,
                "status": "queued",
                "input_image_url": photo_url,
                "input_audio_url": audio_url,
                "gender": gender,
                "script_text": script_text,
                "evs_token": order_id,
            }).execute()
            if getattr(res, "data", None):
                db_id = res.data[0].get("id")
        except Exception as e:
            print("⚠️ Supabase insert error:", e)

    # 2) Chiama RunPod /run
    runpod_url = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run"
    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}

    runpod_input: Dict[str, Any] = {
        "image_url": photo_url,
        "text": script_text,
        "gender": gender,
    }
    if audio_url:
        runpod_input["audio_url"] = audio_url  # se presente, RunPod userà quello

    payload = {"input": runpod_input}

    try:
        r = requests.post(runpod_url, headers=headers, json=payload, timeout=45)
        rp_data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        job_id = rp_data.get("id")

        if not r.ok:
            evs_update_meta(order_id, {"status": "RUNPOD_SUBMIT_FAILED", "runpod_error": r.text})
            if supabase and db_id:
                supabase.table("video_jobs").update({"status": "submit_failed"}).eq("id", db_id).execute()
            print("❌ RunPod submit failed:", r.status_code, r.text)
            return

        if job_id:
            evs_update_meta(order_id, {"status": "PROCESSING_GPU", "runpod_id": job_id, "shopify_order": order_name})
            if supabase and db_id:
                supabase.table("video_jobs").update({"runpod_job_id": job_id, "status": "processing"}).eq("id", db_id).execute()
            print(f"✅ Job inviato a RunPod: {job_id}")
        else:
            evs_update_meta(order_id, {"status": "RUNPOD_NO_JOB_ID", "runpod_raw": rp_data})
            print("❌ RunPod risposta senza id:", rp_data)

    except Exception as e:
        evs_update_meta(order_id, {"status": "RUNPOD_CONN_ERROR", "runpod_error": str(e)})
        print("❌ Errore connessione RunPod:", e)

# =========================
# API ROUTES
# =========================
app = FastAPI(title="EVS Pure Automation", version="3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health():
    return {
        "status": "online",
        "engine": "RunPod-SadTalker",
        "supabase": bool(supabase),
        "public_base_url": bool(PUBLIC_BASE_URL),
    }

# Endpoint per ricevere i file dal modulo Shopify (prima del pagamento)
@app.post("/evs/order")
async def receive_order(
    email: str = Form(...),
    photo: UploadFile = File(...),
    audio: Optional[UploadFile] = File(None),
    script_text: str = Form(""),
    gender: str = Form("male"),
    order_id: Optional[str] = Form(None),
):
    token = order_id or str(uuid.uuid4())
    order_dir = EVS_STORAGE / token
    order_dir.mkdir(parents=True, exist_ok=True)

    # Salva foto (mantieni estensione)
    photo_ext = os.path.splitext(photo.filename or "")[1] or ".png"
    photo_path = order_dir / f"photo{photo_ext}"
    photo_path.write_bytes(await photo.read())

    audio_path = None
    has_audio = False
    if audio is not None:
        has_audio = True
        audio_ext = os.path.splitext(audio.filename or "")[1] or ".wav"
        audio_path = order_dir / f"audio{audio_ext}"
        audio_path.write_bytes(await audio.read())

    gender_norm = (gender or "male").strip().lower()
    if gender_norm not in ("male", "female"):
        gender_norm = "male"

    meta = {
        "email": email,
        "status": "WAITING_PAYMENT",
        "created_at": _now_iso(),
        "script_text": script_text or "",
        "gender": gender_norm,
        "photo_path": str(photo_path),
        "audio_path": str(audio_path) if audio_path else None,
        "has_audio": has_audio,
    }
    (order_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return JSONResponse({"ok": True, "evs_token": token})

# Webhook Shopify (order paid)
@app.post("/shopify/webhook")
async def shopify_webhook(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    _verify_shopify_hmac(request, raw)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "Payload JSON non valido")

    if payload.get("financial_status") not in ("paid", "partially_paid"):
        return {"ok": True, "ignored": "not_paid"}

    order_name = payload.get("name") or "Order"
    email = payload.get("email") or (payload.get("customer") or {}).get("email") or ""

    # Estrazione Token EVS dalle line item properties
    tokens: List[str] = []
    for item in payload.get("line_items", []):
        for prop in (item.get("properties") or []):
            if (prop.get("name") or "").strip() == "EVS Token":
                val = (prop.get("value") or "").strip()
                if val:
                    tokens.append(val)

    if not tokens:
        return {"ok": True, "ignored": "no_evs_tokens"}

    for tok in tokens:
        evs_update_meta(tok, {"status": "PAID", "shopify_order": order_name})
        # lancia RunPod in background
        bg.add_task(evs_launch_runpod, tok, order_name, email)

    return {"ok": True, "processed": tokens}

# Serve i file a RunPod (pubblici via PUBLIC_BASE_URL)
@app.get("/evs/file/{order_id}/{kind}")
def serve_file(order_id: str, kind: str):
    order_dir = EVS_STORAGE / order_id
    meta_path = order_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(404, "Ordine EVS non trovato")

    meta = _safe_json_load(meta_path)

    if kind == "photo":
        path_str = meta.get("photo_path")
    elif kind == "audio":
        path_str = meta.get("audio_path")
    else:
        raise HTTPException(400, "kind deve essere 'photo' o 'audio'")

    if not path_str or not os.path.exists(path_str):
        raise HTTPException(404, "File non trovato")

    ctype, _ = mimetypes.guess_type(path_str)
    if not ctype:
        ctype = "application/octet-stream"

    return Response(content=Path(path_str).read_bytes(), media_type=ctype)
