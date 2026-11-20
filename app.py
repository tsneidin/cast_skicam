"""LAN-only live ski camera channel and explicit Hub Max playback controls."""
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from uuid import UUID
from zoneinfo import ZoneInfo

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

try:
    import pychromecast
except ImportError:
    pychromecast = None

import catalog
from rotation import ShuffledRotation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# Stream directory: use environment variable, default to /stream if writable, else ./stream
stream_dir_env = os.environ.get("STREAM_DIR")
if stream_dir_env:
    ROOT = Path(stream_dir_env)
else:
    try:
        Path("/stream").mkdir(parents=True, exist_ok=True)
        ROOT = Path("/stream")
    except (PermissionError, OSError):
        ROOT = Path("./stream")
ROOT.mkdir(parents=True, exist_ok=True)

CAMS_FILE = Path(__file__).with_name("cameras.json")
CAMS = json.loads(CAMS_FILE.read_text()) if CAMS_FILE.exists() else []

catalog_cache_env = os.environ.get("CATALOG_CACHE")
if catalog_cache_env:
    CATALOG_CACHE = Path(catalog_cache_env)
else:
    try:
        Path("/data").mkdir(parents=True, exist_ok=True)
        CATALOG_CACHE = Path("/data/cameras.json")
    except (PermissionError, OSError):
        CATALOG_CACHE = Path("./data/cameras.json")

PORT = int(os.environ.get("PORT", "8106"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
BASE = os.environ.get("PUBLIC_URL") or f"http://{BIND_HOST if BIND_HOST != '0.0.0.0' else 'localhost'}:{PORT}"
STREAM_URL = BASE + "/live.m3u8"
SECONDS = max(10, min(600, int(os.environ.get("ROTATE_SECONDS", "45"))))
AUTO_START = os.environ.get("AUTO_START", "true").lower() in ("1", "true", "yes", "on")

STATE = {
    "active": False,
    "casting": False,
    "camera": None,
    "error": None,
    "generation": 0,
    "rotation_seconds": SECONDS,
    "completed_cameras": 0,
    "cast_state": None,
    "failed_cameras": 0,
    "catalog_updated": None,
    "rotation_mode": "shuffled_no_repeat",
    "auto_start": AUTO_START,
}

LOCK = threading.RLock()
WAKE = threading.Event()
PROCESS = None
ENCODER = None
FRAMES = queue.Queue(maxsize=3)
CAST = None
WEATHER = {}
FAILED_UNTIL = {}
ROTATION = ShuffledRotation()
SKIP_ACTION = None

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def get_font(path, size, bold=False):
    candidates = [
        path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "DejaVuSans.ttf",
        "Arial.ttf",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                pass
    try:
        return ImageFont.truetype("DejaVuSans", size)
    except Exception:
        return ImageFont.load_default()


def fetch(url):
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=12) as response:
        return response.read().decode()


def rendition(url):
    """Prefer a 720p/1080p rendition rather than decoding a 4K master."""
    lines = fetch(url).splitlines()
    variants = []
    import re
    for i, line in enumerate(lines[:-1]):
        if line.startswith("#EXT-X-STREAM-INF"):
            match = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            if match:
                variants.append((int(match[1]), urljoin(url, lines[i + 1])))
    if not variants:
        return url
    suitable = [v for v in variants if v[0] >= 1280]
    return min(suitable or variants)[1]


def weather(cam):
    if "lat" not in cam or "lon" not in cam:
        return "Live camera"
    key = cam["resort"]
    cached = WEATHER.get(key)
    if cached and time.time() - cached[0] < 900:
        return cached[1]
    try:
        query = urlencode({
            "latitude": cam["lat"],
            "longitude": cam["lon"],
            "current": "temperature_2m,wind_speed_10m,weather_code",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "America/Denver",
        })
        current = json.loads(fetch("https://api.open-meteo.com/v1/forecast?" + query))["current"]
        code = current["weather_code"]
        condition = (
            "Clear" if code == 0 else "Partly cloudy" if code < 3 else
            "Overcast" if code == 3 else "Fog" if code < 50 else
            "Snow" if 71 <= code <= 77 or code in (85, 86) else
            "Thunderstorms" if code >= 95 else "Rain"
        )
        result = f'{round(current["temperature_2m"])}°F   {condition}   Wind {round(current["wind_speed_10m"])} mph'
        WEATHER[key] = (time.time(), result)
        return result
    except Exception as error:
        logging.warning("Weather unavailable for %s: %s", key, error)
        return "Weather unavailable"


def footer(cam, next_cam):
    if Image is None:
        logging.warning("PIL not installed; skipping footer overlay generation")
        return
    image = Image.new("RGB", (1280, 100), "#0b1821")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1279, 2), fill="#739cac")
    draw.text((28, 13), cam["resort"], font=get_font(BOLD, 30, bold=True), fill="white")
    draw.text((28, 57), cam["camera"] + "  /  " + cam.get("region", "Colorado"), font=get_font(FONT, 19), fill="#b7c9d2")
    draw.text((510, 18), weather(cam), font=get_font(FONT, 21), fill="white")
    next_label = next_cam["resort"] + " / " + next_cam["camera"] if next_cam else "New random cycle"
    draw.text((510, 59), "Next: " + next_label, font=get_font(FONT, 17), fill="#b7c9d2")
    if cam.get("timezone"):
        clock = datetime.now(ZoneInfo(cam["timezone"])).strftime("%I:%M %p")
        draw.text((1110, 59), clock + " " + cam.get("zone_label", ""), font=get_font(FONT, 17), fill="#b7c9d2")
    image.save(ROOT / "footer.png")


