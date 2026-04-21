import io
import json
import logging
import os
import sys
import threading
import time

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template_string, request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bloodcam")

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_API_TOKEN  = os.environ.get("R2_API_TOKEN")
R2_BUCKET     = os.environ.get("R2_BUCKET")
WEB_PORT      = int(os.environ.get("WEB_PORT", "8080"))

interval = 30  # seconds between uploads, adjustable via web UI

recording     = threading.Event()
camera_lock   = threading.Lock()
record_thread = None
cam           = None

# Crop region as normalized fractions {x, y, w, h} or None for no crop.
crop_region: dict | None = None
crop_lock = threading.Lock()


# ---------------------------------------------------------------------------
# R2 helpers
# ---------------------------------------------------------------------------
def _r2_url(key: str) -> str:
    return f"https://api.cloudflare.com/client/v4/accounts/{R2_ACCOUNT_ID}/r2/buckets/{R2_BUCKET}/objects/{key}"


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {R2_API_TOKEN}"}


def r2_put(key: str, data: bytes, content_type: str) -> None:
    resp = requests.put(
        _r2_url(key),
        headers={**_auth_headers(), "Content-Type": content_type},
        data=data,
        timeout=30,
    )
    resp.raise_for_status()


def r2_get(key: str) -> bytes | None:
    resp = requests.get(_r2_url(key), headers=_auth_headers(), timeout=15)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def r2_delete(key: str) -> None:
    resp = requests.delete(_r2_url(key), headers=_auth_headers(), timeout=15)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()


MAX_PHOTOS = 100


def update_manifest(filename: str) -> None:
    try:
        raw = r2_get("manifest.json")
        manifest: list[str] = json.loads(raw) if raw else []
    except Exception as e:
        log.error("Manifest GET failed: %s", e)
        return

    manifest.append(filename)

    evicted = []
    if len(manifest) > MAX_PHOTOS:
        evicted = manifest[:-MAX_PHOTOS]
        manifest = manifest[-MAX_PHOTOS:]

    try:
        r2_put("manifest.json", json.dumps(manifest).encode(), "application/json")
    except Exception as e:
        log.error("Manifest PUT failed: %s", e)
        return

    for key in evicted:
        try:
            r2_delete(key)
        except Exception as e:
            log.warning("Failed to delete %s: %s", key, e)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------
def capture_jpeg() -> bytes:
    buf = io.BytesIO()
    cam.capture_file(buf, format="jpeg")
    return buf.getvalue()


def apply_crop(jpeg: bytes, region: dict) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg))
    W, H = img.size
    left   = int(region["x"] * W)
    top    = int(region["y"] * H)
    right  = int((region["x"] + region["w"]) * W)
    bottom = int((region["y"] + region["h"]) * H)
    cropped = img.crop((left, top, right, bottom))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def upload(jpeg: bytes) -> None:
    if not all([R2_ACCOUNT_ID, R2_API_TOKEN, R2_BUCKET]):
        log.warning("R2 credentials not set — cannot upload")
        return

    with crop_lock:
        region = crop_region

    if region:
        try:
            jpeg = apply_crop(jpeg, region)
        except Exception as e:
            log.error("Crop failed: %s", e)
            return

    filename = f"{int(time.time() * 1000)}.jpg"
    try:
        r2_put(filename, jpeg, "image/jpeg")
        log.info("Uploaded → r2://%s/%s", R2_BUCKET, filename)
    except Exception as e:
        log.error("R2 upload failed: %s", e)
        return
    update_manifest(filename)


