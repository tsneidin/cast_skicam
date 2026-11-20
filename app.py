"""LAN-only live ski camera channel and explicit Hub Max playback controls."""
import json
import logging
import os
import queue
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

from PIL import Image, ImageDraw, ImageFont
import pychromecast
import catalog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
ROOT = Path(os.environ.get("STREAM_DIR", "/stream"))
ROOT.mkdir(parents=True, exist_ok=True)
CAMS = json.loads(Path("cameras.json").read_text())
CATALOG_CACHE = Path("/data/cameras.json")
BASE = os.environ["PUBLIC_URL"]
STREAM_URL = BASE + "/live.m3u8"
SECONDS = max(15, min(300, int(os.environ.get("ROTATE_SECONDS", "45"))))
STATE = {"active": False, "casting": False, "camera": None, "error": None,
         "generation": 0, "rotation_seconds": SECONDS, "completed_cameras": 0, "cast_state": None,
         "failed_cameras": 0, "catalog_updated": None}
LOCK = threading.RLock()
WAKE = threading.Event()
PROCESS = None
ENCODER = None
FRAMES = queue.Queue(maxsize=3)
CAST = None
WEATHER = {}
FAILED_UNTIL = {}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


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
        query = urlencode({"latitude": cam["lat"], "longitude": cam["lon"],
                           "current": "temperature_2m,wind_speed_10m,weather_code",
                           "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                           "timezone": "America/Denver"})
        current = json.loads(fetch("https://api.open-meteo.com/v1/forecast?" + query))["current"]
        code = current["weather_code"]
        condition = ("Clear" if code == 0 else "Partly cloudy" if code < 3 else
                     "Overcast" if code == 3 else "Fog" if code < 50 else
                     "Snow" if 71 <= code <= 77 or code in (85, 86) else
                     "Thunderstorms" if code >= 95 else "Rain")
        result = f'{round(current["temperature_2m"])}°F   {condition}   Wind {round(current["wind_speed_10m"])} mph'
        WEATHER[key] = (time.time(), result)
        return result
    except Exception as error:
        logging.warning("Weather unavailable for %s: %s", key, error)
        return "Weather unavailable"


def footer(cam, next_cam):
    image = Image.new("RGB", (1280, 100), "#0b1821")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1279, 2), fill="#739cac")
    draw.text((28, 13), cam["resort"], font=ImageFont.truetype(BOLD, 30), fill="white")
    draw.text((28, 57), cam["camera"] + "  /  " + cam.get("region", "Colorado"), font=ImageFont.truetype(FONT, 19), fill="#b7c9d2")
    draw.text((510, 18), weather(cam), font=ImageFont.truetype(FONT, 21), fill="white")
    draw.text((510, 59), "Next: " + next_cam["resort"] + " / " + next_cam["camera"],
              font=ImageFont.truetype(FONT, 17), fill="#b7c9d2")
    if cam.get("timezone"):
        clock = datetime.now(ZoneInfo(cam["timezone"])).strftime("%I:%M %p")
        draw.text((1110, 59), clock + " " + cam.get("zone_label", ""),
                  font=ImageFont.truetype(FONT, 17), fill="#b7c9d2")
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
                                 "-f", "b[height<=720]/b", "-g", url], capture_output=True,
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
        '-maxrate', '1600k', '-bufsize', '3200k', '-g', '45', '-keyint_min', '45',
        '-sc_threshold', '0', '-f', 'hls', '-hls_time', '3', '-hls_list_size', '12',
        '-hls_flags', 'delete_segments+omit_endlist+temp_file',
        '-hls_segment_filename', str(ROOT / 'segment%08d.ts'), str(ROOT / 'live.m3u8')
    ], stdin=subprocess.PIPE)


def channel():
    global PROCESS
    index = 0
    frame_size = 1280 * 720 * 3 // 2
    while True:
        if not STATE['active']:
            WAKE.wait(1)
            WAKE.clear()
            continue
        generation = STATE['generation']
        lineup = CAMS
        cam = lineup[index % len(lineup)]
        next_cam = lineup[(index + 1) % len(lineup)]
        if FAILED_UNTIL.get(cam["url"], 0) > time.time():
            index += 1
            continue
        timer = None
        process = None
        try:
            footer(cam, next_cam)
            source, still = resolve(cam)
            STATE['camera'] = cam['resort'] + ' / ' + cam['camera']
            logging.info('Producing %s', STATE['camera'])
            filters = ('[0:v]fps=15,scale=1280:620:force_original_aspect_ratio=increase,'
                       'crop=1280:620,pad=1280:720:0:0:color=black,setsar=1[v];'
                       '[v][1:v]overlay=0:620:shortest=1[out]')
            input_args = ['-loop', '1', '-framerate', '1', '-i', source] if still else ['-rw_timeout', '12000000', '-threads', '2', '-re', '-i', source]
            command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                       *input_args,
                       '-loop', '1', '-i', str(ROOT / 'footer.png'),
                       '-filter_complex_threads', '1', '-filter_complex', filters,
                       '-map', '[out]', '-an', '-t', str(SECONDS), '-r', '15',
                       '-threads', '2', '-pix_fmt', 'yuv420p', '-f', 'rawvideo', 'pipe:1']
            with LOCK:
                if not STATE['active'] or STATE['generation'] != generation:
                    continue
                process = subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=frame_size)
                PROCESS = process
            timer = threading.Timer(25, process.kill)
            timer.start()
            first_frame = False
            while STATE['active'] and STATE['generation'] == generation:
                frame = process.stdout.read(frame_size)
                if len(frame) != frame_size:
                    break
                if not first_frame:
                    first_frame = True
                    timer.cancel()
                    timer = threading.Timer(SECONDS + 30, process.kill)
                    timer.start()
                while STATE['active'] and STATE['generation'] == generation:
                    try:
                        FRAMES.put(frame, timeout=0.2)
                        break
                    except queue.Full:
                        continue
            if process.poll() is None and (not STATE['active'] or STATE['generation'] != generation):
                process.terminate()
            result = process.wait(timeout=15)
            if result and STATE['active'] and STATE['generation'] == generation:
                raise RuntimeError(f'Camera decoder exited with status {result}')
            if not first_frame and STATE['active'] and STATE['generation'] == generation:
                raise RuntimeError("No video frames received")
            if result == 0 and STATE['generation'] == generation:
                STATE['completed_cameras'] += 1
                STATE['error'] = None
        except Exception as error:
            if STATE['generation'] == generation:
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
        index += 1