def resolve(cam):
    kind, url = cam["kind"], cam["url"]
    if kind == "image":
        request = Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
        with urlopen(request, timeout=15) as response:
            content = response.read(12_000_000)
        if len(content) < 5000:
            raise RuntimeError("Image empty or too small")
        from io import BytesIO
        image = Image.open(BytesIO(content))
        image.verify()
        image = Image.open(BytesIO(content)).convert("RGB")
        path = ROOT / "camera-image.jpg"
        image.save(path, "JPEG")
        return str(path), True
    if kind == "embedded" and "/ipcamlive-player.html" in url:
        alias = parse_qs(urlparse(url).query)["alias"][0]
        endpoint = "https://www.ipcamlive.com/ajax/getcamerastreamstate.php?cameraalias=" + alias
        details = json.loads(fetch(endpoint))["details"]
        if details.get("streamavailable") != "1":
            raise RuntimeError("IPCamLive feed unavailable")
        url = details["address"].replace("http://", "https://") + "streams/" + details["streamid"] + "/stream.m3u8"
    elif kind == "embedded" and "/brownrice-player.html" in url:
        cam_id = parse_qs(urlparse(url).query)["cam"][0]
        script = fetch("https://live5.brownrice.com/?" + urlencode({"sn": cam_id, "em": "1", "framed": "true"}))
        import re
        match = re.search(r'\["playlistUrl"\]\s*=\s*"([^"]+\.m3u8)', script)
        if not match:
            raise RuntimeError("Brownrice feed unavailable")
        url = match.group(1)
    elif kind in ("youtube", "embedded"):
        result = subprocess.run(["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "10",
                                 "-f", "b[height<=720]/bv*[height<=720]/bv*", "-g", url], capture_output=True,
                                text=True, timeout=25)
        if result.returncode:
            raise RuntimeError("Cannot resolve embedded feed: " + result.stderr[-200:])
        url = result.stdout.strip().splitlines()[0]
    if ".m3u8" in url.split("?", 1)[0]:
        url = rendition(url)
    return url, False


