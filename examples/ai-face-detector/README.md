# AI Face Detector example

Watches a USB webcam, publishes `ds/FaceDetected` (0/1) whenever a face enters or leaves frame, and uploads a snapshot of the most recent detection to Blynk Cloud via the edge-agent's [file-upload proxy](../../README.md#file-uploads).

Uses OpenCV's YuNet DNN face detector (not the older Haar cascade) - noticeably more accurate and less flicker-prone frame to frame.

## Blynk console setup (one-time, manual)

Nothing here creates datastreams or widgets automatically - you need to:

1. Create a `FaceDetected` datastream (integer, 0/1) if you want the on/off state on a dashboard - bind it to a Switch/LED widget, or just watch it in the console.
2. Create a `FaceSnapshot` datastream (any type works - it's only used to carry the `urls` property, not read as a value itself) and add an **Image Gallery** widget bound to it (a plain Image widget won't pick up the update - see the note on `prop/urls` below). The agent publishes the snapshot's URL to `ds/FaceSnapshot/prop/urls` each time a new one is uploaded; with just one URL, the gallery simply shows that single image.

   Confirmed on real hardware: `ds/FaceSnapshot/prop/url` (singular) gets silently accepted and stored (visible in the datastream's own property history) but is **not** what the Image Gallery widget actually renders from - only `prop/urls` (plural) is. `url` with an index is a real Gallery property in Blynk's firmware SDK, for updating one slot in a list, but MQTT has no way to supply that index at all - only whole-value `url` or whole-list `urls` are exposed over MQTT.

## Enabling on a device

Published as a prebuilt image (`ghcr.io/anthony-blynk/ai-face-detector`) - nothing to build yourself. Uncomment the `ai-face-detector` service in the root [`docker-compose.yml`](../../docker-compose.yml) on the *one* device that actually has a camera, then `docker compose up -d` there directly.

**Don't push this uncommented via a fleet-wide OTA update.** Every device pulls the same `docker-compose.yml` on its next OTA - a device without `/dev/video0` would fail to start this service, which can fail the whole apply and trigger a rollback. This is a per-device, opt-in example, enabled by hand on that device's own deployed copy of the file, not something to commit uncommented into the tracked stack config.

Requires a USB webcam at `/dev/video0`. A Raspberry Pi Camera Module (CSI, libcamera-based) is a materially different capture path and isn't supported by this example as-is.

## Tuning

`DEBOUNCE_FRAMES`, `POLL_INTERVAL`, `UPLOAD_COOLDOWN`, and `JPEG_QUALITY` (see `camera_detect.py`) aren't runtime/env-configurable - changing them means editing the script, bumping `VERSION`, and pushing to master (which rebuilds and republishes the image via its own [workflow](../../.github/workflows/build-ai-face-detector.yml), independent of the core agent/mqtt-bridge release cycle).

- `DEBOUNCE_FRAMES` / `POLL_INTERVAL` - how many consecutive agreeing frames are required before flipping the published detection state, and how often frames are read.
- `UPLOAD_COOLDOWN` (default 30s) - a floor on how often a snapshot actually gets uploaded, independent of how often detection flickers - each upload is a real HTTPS call proxied through the agent to Blynk Cloud, not something to fire on every frame.
- `JPEG_QUALITY` (default 85) - encode quality; higher is larger files, comfortably under Blynk's 5MB/file upload cap either way for a webcam frame.

The uploaded filename (`face_snapshot.jpg`) is fixed, not timestamped - Blynk's upload API overwrites a previous upload of the same name, which is exactly what's wanted here since this example only ever shows the single latest snapshot, not a history.
