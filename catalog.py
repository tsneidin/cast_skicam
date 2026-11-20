"""Discover public Ski Cams Live entries and retain the last good catalog."""
import html
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

SITE = "https://skicamslive.com"
TRAFFIC = re.compile(r"(?:\btraffic\b|\broad\b|/road|roadcam|\bhighway\b|hwy|\binterstate\b|\bfreeway\b|\bstreets?\b|\bi[- ]?(?:70|80)\b|dot\.ca\.gov|cotrip)", re.I)


def get(path):
    request = Request(urljoin(SITE, path), headers={"User-Agent": "Mozilla/5.0 (compatible; SkiCameraChannel/1.0)"})
    with urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8", "replace")


def source(ident, iframe):
    if ident.startswith("image:"):
        return "image", ident[6:]
    if re.search(r"\.(?:jpe?g|png|webp)(?:\?|$)", ident, re.I):
        return "image", ident
    if ident.startswith(("http://", "https://")):
        return "video", ident
    if "youtube.com/embed/" in iframe:
        return "youtube", "https://www.youtube.com/watch?v=" + ident
    if iframe:
        return "embedded", urljoin(SITE, iframe)
    return "unknown", ident


def discover():
    home = get("/")
    cards = re.findall(r'<div class="card-wrapper".*?<a href="(/mountain/[^"?]+/)".*?</a>', home, re.S)
    paths = list(dict.fromkeys(cards))
    if len(paths) < 100:
        raise RuntimeError(f"Only {len(paths)} mountain pages found; keeping cached catalog")

    def mountain(path):
        page = get(path)
        title = re.search(r'<title>(.*?)</title>', page, re.S)
        label = html.unescape(title.group(1).split(" Webcams:")[0]) if title else path.strip("/").split("/")[-1]
        region = html.unescape(title.group(1).split(" | ")[-1]) if title and " | " in title.group(1) else ""
        blocks = re.findall(r'<div class="video-container"(.*?)(?=<div class="video-container"|</div>\s*</div>\s*<aside)', page, re.S)
        found = []
        for n, block in enumerate(blocks, 1):
            match = re.search(r'data-cam-id="([^"]+)"', block)
            if not match:
                continue
            ident = html.unescape(match.group(1))
            frame = re.search(r'<iframe[^>]*?src="([^"]+)"', block, re.S)
            iframe = html.unescape(frame.group(1)) if frame else ""
            kind, url = source(ident, iframe)
            if TRAFFIC.search(" ".join((label, ident, url))):
                continue
            found.append({"resort": label, "camera": f"Camera {n}", "region": region,
                          "kind": kind, "url": url, "page": urljoin(SITE, path)})
        return found

    cameras = []
    failures = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(mountain, path): path for path in paths}
        for future, path in futures.items():
            try:
                cameras.extend(future.result())
            except Exception as error:
                failures.append(path)
                logging.warning("Catalog page %s failed: %s", path, error)
    if failures or len(cameras) < 500:
        raise RuntimeError(f"Catalog incomplete: {len(cameras)} cameras, {len(failures)} page failures")
    # Start with a previously verified direct feed so initial Cast buffering is prompt.
    cameras.sort(key=lambda cam: 0 if "vailch11/vailch11.stream" in cam["url"] else 1)
    return cameras


def load(cache):
    cache = Path(cache)
    fallback = Path(__file__).with_name("cameras.json")
    try:
        cameras = discover()
        cache.parent.mkdir(parents=True, exist_ok=True)
        temp = cache.with_suffix(".tmp")
        temp.write_text(json.dumps(cameras, indent=2))
        temp.replace(cache)
        return cameras
    except Exception as error:
        logging.warning("Catalog refresh failed: %s", error)
        return json.loads((cache if cache.exists() else fallback).read_text())
