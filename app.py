#!/usr/bin/env python3
"""
ALL VIDEO DOWNLOAD  —  Termux-friendly Video Downloader
YouTube · Facebook · TikTok / TikTok Lite
"""

import os
import re
import time
import uuid
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, request, render_template_string,
    jsonify, send_file, abort
)
import yt_dlp

# ---------------- Config ----------------
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

PORT = int(os.environ.get("PORT", 8080))
MAX_FILE_AGE = 3600  # 1 hour auto-delete

ALLOWED_DOMAINS = [
    "youtube.com", "youtu.be", "m.youtube.com",
    "facebook.com", "fb.watch", "m.facebook.com",
    "tiktok.com", "vt.tiktok.com", "vm.tiktok.com",
]

QUALITY_FORMATS = {
    "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
    "720":  "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
    "480":  "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best",
    "360":  "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best",
    "audio": "bestaudio/best",
}

app = Flask(__name__)
jobs = {}
jobs_lock = threading.Lock()


# ---------------- Helpers ----------------
def is_allowed(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


def cleanup_old_files():
    now = time.time()
    for f in DOWNLOAD_DIR.glob("*"):
        try:
            if now - f.stat().st_mtime > MAX_FILE_AGE:
                f.unlink()
        except Exception:
            pass


def cleanup_stale_jobs():
    now = time.time()
    with jobs_lock:
        for jid, j in list(jobs.items()):
            if j.get("_ts") and now - j["_ts"] > MAX_FILE_AGE:
                jobs.pop(jid, None)


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ()\u0980-\u09FF]+", "_", name).strip()
    return name[:120] or "video"


def run_download(job_id: str, url: str, quality: str):
    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["720"])
    outtmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")

    def progress_hook(d):
        with jobs_lock:
            if job_id not in jobs:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                if total > 0:
                    jobs[job_id]["progress"] = round(done * 100 / total, 1)
                    jobs[job_id]["size_mb"] = round(total / (1024 * 1024), 1)
                else:
                    jobs[job_id]["progress"] = -1
            elif d.get("status") == "finished":
                jobs[job_id]["progress"] = 99.0

    ydl_opts = {
        "format": fmt,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [progress_hook],
        "nocheckcertificate": True,
    }
    if quality == "audio":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # find produced file
        produced = None
        for ext in (".mp4", ".mkv", ".webm", ".mp3", ".m4a"):
            p = DOWNLOAD_DIR / f"{job_id}{ext}"
            if p.exists():
                produced = p
                break

        if not produced:
            raise RuntimeError("ফাইল তৈরি হয়নি")

        with jobs_lock:
            jobs[job_id].update({
                "status": "done",
                "progress": 100.0,
                "filename": produced.name,
                "title": info.get("title") or "video",
                "size_mb": round(produced.stat().st_size / (1024 * 1024), 1),
                "ext": produced.suffix.lower(),
                "_ts": time.time(),
            })

    except Exception as e:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update({
                    "status": "error",
                    "error": str(e)[:300],
                    "_ts": time.time(),
                })
        for f in DOWNLOAD_DIR.glob(f"{job_id}.*"):
            try: f.unlink()
            except Exception: pass


# ---------------- Routes ----------------
@app.route("/")
def index():
    cleanup_old_files()
    return render_template_string(HTML_PAGE)