# ---------------------------------------------------------------------------
# Recording loop
# ---------------------------------------------------------------------------
def record_loop() -> None:
    log.info("Recording started (interval=%ds)", interval)
    while recording.is_set():
        start = time.monotonic()
        try:
            with camera_lock:
                jpeg = capture_jpeg()
            upload(jpeg)
        except Exception as e:
            log.error("Record loop error: %s", e)

        elapsed = time.monotonic() - start
        deadline = time.monotonic() + max(0.0, interval - elapsed)
        while recording.is_set() and time.monotonic() < deadline:
            time.sleep(0.1)

    log.info("Recording stopped")


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bloodcam</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #111; color: #eee; font-family: monospace;
         display: flex; flex-direction: column; align-items: center;
         padding: 1.5rem; gap: 1rem; }
  h1 { font-size: 1.4rem; letter-spacing: 0.1em; }
  #preview-wrap { position: relative; width: 100%; max-width: 800px; user-select: none; }
  #preview { width: 100%; border: 2px solid #333; border-radius: 4px; display: block; }
  #crop-canvas { position: absolute; inset: 0; width: 100%; height: 100%;
                 cursor: crosshair; border-radius: 4px; }
  #preview-spinner { display: none; position: absolute; inset: 0;
                     background: rgba(0,0,0,0.5); align-items: center;
                     justify-content: center; font-size: 0.9rem; color: #aaa;
                     pointer-events: none; }
  #preview-spinner.show { display: flex; }
  #controls { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem; }
  #badge { font-size: 0.9rem; padding: 0.3rem 0.8rem; border-radius: 999px;
           background: #333; color: #888; transition: all 0.3s; }
  #badge.live { background: #6f1010; color: #ff6060; }
  button { padding: 0.5rem 1.2rem; font-size: 0.95rem; font-family: monospace;
           cursor: pointer; border: 1px solid #555; border-radius: 4px;
           background: #222; color: #eee; transition: background 0.2s; }
  button:hover:not(:disabled) { background: #333; }
  button:disabled { opacity: 0.4; cursor: default; }
  #crop-hint { font-size: 0.8rem; color: #555; }
</style>
</head>
<body>
<h1>bloodcam</h1>
<div id="preview-wrap">
  <img id="preview" src="" alt="preview">
  <canvas id="crop-canvas"></canvas>
  <div id="preview-spinner">capturing…</div>
</div>
<div id="controls">
  <span id="badge">● OFF</span>
  <button id="toggle-btn" onclick="toggleRecording()">Start Recording</button>
  <button id="preview-btn" onclick="refreshPreview()">Refresh Preview</button>
  <button id="clear-crop-btn" onclick="clearCrop()" style="display:none">Clear Crop</button>
  <label style="display:flex;align-items:center;gap:0.4rem;font-size:0.9rem;color:#aaa">
    Interval
    <input id="interval-input" type="number" min="1" value="30"
           style="width:4.5rem;padding:0.3rem 0.5rem;background:#1a1a1a;border:1px solid #555;
                  border-radius:4px;color:#eee;font-family:monospace;font-size:0.9rem">
    s
    <button onclick="saveInterval()">Set</button>
  </label>
</div>
<div id="crop-hint">Drag on the image to set crop region for uploads.</div>
<script>
  let isRecording = false;
  let cropRect = null;   // {x, y, w, h} normalized 0-1
  let dragStart = null;

  // ---- status ----
  async function fetchStatus() {
    const r = await fetch('/status');
    const d = await r.json();
    setRecording(d.recording);
    if (d.crop) setCropRect(d.crop);
    if (d.interval) document.getElementById('interval-input').value = d.interval;
  }

  async function saveInterval() {
    const secs = parseInt(document.getElementById('interval-input').value, 10);
    if (!secs || secs < 1) return;
    await fetch('/interval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seconds: secs }),
    });
  }

  function setRecording(val) {
    isRecording = val;
    const badge      = document.getElementById('badge');
    const btn        = document.getElementById('toggle-btn');
    const previewBtn = document.getElementById('preview-btn');
    badge.textContent   = isRecording ? '● LIVE' : '● OFF';
    badge.className     = isRecording ? 'live' : '';
    btn.textContent     = isRecording ? 'Stop Recording' : 'Start Recording';
    previewBtn.disabled = isRecording;
    previewBtn.title    = isRecording ? 'Stop recording to use preview' : '';
  }

  async function toggleRecording() {
    const r = await fetch(isRecording ? '/stop' : '/start', { method: 'POST' });
    const d = await r.json();
    setRecording(d.recording);
  }

  // ---- preview ----
  async function refreshPreview() {
    const btn     = document.getElementById('preview-btn');
    const spinner = document.getElementById('preview-spinner');
    btn.disabled  = true;
    spinner.classList.add('show');
    try {
      const r    = await fetch('/preview?t=' + Date.now());
      const blob = await r.blob();
      const img  = document.getElementById('preview');
      img.src    = URL.createObjectURL(blob);
      img.onload = () => { syncCanvas(); redrawCrop(); };
    } finally {
      spinner.classList.remove('show');
      btn.disabled = false;
    }
  }

  // ---- canvas / crop drawing ----
  function syncCanvas() {
    const img = document.getElementById('preview');
    const c   = document.getElementById('crop-canvas');
    c.width   = img.offsetWidth;
    c.height  = img.offsetHeight;
  }

  function redrawCrop() {
    const c   = document.getElementById('crop-canvas');
    const ctx = c.getContext('2d');
    ctx.clearRect(0, 0, c.width, c.height);
    if (!cropRect) return;
    const x = cropRect.x * c.width;
    const y = cropRect.y * c.height;
    const w = cropRect.w * c.width;
    const h = cropRect.h * c.height;
    ctx.fillStyle   = 'rgba(0,0,0,0.45)';
    ctx.fillRect(0, 0, c.width, c.height);
    ctx.clearRect(x, y, w, h);
    ctx.strokeStyle = '#ff4444';
    ctx.lineWidth   = 2;
    ctx.strokeRect(x, y, w, h);
  }

  function pointerPos(e, canvas) {
    const rect = canvas.getBoundingClientRect();
    const src  = e.touches ? e.touches[0] : e;
    return {
      x: Math.max(0, Math.min(1, (src.clientX - rect.left)  / rect.width)),
      y: Math.max(0, Math.min(1, (src.clientY - rect.top)   / rect.height)),
    };
  }

  function drawDrag(p1, p2) {
    const c   = document.getElementById('crop-canvas');
    const ctx = c.getContext('2d');
    ctx.clearRect(0, 0, c.width, c.height);
    const x = Math.min(p1.x, p2.x) * c.width;
    const y = Math.min(p1.y, p2.y) * c.height;
    const w = Math.abs(p2.x - p1.x) * c.width;
    const h = Math.abs(p2.y - p1.y) * c.height;
    ctx.fillStyle   = 'rgba(0,0,0,0.45)';
    ctx.fillRect(0, 0, c.width, c.height);
    ctx.clearRect(x, y, w, h);
    ctx.strokeStyle = '#ff4444';
    ctx.lineWidth   = 2;
    ctx.setLineDash([4, 3]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);
  }

  function setCropRect(rect) {
    cropRect = rect;
    document.getElementById('clear-crop-btn').style.display = rect ? '' : 'none';
    redrawCrop();
  }

  async function saveCrop(rect) {
    const r = await fetch('/crop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rect),
    });
    const d = await r.json();
    setCropRect(d.crop);
  }

  async function clearCrop() {
    const r = await fetch('/crop', { method: 'DELETE' });
    const d = await r.json();
    setCropRect(d.crop);
  }

  // mouse
  const canvas = document.getElementById('crop-canvas');
  canvas.addEventListener('mousedown', (e) => {
    if (isRecording) return;
    dragStart = pointerPos(e, canvas);
  });
  canvas.addEventListener('mousemove', (e) => {
    if (!dragStart) return;
    drawDrag(dragStart, pointerPos(e, canvas));
  });
  canvas.addEventListener('mouseup', (e) => {
    if (!dragStart) return;
    const end = pointerPos(e, canvas);
    const rect = {
      x: Math.min(dragStart.x, end.x),
      y: Math.min(dragStart.y, end.y),
      w: Math.abs(end.x - dragStart.x),
      h: Math.abs(end.y - dragStart.y),
    };
    dragStart = null;
    if (rect.w < 0.02 || rect.h < 0.02) { redrawCrop(); return; }
    saveCrop(rect);
  });

  // touch
  canvas.addEventListener('touchstart', (e) => {
    if (isRecording) return;
    e.preventDefault();
    dragStart = pointerPos(e, canvas);
  }, { passive: false });
  canvas.addEventListener('touchmove', (e) => {
    if (!dragStart) return;
    e.preventDefault();
    drawDrag(dragStart, pointerPos(e, canvas));
  }, { passive: false });
  canvas.addEventListener('touchend', (e) => {
    if (!dragStart) return;
    e.preventDefault();
    const touch = e.changedTouches[0];
    const rect2 = canvas.getBoundingClientRect();
    const end = {
      x: Math.max(0, Math.min(1, (touch.clientX - rect2.left) / rect2.width)),
      y: Math.max(0, Math.min(1, (touch.clientY - rect2.top)  / rect2.height)),
    };
    const rect = {
      x: Math.min(dragStart.x, end.x),
      y: Math.min(dragStart.y, end.y),
      w: Math.abs(end.x - dragStart.x),
      h: Math.abs(end.y - dragStart.y),
    };
    dragStart = null;
    if (rect.w < 0.02 || rect.h < 0.02) { redrawCrop(); return; }
    saveCrop(rect);
  }, { passive: false });

  window.addEventListener('resize', () => { syncCanvas(); redrawCrop(); });

  fetchStatus();
  refreshPreview();
