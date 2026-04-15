import json
import logging
import os
import socket
import sys
import threading
import time

import requests
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bloodcam")

# ---------------------------------------------------------------------------
# Waveshare 1.44" LCD HAT driver
# LCD_1in44.py and config.py must be in the same directory as this script.
# ---------------------------------------------------------------------------
try:
    import LCD_1in44
    HAT_AVAILABLE = True
except ImportError:
    HAT_AVAILABLE = False
    log.warning("LCD_1in44 not found — running without display/buttons.")

# ---------------------------------------------------------------------------
# Config — set as environment variables on the Pi
#   R2_ACCOUNT_ID : Cloudflare account ID
#   R2_API_TOKEN  : Cloudflare API token with R2 read/write permission
#   R2_BUCKET     : bucket name (e.g. bloodcam)
# ---------------------------------------------------------------------------
R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_API_TOKEN  = os.environ.get("R2_API_TOKEN")
R2_BUCKET     = os.environ.get("R2_BUCKET")

INTERVAL     = 5
CAPTURE_PATH = "/tmp/bloodcam.jpg"

# ---------------------------------------------------------------------------
# Upload state — toggled by KEY1 on the HAT
# ---------------------------------------------------------------------------
upload_enabled = threading.Event()   # set = uploading, cleared = paused


def toggle_upload() -> None:
    if upload_enabled.is_set():
        upload_enabled.clear()
        log.info("Upload disabled (KEY1 pressed)")
    else:
        upload_enabled.set()
        log.info("Upload enabled (KEY1 pressed)")


# ---------------------------------------------------------------------------
# R2 helpers — Cloudflare REST API with Bearer token
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        log.warning("Could not determine local IP: %s", e)
        return "No IP"


def make_display_image(ip: str, uploading: bool) -> Image.Image:
    try:
        img = Image.open(CAPTURE_PATH).convert("RGB").resize((128, 128), Image.LANCZOS)
    except Exception:
        img = Image.new("RGB", (128, 128), (20, 20, 20))

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=11)
    except TypeError:
        font = ImageFont.load_default()

    draw.rectangle([0, 112, 128, 128], fill=(0, 0, 0))
    draw.text((3, 115), ip, font=font, fill=(200, 200, 200))

    status_text  = "● LIVE" if uploading else "● OFF"
    status_color = (255, 60, 60) if uploading else (110, 110, 110)
    try:
        bbox   = draw.textbbox((0, 0), status_text, font=font)
        text_w = bbox[2] - bbox[0]
    except AttributeError:
        text_w = len(status_text) * 6
    draw.text((125 - text_w, 115), status_text, font=font, fill=status_color)

    return img


def update_manifest(filename: str) -> None:
    """Append filename to manifest.json so the bot can discover it without credentials."""
    try:
        raw = r2_get("manifest.json")
        manifest: list[str] = json.loads(raw) if raw else []
    except Exception as e:
        log.error("Manifest GET failed: %s", e)
        return

    manifest.append(filename)
    try:
        r2_put("manifest.json", json.dumps(manifest).encode(), "application/json")
        log.debug("Manifest updated (%d entries)", len(manifest))
    except Exception as e:
        log.error("Manifest PUT failed: %s", e)


def capture_and_upload(camera) -> None:
    try:
        camera.capture_file(CAPTURE_PATH)
        log.debug("Captured → %s", CAPTURE_PATH)
    except Exception as e:
        log.error("Capture failed: %s", e)
        return

    if not upload_enabled.is_set():
        log.debug("Upload disabled — skipping")
        return

    if not all([R2_ACCOUNT_ID, R2_API_TOKEN, R2_BUCKET]):
        log.warning("R2 credentials not set — cannot upload")
        return

    filename = f"{int(time.time() * 1000)}.jpg"
    try:
        with open(CAPTURE_PATH, "rb") as f:
            r2_put(filename, f.read(), "image/jpeg")
        log.info("Uploaded → r2://%s/%s", R2_BUCKET, filename)
    except Exception as e:
        log.error("R2 upload failed: %s", e)
        return

    update_manifest(filename)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    try:
        from picamera2 import Picamera2
    except ImportError:
        log.error("picamera2 not found — must run on a Raspberry Pi.")
        sys.exit(1)

    log.info("Starting bloodcam (interval=%ds, bucket=%s)", INTERVAL, R2_BUCKET or "not set")

    lcd = None
    if HAT_AVAILABLE:
        try:
            lcd = LCD_1in44.LCD()
            lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
            lcd.LCD_Clear()
            lcd.GPIO_KEY1_PIN.when_activated = toggle_upload
            log.info("Display ready — press KEY1 to toggle upload on/off")
        except Exception as e:
            log.error("LCD init failed: %s", e)
            lcd = None

    ip = get_local_ip()
    log.info("Local IP: %s", ip)

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

    if lcd:
        lcd.LCD_ShowImage(make_display_image(ip, False), 0, 0)

    log.info("Capture loop running — press KEY1 to start uploading")
    while True:
        start = time.monotonic()

        capture_and_upload(cam)

        if lcd:
            lcd.LCD_ShowImage(make_display_image(ip, upload_enabled.is_set()), 0, 0)

        elapsed   = time.monotonic() - start
        sleep_for = max(0.0, INTERVAL - elapsed)
        if sleep_for:
            time.sleep(sleep_for)


if __name__ == "__main__":
    main()