def refresh_catalog():
    global CAMS
    while True:
        try:
            updated = catalog.load(CATALOG_CACHE)
            if updated:
                with LOCK:
                    CAMS = updated
                    STATE["catalog_updated"] = datetime.now().isoformat()
                logging.info("Loaded %d cameras from Ski Cams Live", len(updated))
        except Exception as error:
            logging.warning("Catalog refresh failed: %s", error)
        time.sleep(24 * 3600)


def pump_frames(process, generation):
    frame = bytes([16]) * (1280 * 720) + bytes([128]) * (1280 * 720 // 2)
    tick = time.monotonic()
    try:
        while STATE['active'] and STATE['generation'] == generation:
            try:
                frame = FRAMES.get_nowait()
            except queue.Empty:
                pass
            process.stdin.write(frame)
            tick += 1 / 15
            time.sleep(max(0, tick - time.monotonic()))
    except (BrokenPipeError, OSError):
        if STATE['active'] and STATE['generation'] == generation:
            STATE['error'] = 'Continuous video encoder stopped'
            stop(False)
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass


def launch_encoder():
    return subprocess.Popen([
        'ffmpeg', '-hide_banner', '-loglevel', 'warning', '-nostdin',
        '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-s', '1280x720', '-r', '15', '-i', 'pipe:0',
        '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000',
        '-map', '0:v', '-map', '1:a', '-c:a', 'aac', '-b:a', '64k',
        '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast',
        '-profile:v', 'main', '-level:v', '3.1', '-pix_fmt', 'yuv420p', '-crf', '24',
        '-maxrate', '1600k', '-bufsize', '3200k', '-g', '30', '-keyint_min', '30',
        '-sc_threshold', '0', '-f', 'hls', '-hls_time', '2', '-hls_list_size', '5',
        '-hls_flags', 'delete_segments+omit_endlist+temp_file',
        '-hls_segment_filename', str(ROOT / 'segment%08d.ts'), str(ROOT / 'live.m3u8')
    ], stdin=subprocess.PIPE)


def next_camera():
    global SKIP_ACTION, PROCESS
    with LOCK:
        SKIP_ACTION = "next"
        STATE['camera'] = "Switching to next camera…"
        while not FRAMES.empty():
            try:
                FRAMES.get_nowait()
            except queue.Empty:
                break
        p = PROCESS
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


def prev_camera():
    global SKIP_ACTION, PROCESS
    with LOCK:
        SKIP_ACTION = "prev"
        STATE['camera'] = "Switching to previous camera…"
        while not FRAMES.empty():
            try:
                FRAMES.get_nowait()
            except queue.Empty:
                break
        p = PROCESS
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


def set_duration(seconds):
    try:
        sec = max(10, min(600, int(seconds)))
        with LOCK:
            STATE["rotation_seconds"] = sec
        logging.info("Viewing duration updated to %d seconds", sec)
        return sec
    except (ValueError, TypeError):
        return None


def channel():
    global PROCESS, SKIP_ACTION
    frame_size = 1280 * 720 * 3 // 2
    while True:
        if not STATE['active']:
            WAKE.wait(1)
            WAKE.clear()
            continue
        generation = STATE['generation']

        with LOCK:
            action = SKIP_ACTION
            SKIP_ACTION = None

        if action == "prev":
            cam, next_cam = ROTATION.prev(CAMS, FAILED_UNTIL)
            if cam is None:
                cam, next_cam = ROTATION.next(CAMS, FAILED_UNTIL)
        else:
            cam, next_cam = ROTATION.next(CAMS, FAILED_UNTIL)

        if cam is None:
            time.sleep(5)
            continue

        with LOCK:
            STATE['camera'] = cam['resort'] + ' / ' + cam['camera']
            STATE['error'] = None

        timer = None
        process = None
        current_duration = STATE.get("rotation_seconds", SECONDS)
        try:
            footer(cam, next_cam)
            if SKIP_ACTION:
                continue
            source, still = resolve(cam)
            if SKIP_ACTION:
                continue

            logging.info('Producing %s (duration %ds)', STATE['camera'], current_duration)
            filters = ('[0:v]fps=15,scale=1280:620:force_original_aspect_ratio=increase,'
                       'crop=1280:620,pad=1280:720:0:0:color=black,setsar=1[v];'
                       '[v][1:v]overlay=0:620:shortest=1[out]')
            input_args = ['-loop', '1', '-framerate', '1', '-i', source] if still else ['-rw_timeout', '12000000', '-threads', '2', '-re', '-i', source]
            command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                       *input_args,
                       '-loop', '1', '-i', str(ROOT / 'footer.png'),
                       '-filter_complex_threads', '1', '-filter_complex', filters,
                       '-map', '[out]', '-an', '-t', str(current_duration), '-r', '15',
                       '-threads', '2', '-pix_fmt', 'yuv420p', '-f', 'rawvideo', 'pipe:1']
            with LOCK:
                if not STATE['active'] or STATE['generation'] != generation or SKIP_ACTION:
                    continue
                process = subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=frame_size)
                PROCESS = process
            timer = threading.Timer(25, process.kill)
            timer.start()
            first_frame = False
            while STATE['active'] and STATE['generation'] == generation:
                if SKIP_ACTION:
                    break
                frame = process.stdout.read(frame_size)
                if len(frame) != frame_size:
                    break
                if not first_frame:
                    first_frame = True
                    timer.cancel()
                    timer = threading.Timer(current_duration + 30, process.kill)
                    timer.start()
                while STATE['active'] and STATE['generation'] == generation:
                    if SKIP_ACTION:
                        break
                    try:
                        FRAMES.put(frame, timeout=0.2)
                        break
                    except queue.Full:
                        continue

            was_skipped = bool(SKIP_ACTION)
            if process.poll() is None:
                process.kill()
            result = process.wait(timeout=5)

            if was_skipped:
                logging.info('Skipping camera on user request: %s', cam['camera'])
                STATE['error'] = None
                continue

            if result and STATE['active'] and STATE['generation'] == generation:
                raise RuntimeError(f'Camera decoder exited with status {result}')
            if not first_frame and STATE['active'] and STATE['generation'] == generation:
                raise RuntimeError("No video frames received")
            if result == 0 and STATE['generation'] == generation:
                STATE['completed_cameras'] += 1
                STATE['error'] = None
        except Exception as error:
            was_skipped = bool(SKIP_ACTION)
            if not was_skipped and STATE['generation'] == generation:
                STATE['error'] = str(error)
                STATE['failed_cameras'] += 1
                FAILED_UNTIL[cam["url"]] = time.time() + 3600
                logging.exception('Camera failed: %s', cam['camera'])
            if process and process.poll() is None:
                process.kill()
            time.sleep(1)
        finally:
            if timer:
                timer.cancel()
            with LOCK:
                if PROCESS is process:
                    PROCESS = None
            if process and process.stdout:
                process.stdout.close()