</script>
</body>
</html>
"""

app = Flask(__name__)


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/preview")
def preview():
    if recording.is_set():
        return Response("Recording in progress", status=503)
    try:
        with camera_lock:
            jpeg = capture_jpeg()
        return Response(jpeg, mimetype="image/jpeg")
    except Exception as e:
        log.error("Preview capture failed: %s", e)
        return Response("Capture failed", status=500)


@app.route("/status")
def status():
    with crop_lock:
        region = crop_region
    return jsonify(recording=recording.is_set(), crop=region, interval=interval)


@app.route("/interval", methods=["POST"])
def set_interval():
    global interval
    data = request.get_json(force=True)
    secs = int(data["seconds"])
    if secs < 1:
        return jsonify(error="interval must be >= 1"), 400
    interval = secs
    log.info("Interval set to %ds", interval)
    return jsonify(interval=interval)


@app.route("/start", methods=["POST"])
def start():
    global record_thread
    if not recording.is_set():
        recording.set()
        record_thread = threading.Thread(target=record_loop, daemon=True)
        record_thread.start()
    return jsonify(recording=True)


@app.route("/stop", methods=["POST"])
def stop():
    recording.clear()
    return jsonify(recording=False)


@app.route("/crop", methods=["POST"])
def set_crop():
    global crop_region
    data = request.get_json(force=True)
    region = {
        "x": float(data["x"]),
        "y": float(data["y"]),
        "w": float(data["w"]),
        "h": float(data["h"]),
    }
    with crop_lock:
        crop_region = region
    log.info("Crop set: x=%.3f y=%.3f w=%.3f h=%.3f", region["x"], region["y"], region["w"], region["h"])
    return jsonify(crop=region)


@app.route("/crop", methods=["DELETE"])
def clear_crop():
    global crop_region
    with crop_lock:
        crop_region = None
    log.info("Crop cleared")
    return jsonify(crop=None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    global cam

    try:
        from picamera2 import Picamera2
    except ImportError:
        log.error("picamera2 not found — must run on a Raspberry Pi.")
        sys.exit(1)

    log.info("Starting bloodcam (interval=%ds, bucket=%s)", INTERVAL, R2_BUCKET or "not set")

    try:
        cam = Picamera2()
        cam_config = cam.create_still_configuration(main={"size": (1920, 1080)})
        cam.configure(cam_config)
        cam.start()
        log.info("Camera started (1920x1080)")
        time.sleep(2)
    except Exception as e:
        log.error("Camera init failed: %s", e)
        sys.exit(1)

    log.info("Web UI available at http://0.0.0.0:%d", WEB_PORT)
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)


if __name__ == "__main__":
    main()
