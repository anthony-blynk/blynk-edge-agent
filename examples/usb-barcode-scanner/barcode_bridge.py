"""
USB barcode scanner bridge - reads a keyboard-wedge barcode scanner directly
via evdev (bypassing whatever has keyboard focus) and publishes each scan to
this registered sub-device's own namespace via the edge-agent's MQTT Gateway
feature (see ../../README.md#mqtt-gateway-sub-devices) - no Blynk
credentials needed here at all, same as every other local app in this
project; only mqtt-bridge/the agent ever hold a real token.

Numeric-only barcodes (UPC/EAN and similar) - a scan containing letters logs
a warning per unmapped key and is otherwise ignored, it's not a supported
format for this version.
"""

import logging
import os
import time

import evdev
from evdev import ecodes
import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MQTT_HOST = os.getenv("MQTT_HOST", "mqtt-bridge")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
# No sane default across different scanner models/serials - find yours with
# `ls -l /dev/input/by-id/` on the host (see README).
SCANNER_DEVICE_PATH = os.getenv("SCANNER_DEVICE_PATH")
# Must match this scanner's own entry (key) in the gateway's
# AgentSubDeviceRegistry datastream on this device - or, if it's not there
# yet, this is the name it'll auto-register itself under (with a
# placeholder token pending configuration) - see README.
SUB_DEVICE_NAME = os.getenv("SUB_DEVICE_NAME")
DATASTREAM_NAME = os.getenv("DATASTREAM_NAME", "BarcodeScanned")

DEVICE_RETRY_SECONDS = 5

DIGIT_KEYS = {
    ecodes.KEY_0: "0", ecodes.KEY_1: "1", ecodes.KEY_2: "2", ecodes.KEY_3: "3",
    ecodes.KEY_4: "4", ecodes.KEY_5: "5", ecodes.KEY_6: "6", ecodes.KEY_7: "7",
    ecodes.KEY_8: "8", ecodes.KEY_9: "9",
}


def _open_scanner() -> evdev.InputDevice:
    """Blocks until the scanner's device node exists and can be grabbed
    exclusively - covers both the container starting before the scanner is
    plugged in, and the scanner being unplugged/replugged later. Grabbing it
    (EVIOCGRAB) is what stops its keystrokes reaching anything else on the
    host - without this, every scan would "type" into whatever has focus."""
    while True:
        try:
            dev = evdev.InputDevice(SCANNER_DEVICE_PATH)
            dev.grab()
            logger.info(f"Grabbed scanner: {dev.name} ({dev.path})")
            return dev
        except (FileNotFoundError, OSError) as e:
            logger.warning(f"Scanner not available yet ({e}), retrying in {DEVICE_RETRY_SECONDS}s")
            time.sleep(DEVICE_RETRY_SECONDS)


def main() -> None:
    if not SCANNER_DEVICE_PATH:
        raise SystemExit("SCANNER_DEVICE_PATH is required - see README")
    if not SUB_DEVICE_NAME:
        raise SystemExit("SUB_DEVICE_NAME is required - see README")

    topic = f"sub/{SUB_DEVICE_NAME}/ds/{DATASTREAM_NAME}"
    logger.info(f"Publishing scans to {topic} via {MQTT_HOST}:{MQTT_PORT}")

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()

    buffer = ""
    while True:
        dev = _open_scanner()
        try:
            for event in dev.read_loop():
                if event.type != ecodes.EV_KEY or event.value != 1:  # key down only
                    continue
                if event.code == ecodes.KEY_ENTER:
                    if buffer:
                        logger.info(f"Scanned: {buffer}")
                        client.publish(topic, buffer, qos=1)
                        buffer = ""
                elif event.code in DIGIT_KEYS:
                    buffer += DIGIT_KEYS[event.code]
                else:
                    logger.warning(
                        f"Unmapped key code {event.code} ({ecodes.KEY.get(event.code)}) - "
                        "ignored (numeric-only barcodes supported in this version)"
                    )
        except OSError as e:
            # The scanner was unplugged mid-scan - discard whatever partial
            # buffer we had rather than risk stitching it to a later,
            # unrelated scan once the device comes back.
            logger.warning(f"Lost scanner ({e}), waiting for it to come back")
            buffer = ""
            time.sleep(DEVICE_RETRY_SECONDS)


if __name__ == "__main__":
    main()