def connect_cast():
    global CAST
    if pychromecast is None:
        logging.warning("PyChromecast not installed; running in streaming-only mode")
        return None
    cast_host = os.environ.get("CAST_HOST", "").strip()
    cast_uuid = os.environ.get("CAST_UUID", "").strip()
    if not cast_host:
        return None
    if CAST is None:
        try:
            uuid_obj = UUID(cast_uuid) if cast_uuid else None
            CAST = pychromecast.get_chromecast_from_host(
                (cast_host, 8009, uuid_obj, "Google Nest Hub Max", "Kitchen Display"),
                tries=2, timeout=10)
            CAST.wait(timeout=20)
        except Exception as e:
            logging.warning("Chromecast connection failed: %s", e)
            CAST = None
    return CAST


def stop(quit_app=True):
    with LOCK:
        STATE["active"] = False
        STATE["generation"] += 1
        STATE["casting"] = False
        for process in (PROCESS, ENCODER):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
    if quit_app and CAST and CAST.media_controller.status.content_id == STREAM_URL:
        try:
            CAST.quit_app()
        except Exception:
            pass


def start():
    global ENCODER
    with LOCK:
        if STATE["active"]:
            if CAST and CAST.media_controller.status.content_id == STREAM_URL and CAST.media_controller.status.player_state == "PAUSED":
                CAST.media_controller.play()
            return
        # A new session must not replay an old cached playlist.
        for path in ROOT.glob("*"):
            if path.suffix in (".ts", ".m3u8", ".tmp"):
                path.unlink(missing_ok=True)
        while not FRAMES.empty():
            try:
                FRAMES.get_nowait()
            except queue.Empty:
                break
        ENCODER = launch_encoder()
        STATE["generation"] += 1
        STATE.update(active=True, casting=False, error=None)
        threading.Thread(target=pump_frames, args=(ENCODER, STATE["generation"]), daemon=True).start()
        WAKE.set()

    cast_host = os.environ.get("CAST_HOST", "").strip()
    if not cast_host:
        logging.info("CAST_HOST not configured. Video stream active at %s", STREAM_URL)
        return

    try:
        cast = connect_cast()
        if not cast:
            logging.warning("Could not reach Chromecast at %s", cast_host)
            STATE["error"] = f"Chromecast at {cast_host} unreachable"
            return
        if cast.media_controller.status.content_id == STREAM_URL:
            cast.quit_app()
        deadline = time.monotonic() + 300
        while STATE["active"] and time.monotonic() < deadline:
            playlist = ROOT / "live.m3u8"
            if playlist.exists() and playlist.read_text().count("#EXTINF") >= 3:
                cast.media_controller.play_media(STREAM_URL, "application/vnd.apple.mpegurl",
                         title="Ski cameras", stream_type="LIVE")
                cast.media_controller.block_until_active(timeout=20)
                STATE["casting"] = True
                return
            time.sleep(1)
        if STATE["active"]:
            raise TimeoutError("No playable camera stream was produced within 300 seconds")
    except Exception as error:
        STATE["error"] = str(error)
        logging.exception("Cannot start display on Hub Max")