def connect_cast():
    global CAST
    if CAST is None:
        CAST = pychromecast.get_chromecast_from_host((os.environ["CAST_HOST"], 8009,
                    UUID(os.environ["CAST_UUID"]), "Google Nest Hub Max", "Kitchen Display"),
                    tries=2, timeout=10)
        CAST.wait(timeout=20)
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
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
    if quit_app and CAST and CAST.media_controller.status.content_id == STREAM_URL:
        CAST.quit_app()


def start():
    global ENCODER
    with LOCK:
        if STATE["active"]:
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
    try:
        cast = connect_cast()
        deadline = time.monotonic() + 100
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
            raise TimeoutError("No playable camera stream was produced within 100 seconds")
    except Exception as error:
        STATE["error"] = str(error)
        logging.exception("Cannot start display")
        stop(False)


def monitor():
    misses = 0
    while True:
        time.sleep(3)
        if not CAST or not STATE["casting"]:
            misses = 0
            continue
        status = CAST.media_controller.status
        STATE["cast_state"] = status.player_state
        # Honor Hey Google stop, other casts, and the display's own controls.
        ours = status.content_id == STREAM_URL and status.player_state in ("PLAYING", "BUFFERING", "PAUSED")
        misses = 0 if ours else misses + 1
        if misses >= 5:
            logging.info("Playback stopped or another application took over")
            stop(False)


PAGE = """<!doctype html><html lang=en><meta name=viewport content="width=device-width, initial-scale=1">
<title>Ski cameras</title><style>body{background:#0b1821;color:#edf4f7;font:18px system-ui;margin:5vw;max-width:1000px}h1{font-size:38px}button{font:inherit;padding:14px 25px;margin:8px 12px 8px 0;border:1px solid #94aeba;border-radius:6px;background:#233d4a;color:white}p{line-height:1.6}video{width:100%}small{color:#b7c9d2}a{color:#c4e2ef}</style>
<h1>Ski cameras</h1><p>Hub Max · rotating live cameras</p>
<button onclick="control('start')">Start on Hub Max</button><button onclick="control('stop')">Stop</button>
<p id=status>Loading status…</p><p id=cameras></p>
<small>Resort camera feeds discovered through Ski Cams Live. Weather: Open-Meteo, modeled near each resort. No OnTheSnow graphics, QR codes, or advertising added.</small>
<script>async function control(action){await fetch('/'+action,{method:'POST'});update()}
async function update(){let s=await(await fetch('/status')).json();document.getElementById('status').textContent=(s.active?(s.casting?'Playing: ':'Starting: ')+(s.camera||'connecting'):'Stopped')+(s.error?' | '+s.error:'');document.getElementById('cameras').textContent=s.camera_count+' cameras in catalog · '+s.failed_cameras+' failed attempts'}update();setInterval(update,3000)</script></html>"""


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
        if self.path == "/":
            return self.reply(PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/status":
            state = dict(STATE, camera_count=len(CAMS), cooling_down=sum(t > time.time() for t in FAILED_UNTIL.values()))
            if CAST:
                s = CAST.media_controller.status
                state["receiver"] = {"player_state": s.player_state, "content_id": s.content_id,
                                     "idle_reason": s.idle_reason, "app_id": CAST.app_id}
            return self.reply(json.dumps(state).encode(), "application/json")
        if not (self.path == "/live.m3u8" or
                self.path.startswith("/segment") and self.path.endswith(".ts") and "/" not in self.path[1:]):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send_error(403)
        if self.path == "/start":
            threading.Thread(target=start, daemon=True).start()
        elif self.path == "/stop":
            stop()
        else:
            return self.send_error(404)
        self.reply(b'{"ok":true}', "application/json")

    def log_message(self, fmt, *args):
        if self.path in ("/start", "/stop", "/live.m3u8"):
            logging.info(self.client_address[0] + " " + fmt, *args)


if __name__ == "__main__":
    for target in (channel, monitor, refresh_catalog):
        threading.Thread(target=target, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8106), Handler).serve_forever()
