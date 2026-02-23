import os, subprocess, sys, runpod, uuid, glob, time, requests

# --- CONFIGURAZIONE SUPABASE ---
# Assicurati di aver messo queste ENV su RunPod
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_VIDEOS_BUCKET", "videos")

def upload_to_supabase(local_path, token):
    """Carica il video direttamente su Supabase senza passare da Catbox"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌ ENV Supabase mancanti su RunPod!")
        return None

    object_path = f"evs/{token}.mp4"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_path}"
    
    try:
        with open(local_path, "rb") as f:
            file_data = f.read()

        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "video/mp4",
            "x-upsert": "true"
        }

        print(f"🚀 Uploading to Supabase: {object_path}")
        r = requests.post(upload_url, headers=headers, data=file_data, timeout=300)
        
        if r.status_code in [200, 201]:
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{object_path}"
            return public_url
        else:
            print(f"❌ Errore Supabase: {r.text}")
            return None
    except Exception as e:
        print(f"❌ Eccezione upload: {e}")
        return None

def install_essentials():
    target_dir = "/tmp/custom_libs"
    if os.path.exists(target_dir): return # Evita di reinstallare se già presente
    os.makedirs(target_dir, exist_ok=True)
    libs = ["edge-tts", "gfpgan", "basicsr==1.4.2", "facexlib", "imageio-ffmpeg", "requests"]
    for lib in libs: 
        subprocess.run([sys.executable, "-m", "pip", "install", "-t", target_dir, lib], check=False)
    
    subprocess.run(f"find {target_dir} -name 'degradations.py' -exec sed -i 's/from torchvision.transforms.functional_tensor import/import torchvision.transforms.functional as/g' {{}} +", shell=True)

def handler(job):
    install_essentials()
    env = os.environ.copy()
    env["PYTHONPATH"] = f"/tmp/custom_libs:{os.getcwd()}"
    
    i = job['input']
    # Recuperiamo il 'token' inviato dal backend Render v3.6
    token = i.get('token') or str(uuid.uuid4())
    img, txt, aud, gen = i.get('image_url'), i.get('text'), i.get('audio_url'), i.get('gender', 'male')
    
    tmp_i, tmp_a, tmp_r = "/tmp/s.jpg", "/tmp/a.wav", "/tmp/o"
    try:
        os.makedirs(tmp_r, exist_ok=True)
        subprocess.run(["curl", "-L", "-o", tmp_i, img], check=True)
        
        if aud:
            subprocess.run(["curl", "-L", "-o", tmp_a, aud], check=True)
        else:
            v = "it-IT-GiuseppeNeural" if gen == "male" else "it-IT-ElsaNeural"
            subprocess.run(["edge-tts", "--text", txt, "--voice", v, "--write-media", tmp_a], check=True)
        
        # Rendering HD
        subprocess.run([sys.executable, "inference.py", "--source_image", tmp_i, "--driven_audio", tmp_a, "--result_dir", tmp_r, "--still", "--preprocess", "full", "--enhancer", "gfpgan", "--size", "512"], env=env, check=True)
        
        # Trova il file generato
        files = glob.glob(f"{tmp_r}/**/*.mp4", recursive=True)
        if not files: return {"error": "Nessun video generato"}
        
        path = max(files, key=os.path.getctime)
        
        # 🔥 UPLOAD DIRETTO A SUPABASE
        url = upload_to_supabase(path, token)
        
        return {"video_url": url} if url else {"error": "Upload Supabase fallito"}
    except Exception as e: 
        return {"error": str(e)}

runpod.serverless.start({"handler": handler})
