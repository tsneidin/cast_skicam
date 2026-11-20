# Ski camera channel for a Nest Hub Max

This Docker service casts a continuous H.264 channel to a Google Nest Hub Max. It rotates the public camera catalog on [Ski Cams Live](https://skicamslive.com/) every 45 seconds. The initial catalog contains 830 entries from 187 mountain pages after five obvious traffic cameras are excluded. The catalog includes the site's non-ski sections because they are part of its camera listing. The service refreshes the catalog daily and keeps the last good copy in `data/cameras.json` if a refresh fails.

Direct video and HLS, YouTube live, still images, IPCamLive, Brownrice, and some other embedded feeds are attempted. A source that cannot produce video frames is skipped and retried after one hour. Some embedded providers cannot be resolved outside their browser player and will be skipped. The program cannot reliably identify a video that plays a still error graphic. Traffic filtering uses names and URLs, so a traffic feed without a descriptive name or URL may slip through. The `/status` endpoint reports catalog size, failed attempts, and feeds cooling down. Catalog size is not a count of currently working feeds.

The channel adds only its own resort/camera footer. It does not add OnTheSnow branding, QR codes, or ads. Branding baked into a source feed can still appear. Most newly discovered cameras do not have coordinate metadata, so the footer shows `Live camera` instead of weather for those feeds.

## Install

Install Docker Compose, then copy `.env.example` to `.env`. Set `BIND_HOST` to the server's LAN IPv4 address, `CAST_HOST` to the Hub's LAN IPv4 address, and `CAST_UUID` to its Cast UUID. Keep the Hub's DHCP address stable.

```sh
cp .env.example .env
# Edit .env with the actual addresses and UUID.
docker compose up -d --build
```

The control page is `http://<BIND_HOST>:8106/`. Use **Start on Hub Max** and **Stop**. Playback starts only when requested. The browser does not need to stay open. The service uses a continuous 1280 × 720, 15 fps H.264 stream with silent AAC audio so camera changes do not interrupt Cast playback.

For an existing Unraid installation at `/mnt/user/appdata/ski-cams`, update the source while preserving its private Compose configuration:

```sh
git clone https://github.com/tsneidin/cast_skicam.git /tmp/cast_skicam_update
bash /tmp/cast_skicam_update/deploy-unraid.sh
```

The script backs up the prior application source under the installation directory, copies the new code, and rebuilds the container. Rebuilding interrupts any current cast. It does not start playback; use the control page afterward.

## Manage

```sh
docker logs --tail 50 ski-cams
docker stats --no-stream ski-cams
curl http://<BIND_HOST>:8106/status
docker compose up -d --build
docker compose down
```

Change `ROTATE_SECONDS` in `compose.yaml` to alter the display time. The bundled `cameras.json` is a fallback snapshot. On startup, the service fetches the site catalog and writes it to `data/cameras.json`; edits to that cached file are replaced at the next refresh. Rebuilding interrupts an active Cast session. The runtime HLS segments use a 96 MB RAM filesystem, and no video is archived. The container is limited to 2 CPU cores and 768 MB of memory.

The control page uses local HTTP without authentication. Bind it to a LAN address and do not expose port 8106 publicly. Browser cross-origin POST requests are rejected.

## Verification

The initial catalog was fetched from all 187 public mountain pages on November 20, 2026. A YouTube live example resolved to an HLS stream; IPCamLive and Brownrice stream metadata were confirmed. Source availability changes and is checked during playback. The original five-camera version was observed casting and rotating on the Hub Max. The expanded catalog still needs a live Hub playback check after deployment.