def monitor():
    misses = 0
    while True:
        time.sleep(3)
        if not CAST or not STATE["casting"]:
            misses = 0
            continue
        try:
            status = CAST.media_controller.status
            STATE["cast_state"] = status.player_state
            ours = status.content_id == STREAM_URL and status.player_state in ("PLAYING", "BUFFERING", "PAUSED")
            misses = 0 if ours else misses + 1
            if misses >= 5:
                logging.info("Playback stopped or another application took over")
                stop(False)
        except Exception:
            pass


def shutdown(signum, frame):
    stop()
    raise SystemExit(0)


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ski cameras</title>
  <style>
    body { background: #0b1821; color: #edf4f7; font: 17px system-ui, -apple-system, sans-serif; margin: 4vw auto; max-width: 860px; padding: 0 20px; }
    h1 { font-size: 36px; margin-bottom: 4px; }
    p { line-height: 1.5; color: #b7c9d2; }
    .btn-group { display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0; }
    button {
      font: inherit; font-size: 16px; font-weight: 500; padding: 12px 22px;
      border: 1px solid #739cac; border-radius: 6px; background: #233d4a; color: white;
      cursor: pointer; transition: background 0.15s, transform 0.1s;
    }
    button:hover { background: #2f5060; }
    button:active { transform: scale(0.98); }
    button.primary { background: #1c5d79; border-color: #5aa1c2; }
    button.primary:hover { background: #257496; }
    button.danger { background: #5c2727; border-color: #9c4b4b; }
    button.danger:hover { background: #733131; }
    .duration-panel {
      background: #13222d; padding: 16px 20px; border-radius: 8px; border: 1px solid #233d4a;
      margin: 20px 0; display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
    }
    .duration-panel label { font-weight: 500; color: #edf4f7; }
    input[type=number] {
      font: inherit; padding: 8px 12px; width: 80px; text-align: center;
      background: #0b1821; color: white; border: 1px solid #739cac; border-radius: 6px;
    }
    .quick-btn { padding: 6px 12px; font-size: 14px; background: #1c323d; border-color: #3b5f70; }
    .card { background: #13222d; border-radius: 8px; border: 1px solid #233d4a; padding: 18px 20px; margin: 20px 0; min-height: 90px; }
    #status { font-size: 18px; color: #edf4f7; margin-bottom: 6px; }
    #camera-name { font-size: 20px; color: #5bc0be; font-weight: bold; margin: 4px 0 10px; min-height: 28px; }
    #cameras { margin: 0; color: #8fa7b3; font-size: 15px; }
    small { color: #8fa7b3; display: block; margin-top: 24px; line-height: 1.4; }
    a { color: #7ec8e3; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>Ski cameras</h1>
  <p>Live mountain webcams · Google Nest Hub Max & LAN stream</p>

  <div class="btn-group">
    <button class="primary" onclick="control('start')">▶ Start App</button>
    <button class="danger" onclick="control('stop')">⏹ Stop</button>
    <button id="btn-prev" onclick="control('prev')">⏮ Prev</button>
    <button id="btn-next" onclick="control('next')">Next ⏭</button>
  </div>

  <div class="duration-panel">
    <label for="duration">Viewing duration:</label>
    <input type="number" id="duration" min="10" max="600" step="5" value="45">
    <span>sec</span>
    <button onclick="applyDuration()">Set</button>
    <button class="quick-btn" onclick="setQuick(30)">30s</button>
    <button class="quick-btn" onclick="setQuick(45)">45s</button>
    <button class="quick-btn" onclick="setQuick(60)">60s</button>
    <button class="quick-btn" onclick="setQuick(90)">90s</button>
    <button class="quick-btn" onclick="setQuick(120)">2m</button>
  </div>

  <div class="card">
    <div id="status">Loading status…</div>
    <div id="camera-name"></div>
    <p id="cameras"></p>
  </div>

  <p>Stream URL: <a href="/live.m3u8" target="_blank">/live.m3u8</a></p>

  <small>Resort camera feeds discovered through Ski Cams Live. Weather: Open-Meteo. No OnTheSnow graphics, QR codes, or advertising added.</small>

  <script>
    let durationFocused = false;
    const durInput = document.getElementById('duration');
    durInput.addEventListener('focus', () => durationFocused = true);
    durInput.addEventListener('blur', () => durationFocused = false);

    async function control(action) {
      const statusEl = document.getElementById('status');
      const camEl = document.getElementById('camera-name');
      if (action === 'next') {
        camEl.textContent = '⏭ Switching to next camera…';
      } else if (action === 'prev') {
        camEl.textContent = '⏮ Switching to previous camera…';
      } else if (action === 'start') {
        statusEl.textContent = 'Starting…';
      } else if (action === 'stop') {
        statusEl.textContent = 'Stopping…';
      }
      try {
        const res = await (await fetch('/' + action, { method: 'POST' })).json();
        if (res.camera && !res.camera.startsWith('Switching')) {
          camEl.textContent = res.camera;
        }
      } catch (e) {
        statusEl.textContent = 'Error sending command';
      }
      setTimeout(update, 300);
    }

    async function applyDuration() {
      const val = parseInt(durInput.value, 10);
      if (val >= 10 && val <= 600) {
        await fetch('/duration', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({seconds: val})
        });
        update();
      }
    }

    function setQuick(sec) {
      durInput.value = sec;
      applyDuration();
    }

    async function update() {
      try {
        const s = await (await fetch('/status')).json();
        const activeText = s.active ? (s.casting ? 'Casting to Hub Max' : 'Channel Streaming') : 'Stopped';
        document.getElementById('status').textContent = activeText + (s.error ? ' | Error: ' + s.error : '');
        if (s.active && s.camera) {
          document.getElementById('camera-name').textContent = s.camera;
        } else if (!s.active) {
          document.getElementById('camera-name').textContent = '';
        }
        document.getElementById('cameras').textContent = 
          s.camera_count + ' cameras in catalog · ' +
          s.failed_cameras + ' failed attempts · ' +
          'Rotation: ' + (s.rotation_seconds || 45) + 's';
        if (!durationFocused && s.rotation_seconds) {
          durInput.value = s.rotation_seconds;
        }
      } catch (e) {
        document.getElementById('status').textContent = 'Unable to reach service';
      }
    }

    update();
    setInterval(update, 2000);
  </script>
</body>
</html>"""


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def reply(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self.reply(PAGE.encode(), "text/html; charset=utf-8")
        if path == "/status":
            state = dict(STATE, camera_count=len(CAMS), cooling_down=sum(t > time.time() for t in FAILED_UNTIL.values()))
            if CAST:
                try:
                    s = CAST.media_controller.status
                    state["receiver"] = {
                        "player_state": s.player_state,
                        "content_id": s.content_id,
                        "idle_reason": s.idle_reason,
                        "app_id": CAST.app_id,
                    }
                except Exception:
                    pass
            return self.reply(json.dumps(state).encode(), "application/json")
        if not (path == "/live.m3u8" or
                (path.startswith("/segment") and path.endswith(".ts") and "/" not in path[1:])):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin:
            origin_netloc = urlparse(origin).netloc
            host = self.headers.get("Host", "")
            if origin_netloc != host and origin_netloc.split(":")[0] != host.split(":")[0]:
                return self.send_error(403)

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        path = self.path.split("?")[0]
        params = parse_qs(urlparse(self.path).query)
        if body:
            try:
                if self.headers.get("Content-Type", "").startswith("application/json"):
                    data = json.loads(body.decode("utf-8"))
                    for k, v in data.items():
                        params[k] = [str(v)]
                else:
                    for k, v in parse_qs(body.decode("utf-8", "replace")).items():
                        params[k] = v
            except Exception:
                pass

        if path == "/start":
            threading.Thread(target=start, daemon=True).start()
        elif path == "/stop":
            stop()
        elif path == "/next":
            if not STATE["active"]:
                threading.Thread(target=start, daemon=True).start()
            next_camera()
            return self.reply(json.dumps({"ok": True, "camera": STATE.get("camera")}).encode(), "application/json")
        elif path == "/prev":
            if not STATE["active"]:
                threading.Thread(target=start, daemon=True).start()
            prev_camera()
            return self.reply(json.dumps({"ok": True, "camera": STATE.get("camera")}).encode(), "application/json")
        elif path == "/duration":
            val = params.get("seconds", params.get("duration", [None]))[0]
            if val is not None:
                updated = set_duration(val)
                if updated is not None:
                    return self.reply(json.dumps({"ok": True, "rotation_seconds": updated}).encode(), "application/json")
                return self.send_error(400, "Invalid duration")
            return self.send_error(400, "Missing duration parameter")
        else:
            return self.send_error(404)
        self.reply(b'{"ok":true}', "application/json")

    def log_message(self, fmt, *args):
        path = self.path.split("?")[0]
        if path in ("/start", "/stop", "/next", "/prev", "/duration", "/live.m3u8"):
            logging.info(self.client_address[0] + " " + fmt, *args)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    for target in (channel, monitor, refresh_catalog):
        threading.Thread(target=target, daemon=True).start()
    if AUTO_START:
        threading.Thread(target=start, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logging.info("Starting ski-cams HTTP server on port %d", PORT)
    server.serve_forever()
