import os
import time

import cv2
import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt-bridge")
TOPIC = "ds/FaceDetected"
DEBOUNCE_FRAMES = 2  # consecutive agreeing frames required before flipping the published state
POLL_INTERVAL = 0.1

# Blynk's own upload API overwrites a previous upload of the same filename
# (see the edge-agent README's "File uploads" section) - a fixed name here
# is deliberate, not an oversight: this example only ever cares about the
# single latest snapshot, so overwriting is exactly the wanted behavior,
# not a limitation to work around.
SNAPSHOT_UPLOAD_TOPIC = "local/blynk/upload/face_snapshot.jpg"
UPLOAD_RESULT_TOPIC = "local/blynk/upload_result"
# The Image Gallery widget's URL lives on this property topic, not a plain
# ds/ publish - see docs.blynk.io's MQTT device API, "datastream
# properties" (ds/{DatastreamName}/prop/{property}, single value as plain
# text). Deliberately "urls" (plural), not "url" (singular) - confirmed on
# real hardware that "url" is silently accepted and stored (visible in the
# datastream's own property history) but is NOT what the widget actually
# renders from; "urls" is. "url" with an index is a real Gallery property
# in the firmware SDK (Blynk.setProperty(pin, "url", index, ...), for
# updating one slot in the list), but MQTT has no way to supply that index
# at all - only "url" (whole, no index) or "urls" (replace the whole list)
# are exposed over MQTT. For a single image, "urls" just takes that one
# URL as its own payload, no list/array encoding needed. Create a
# "FaceSnapshot" datastream and an Image Gallery widget bound to it in the
# Blynk console first - nothing here creates that automatically.
SNAPSHOT_PROPERTY_TOPIC = "ds/FaceSnapshot/prop/urls"
# However often a face flickers in and out of frame, an upload is a real
# HTTPS call proxied through the agent to Blynk Cloud - this puts a floor
# under how often that actually fires, independent of the frame-level
# debounce above.
UPLOAD_COOLDOWN = 30  # seconds
JPEG_QUALITY = 85

detector = cv2.FaceDetectorYN_create("face_detection_yunet_2023mar.onnx", "", (320, 320))
camera = cv2.VideoCapture(0)

ok, frame = camera.read()
if ok:
    detector.setInputSize((frame.shape[1], frame.shape[0]))


def on_message(client, userdata, message):
    payload = message.payload.decode("utf-8", errors="replace")
    if payload.startswith("[error]"):
        print(f"Snapshot upload failed: {payload}")
        return
    client.publish(SNAPSHOT_PROPERTY_TOPIC, payload, qos=1)
    print(f"Snapshot uploaded: {payload}")


client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
client.on_message = on_message
client.connect(MQTT_HOST, 1883)
client.subscribe(UPLOAD_RESULT_TOPIC, qos=1)
client.loop_start()

published = False
pending = False
pending_count = 0
last_upload_time = 0.0

while True:
    ok, frame = camera.read()
    if not ok:
        # A one-shot cv2.VideoCapture(0) at startup can lose a race for the
        # device (e.g. against another process briefly opening it) and never
        # recover on its own - retry opening it instead of reading forever
        # from a capture object that will never succeed.
        print("Camera read failed, retrying open()")
        camera.open(0)
        time.sleep(1)
        continue

    _, faces = detector.detect(frame)
    detected = faces is not None and len(faces) > 0

    if detected == pending:
        pending_count += 1
    else:
        pending, pending_count = detected, 1

    if pending_count >= DEBOUNCE_FRAMES and detected != published:
        published = detected
        client.publish(TOPIC, int(published))
        print(f"{TOPIC}: {int(published)}")

        if published and time.time() - last_upload_time >= UPLOAD_COOLDOWN:
            last_upload_time = time.time()
            ok_encode, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok_encode:
                client.publish(SNAPSHOT_UPLOAD_TOPIC, buf.tobytes(), qos=1)
                print(f"Uploading snapshot ({len(buf)} bytes)")

    time.sleep(POLL_INTERVAL)
