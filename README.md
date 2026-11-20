# Ski camera channel for a Nest Hub Max

This project makes a continuous live video channel from resort webcams and casts it to a Google Nest Hub Max. It rotates five camera feeds every 45 seconds. A small strip shows the resort, camera, nearby weather in °F and mph, the next view, and resort-local time. It adds no OnTheSnow branding, QR codes, or advertisements. Signs or logos already present in a source camera remain visible.

The channel runs in Docker on a LAN server. A browser or Mac does not need to remain open. It uses H.264 video at 1280 × 720 and 15 frames per second, plus silent AAC audio. A continuous encoder keeps timestamps stable when the camera changes so the Hub does not stop at a playlist discontinuity.

## Camera lineup

- Vail, mountain view
- Keystone, Bergman Bowl
- Breckenridge, Peak 8 from Peak 9
- Keystone, North Peak
- Heavenly, Top of Tram

The camera URLs in [cameras.json](cameras.json) were found through [Ski Cams Live](https://skicamslive.com/). They point to the camera provider's streams. Nearby weather uses [Open-Meteo](https://open-meteo.com/) and refreshes at most every 15 minutes. Source feeds can change or fail. A connection failure skips to the next camera. A still error picture inside a working video cannot be detected reliably.

## Install

Install Docker Compose, then copy `.env.example` to `.env`. Set `BIND_HOST` to the server's LAN IPv4 address and `CAST_HOST` to the Hub's LAN IPv4 address. Set `CAST_UUID` to the Hub's Cast UUID. The server and Hub must be able to reach each other on the LAN. Keep the Hub's DHCP address stable.

```sh
cp .env.example .env
# Edit .env with the actual addresses and UUID.
docker compose up -d --build
```

The control page is `http://<BIND_HOST>:8106/`. Use **Start on Hub Max** to play and **Stop** to end playback. The `/status` endpoint shows the current camera and receiver state. Initial buffering usually takes 15–25 seconds. Saying “Hey Google, stop” or starting other media also ends the session. The service stays available after playback stops.

For unattended service startup, set the container to autostart in Unraid. Compose also uses `restart: unless-stopped`. A ski playback session starts only when requested; no schedule is included.

## Manage

```sh
docker logs --tail 50 ski-cams
docker stats --no-stream ski-cams
docker compose up -d --build
docker compose down
```

Edit [cameras.json](cameras.json) to change the lineup. Change `ROTATE_SECONDS` in [compose.yaml](compose.yaml) to change the duration. Rebuild after either edit; rebuilding interrupts the current cast. Runtime HLS segments use a 96 MB RAM filesystem, and no camera video is archived. Docker limits the container to 2 CPU cores and 768 MB of memory.

The control page uses local HTTP without authentication. It binds to the server's LAN address. Do not expose port 8106 publicly. Browser cross-origin POST requests are rejected.

## Verification

On the original installation, the Hub Max reported `PLAYING` with the channel URL, and playback position advanced. Multiple camera changes completed while the Cast session stayed active. A 110-second local decode check passed. The physical Hub screen was not independently inspected.