@app.route("/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or request.form
    url = (data.get("url") or "").strip()
    quality = (data.get("quality") or "720").strip()

    if not url:
        return jsonify({"error": "লিংক দিন"}), 400
    if not is_allowed(url):
        return jsonify({"error": "এই সাইটটি সমর্থিত নয়"}), 400
    if quality not in QUALITY_FORMATS:
        quality = "720"

    cleanup_old_files()
    cleanup_stale_jobs()

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "filename": None,
            "error": None,
            "url": url,
            "quality": quality,
            "_ts": time.time(),
        }

    threading.Thread(target=run_download, args=(job_id, url, quality), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def job_status(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
    if not j:
        return jsonify({"error": "job not found"}), 404
    return jsonify({
        "status": j["status"],
        "progress": j["progress"],
        "error": j["error"],
        "title": j.get("title"),
        "size_mb": j.get("size_mb"),
        "filename": j["filename"],
        "quality": j.get("quality"),
    })


@app.route("/file/<job_id>")
def job_file(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
    if not j or j["status"] != "done":
        abort(404)
    path = DOWNLOAD_DIR / j["filename"]
    if not path.exists():
        abort(404)

    suffix = path.suffix.lower()
    mime = "audio/mpeg" if suffix == ".mp3" else "video/mp4"
    nice = safe_filename(j.get("title") or "video") + suffix
    return send_file(path, as_attachment=True, download_name=nice, mimetype=mime)


# ---------------- UI ----------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>ALL VIDEO DOWNLOAD</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{height:100%}
  body{
    font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
    background:linear-gradient(-45deg,#0f0c29,#302b63,#24243e,#5b0e8c);
    background-size:400% 400%;
    animation:bgShift 18s ease infinite;
    min-height:100vh;
    overflow-x:hidden;
    color:#fff;
    position:relative;
  }
  @keyframes bgShift{
    0%{background-position:0% 50%}
    50%{background-position:100% 50%}
    100%{background-position:0% 50%}
  }

  /* --------- Floating color orbs --------- */
  .orb{
    position:fixed;border-radius:50%;
    filter:blur(70px);opacity:.55;
    pointer-events:none;z-index:0;
    animation:floatOrb 22s ease-in-out infinite;
  }
  .orb.o1{width:340px;height:340px;background:#ff006e;top:-120px;left:-120px;animation-delay:0s}
  .orb.o2{width:280px;height:280px;background:#3a86ff;top:40%;right:-120px;animation-delay:-6s}
  .orb.o3{width:230px;height:230px;background:#8338ec;bottom:-80px;left:25%;animation-delay:-12s}
  .orb.o4{width:200px;height:200px;background:#06ffa5;top:25%;left:-60px;animation-delay:-18s}
  .orb.o5{width:170px;height:170px;background:#ffbe0b;bottom:25%;right:-40px;animation-delay:-9s}
  @keyframes floatOrb{
    0%,100%{transform:translate(0,0) scale(1)}
    25%{transform:translate(60px,-40px) scale(1.15)}
    50%{transform:translate(-40px,60px) scale(.9)}
    75%{transform:translate(40px,30px) scale(1.05)}
  }

  /* --------- Container --------- */
  .container{
    position:relative;z-index:2;
    max-width:600px;margin:0 auto;
    padding:36px 16px 60px;
    animation:slideUp .8s ease;
  }
  @keyframes slideUp{
    from{opacity:0;transform:translateY(30px)}
    to{opacity:1;transform:translateY(0)}
  }

  /* --------- Header --------- */
  header{text-align:center;margin-bottom:26px}
  .logo{
    display:inline-block;font-size:56px;
    animation:pulse 2s ease-in-out infinite;
    filter:drop-shadow(0 0 20px rgba(255,0,110,.6));
  }
  @keyframes pulse{
    0%,100%{transform:scale(1)}
    50%{transform:scale(1.12)}
  }
  h1{
    font-size:clamp(26px,7vw,38px);
    font-weight:900;letter-spacing:1px;
    background:linear-gradient(90deg,#ff006e,#ffbe0b,#06ffa5,#3a86ff,#8338ec,#ff006e);
    background-size:300% 100%;
    -webkit-background-clip:text;background-clip:text;
    -webkit-text-fill-color:transparent;
    animation:shineText 6s linear infinite;
    margin:8px 0 6px;
  }
  @keyframes shineText{
    0%{background-position:0% 50%}
    100%{background-position:300% 50%}
  }
  .subtitle{color:#c7c7ff;font-size:15px;margin-bottom:4px}
  .tagline{color:#9a9ac0;font-size:13px;font-style:italic}

  /* --------- Card (glassmorphism) --------- */
  .card{
    background:rgba(255,255,255,.08);
    backdrop-filter:blur(22px);
    -webkit-backdrop-filter:blur(22px);
    border:1px solid rgba(255,255,255,.18);
    border-radius:24px;
    padding:22px;
    box-shadow:0 20px 60px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.2);
    margin-bottom:18px;
  }

  input{
    width:100%;padding:15px 18px;
    border:2px solid rgba(255,255,255,.15);
    border-radius:14px;
    background:rgba(255,255,255,.06);
    color:#fff;font-size:16px;
    outline:none;transition:all .3s ease;
  }
  input::placeholder{color:rgba(255,255,255,.5)}
  input:focus{
    border-color:#3a86ff;
    background:rgba(255,255,255,.1);
    box-shadow:0 0 0 4px rgba(58,134,255,.25),0 0 24px rgba(58,134,255,.4);
  }

  .q-label{
    margin:18px 0 10px;
    font-size:14px;font-weight:600;
    color:#d8d8ff;letter-spacing:.3px;
  }
  .quality-grid{
    display:grid;
    grid-template-columns:repeat(5,1fr);
    gap:8px;margin-bottom:18px;
  }
  .q-btn{
    padding:11px 4px;
    border:2px solid rgba(255,255,255,.2);
    background:rgba(255,255,255,.05);
    color:#fff;border-radius:12px;
    font-size:13px;font-weight:700;
    cursor:pointer;
    transition:all .25s cubic-bezier(.4,0,.2,1);
    font-family:inherit;
  }
  .q-btn:hover{
    transform:translateY(-2px);
    border-color:rgba(255,255,255,.4);
    background:rgba(255,255,255,.12);
  }
  .q-btn.active{
    background:linear-gradient(135deg,#667eea,#764ba2);
    border-color:#a78bfa;
    box-shadow:0 0 20px rgba(167,139,250,.7),0 4px 14px rgba(102,126,234,.5);
    transform:translateY(-2px) scale(1.05);
  }

  /* --------- Download button --------- */
  .dl-btn{
    width:100%;padding:16px;
    border:0;border-radius:14px;
    font-family:inherit;
    font-size:17px;font-weight:800;
    color:#fff;cursor:pointer;
    background:linear-gradient(135deg,#ff006e,#8338ec,#3a86ff);
    background-size:200% 200%;
    animation:btnGradient 4s ease infinite;
    box-shadow:0 8px 24px rgba(255,0,110,.4),0 4px 12px rgba(58,134,255,.3);
    transition:transform .2s ease,box-shadow .3s ease;
    position:relative;overflow:hidden;
  }
  @keyframes btnGradient{
    0%,100%{background-position:0% 50%}
    50%{background-position:100% 50%}
  }
  .dl-btn::before{
    content:'';position:absolute;inset:0;
    background:linear-gradient(90deg,transparent,rgba(255,255,255,.4),transparent);
    transform:translateX(-100%);
    animation:shine 3s ease-in-out infinite;
  }
  @keyframes shine{
    0%{transform:translateX(-100%)}
    60%,100%{transform:translateX(100%)}
  }
  .dl-btn:hover:not(:disabled){
    transform:translateY(-3px);
    box-shadow:0 14px 32px rgba(255,0,110,.55),0 6px 18px rgba(58,134,255,.4);
  }
  .dl-btn:active:not(:disabled){transform:translateY(0) scale(.98)}
  .dl-btn:disabled{opacity:.65;cursor:not-allowed;animation:none}

  /* --------- Status panel --------- */
  .status-card{
    background:rgba(255,255,255,.08);
    backdrop-filter:blur(22px);
    -webkit-backdrop-filter:blur(22px);
    border:1px solid rgba(255,255,255,.18);
    border-radius:20px;padding:18px;
    box-shadow:0 12px 40px rgba(0,0,0,.3);
    animation:slideUp .5s ease;
  }
  .msg{font-size:15px;font-weight:600;margin-bottom:12px;text-align:center}
  .msg.err{color:#ff6b9d}
  .msg.ok{color:#06ffa5}

  .progress-bar{
    height:12px;border-radius:8px;
    background:rgba(255,255,255,.1);
    overflow:hidden;position:relative;
  }
  #fill{
    height:100%;width:0;
    background:linear-gradient(90deg,#06ffa5,#3a86ff,#8338ec,#ff006e);
    background-size:300% 100%;
    animation:btnGradient 3s linear infinite;
    border-radius:8px;
    transition:width .4s ease;
    box-shadow:0 0 12px rgba(58,134,255,.7);
  }
  .meta{
    margin-top:10px;text-align:center;
    font-size:13px;color:#b8b8e0;min-height:18px;
  }

  .dl-link{
    display:block;text-align:center;
    margin-top:16px;padding:14px;
    background:linear-gradient(135deg,#06ffa5,#18a558);
    color:#002d15;font-weight:900;
    border-radius:14px;text-decoration:none;
    box-shadow:0 8px 24px rgba(6,255,165,.4);
    transition:transform .2s ease,box-shadow .3s ease;
    animation:pop .5s cubic-bezier(.34,1.56,.64,1);
  }
  .dl-link:hover{
    transform:translateY(-3px) scale(1.02);
    box-shadow:0 12px 32px rgba(6,255,165,.6);
  }
  @keyframes pop{
    0%{opacity:0;transform:scale(.7)}
    100%{opacity:1;transform:scale(1)}
  }

  footer{
    text-align:center;margin-top:30px;
    font-size:12px;color:rgba(255,255,255,.4);
  }

  /* Small screens */
  @media(max-width:420px){
    .quality-grid{grid-template-columns:repeat(3,1fr)}
    .card{padding:16px}
    .logo{font-size:46px}
  }
</style>
</head>
<body>
  <div class="orb o1"></div>
  <div class="orb o2"></div>
  <div class="orb o3"></div>
  <div class="orb o4"></div>
  <div class="orb o5"></div>

  <div class="container">
    <header>
      <div class="logo">⬇️</div>
      <h1>ALL VIDEO DOWNLOAD</h1>
      <p class="subtitle">▶️ YouTube · 📘 Facebook · 🎵 TikTok · TikTok Lite</p>
      <p class="tagline">✨ ওয়াটারমার্ক ছাড়া · যেকোনো কোয়ালিটি ✨</p>
    </header>

    <div class="card">
      <input id="url" type="url" placeholder="🔗 ভিডিও লিংক পেস্ট করুন" autocomplete="off">
      <div class="q-label">🎯 কোয়ালিটি বেছে নিন</div>
      <div class="quality-grid" id="qgrid">
        <button class="q-btn" data-q="1080">1080p</button>
        <button class="q-btn active" data-q="720">720p</button>
        <button class="q-btn" data-q="480">480p</button>
        <button class="q-btn" data-q="360">360p</button>
        <button class="q-btn" data-q="audio">🎵 MP3</button>
      </div>
      <button id="btn" class="dl-btn"><span>⬇️ ডাউনলোড শুরু করুন</span></button>
    </div>

    <div class="status-card" id="panel" style="display:none">
      <div id="msg" class="msg">প্রস্তুত হচ্ছে…</div>
      <div class="progress-bar"><div id="fill"></div></div>
      <div class="meta" id="meta"></div>
      <div id="link"></div>
    </div>

    <footer>Made with ❤️ · Termux Friendly</footer>
  </div>

<script>
/* ------------- Sound engine (Web Audio API) ------------- */
let audioCtx = null;
function getCtx(){
  if(!audioCtx){
    try{ audioCtx = new (window.AudioContext||window.webkitAudioContext)(); }catch(e){}
  }
  return audioCtx;
}
function tone(freq,dur,type='sine',vol=0.08){
  const c = getCtx(); if(!c) return;
  const o = c.createOscillator();
  const g = c.createGain();
  o.type = type; o.frequency.value = freq;
  o.connect(g); g.connect(c.destination);
  g.gain.setValueAtTime(vol,c.currentTime);
  g.gain.exponentialRampToValueAtTime(0.001,c.currentTime+dur);
  o.start(); o.stop(c.currentTime+dur);
}
const sfx = {
  click(){ tone(660,0.08,'square',0.05); },
  start(){ tone(440,0.12); setTimeout(()=>tone(660,0.12),100); setTimeout(()=>tone(880,0.18),220); },
  success(){ tone(660,0.1); setTimeout(()=>tone(880,0.12),110); setTimeout(()=>tone(1180,0.25),230); },
  error(){ tone(220,0.25,'sawtooth',0.09); setTimeout(()=>tone(160,0.35,'sawtooth',0.09),180); },
  tick(){ tone(880,0.04,'triangle',0.03); }
};

/* ------------- DOM helpers ------------- */
const $ = id => document.getElementById(id);
const qgrid = $('qgrid');
let selectedQuality = '720';
let timer = null;
let lastTick = 0;

qgrid.addEventListener('click', e=>{
  const b = e.target.closest('.q-btn');
  if(!b) return;
  qgrid.querySelectorAll('.q-btn').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  selectedQuality = b.dataset.q;
  sfx.click();
});

/* ------------- Download flow ------------- */
async function go(){
  const url = $('url').value.trim();
  if(!url){ sfx.error(); return; }

  $('btn').disabled = true;
  $('panel').style.display = 'block';
  $('link').innerHTML = '';
  $('msg').className = 'msg';
  $('msg').textContent = '🔗 সংযোগ করা হচ্ছে…';
  $('fill').style.width = '0%';
  $('meta').textContent = '';
  sfx.start();

  const fd = new FormData();
  fd.append('url', url);
  fd.append('quality', selectedQuality);

  let res;
  try{
    res = await fetch('/download', { method:'POST', body: fd });
  }catch(e){
    return fail('নেটওয়ার্ক সমস্যা — ইন্টারনেট চেক করুন');
  }

  if(!res.ok){
    const j = await res.json().catch(()=>({error:'ব্যর্থ'}));
    return fail(j.error || 'শুরু করা যায়নি');
  }

  const { job_id } = await res.json();
  clearInterval(timer);
  timer = setInterval(()=>poll(job_id), 1200);
  poll(job_id);
}

async function poll(id){
  let res;
  try{ res = await fetch('/status/' + id); }catch(e){ return; }
  if(!res.ok) return;
  const j = await res.json();

  $('msg').className = 'msg';
  $('msg').textContent =
    j.status === 'queued' ? '⏳ লাইনে আছে…' :
    j.status === 'done'   ? '✅ ডাউনলোড সম্পন্ন!' :
    j.status === 'error'  ? '❌ ব্যর্থ হয়েছে' :
    '⬇️ ডাউনলোড হচ্ছে…';

  if(j.progress >= 0){
    const p = Math.min(j.progress, 100);
    $('fill').style.width = p + '%';
    // play a soft tick at 25/50/75 milestones
    if(p >= 50 && lastTick < 50){ sfx.tick(); lastTick = 50; }
    else if(p >= 75 && lastTick < 75){ sfx.tick(); lastTick = 75; }
  }

  const q = j.quality === 'audio' ? 'MP3' : (j.quality || '') + 'p';
  $('meta').textContent =
    (j.size_mb ? j.size_mb + ' MB · ' : '') +
    q +
    (j.progress >= 0 ? ' · ' + Math.round(j.progress) + '%' : '');

  if(j.status === 'done'){
    clearInterval(timer);
    $('btn').disabled = false;
    lastTick = 0;
    sfx.success();
    $('msg').className = 'msg ok';
    $('msg').textContent = '✅ ' + (j.title ? j.title.slice(0,60) : 'ডাউনলোড সম্পন্ন');
    $('link').innerHTML =
      '<a class="dl-link" href="/file/' + id + '">⬇️ ফাইল ডাউনলোড করুন</a>';
    $('fill').style.width = '100%';
  } else if(j.status === 'error'){
    clearInterval(timer);
    fail(j.error || 'ডাউনলোড ব্যর্থ');
  }
}

function fail(m){
  clearInterval(timer);
  $('btn').disabled = false;
  lastTick = 0;
  sfx.error();
  $('msg').className = 'msg err';
  $('msg').textContent = '❌ ' + m;
}

/* ------------- Bind ------------- */
$('btn').addEventListener('click', go);
$('url').addEventListener('keydown', e=>{
  if(e.key === 'Enter'){ e.preventDefault(); go(); }
});
</script>
</body>
</html>
"""


# ---------------- Entry ----------------
if __name__ == "__main__":
    print(f"[*] Serving on http://0.0.0.0:{PORT}")
    print(f"[*] Downloads saved to: {DOWNLOAD_DIR}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)