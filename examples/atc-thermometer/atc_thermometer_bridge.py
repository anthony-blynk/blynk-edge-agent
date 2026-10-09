"""
ATC/pvvx BLE thermometer bridge - passively scans for Xiaomi Mijia
LYWSD03MMC/similar sensors flashed with the atc_MiThermometer custom
firmware (https://github.com/pvvx/ATC_MiThermometer), decodes their
broadcast temperature/humidity/battery readings directly from BLE
advertisements (no pairing/connecting needed), and publishes each sensor as
its own, independent Blynk device through the edge-agent's MQTT Gateway
feature (see ../../README.md#mqtt-gateway-sub-devices) - no Blynk
credentials needed here at all, same as every other local app in this
project; only mqtt-bridge/the agent ever hold a real token.

Each sensor's own MAC address becomes its sub-device registry name
(atc-<mac, no colons>) - fully automatic via the gateway's auto-discovery,
no per-sensor configuration needed at all; just replace the placeholder
token with that sensor's own real Blynk device token once it shows up in
AgentSubDeviceRegistry.

Supports both known advertisement layouts (the original 13-byte "atc1441"
format and the newer 14-byte "pvvx" format) - which one a given sensor
actually uses is determined by payload length alone. Only publishes a
reading when the sensor's own packet counter changes, not on every
advertisement received - these broadcast far more often than their actual
reading interval, and the counter is a free, built-in "is this a new
reading" signal.
"""

import asyncio
import logging
import os

from bleak import BleakScanner
import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

MQTT_HOST = os.getenv("MQTT_HOST", "mqtt-bridge")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

ATC_SERVICE_UUID = "0000181a-0000-1000-8000-00805f9b34fb"  # Environmental Sensing


def _decode_atc1441(data: bytes):
    """mac(6) + temp_i16_be_0.1C(2) + humidity_u8_pct(1) + batt_pct_u8(1)
    + batt_mv_u16_be(2) + counter_u8(1) = 13 bytes. Confirmed on real
    hardware (2026-10) against a pvvx-firmware-flashed sensor that was
    still actually broadcasting in this original, simpler layout - the
    firmware name alone doesn't tell you which format is in use."""
    if len(data) != 13:
        return None
    return {
        "temp_c": int.from_bytes(data[6:8], "big", signed=True) / 10.0,
        "humidity_pct": float(data[8]),
        "battery_pct": data[9],
        "counter": data[12],
    }


def _decode_pvvx(data: bytes):
    """mac(6) + temp_i16_le_0.01C(2) + humidity_u16_le_0.01pct(2)
    + batt_mv_u16_le(2) + batt_pct_u8(1) + counter_u8(1) = 14 bytes - the
    newer, higher-precision layout pvvx's firmware can also be configured
    to use instead of the original atc1441 one above. Not yet confirmed
    against a real sensor actually using this layout - only the 13-byte
    one has been seen on real hardware so far."""
    if len(data) != 14:
        return None
    return {
        "temp_c": int.from_bytes(data[6:8], "little", signed=True) / 100.0,
        "humidity_pct": int.from_bytes(data[8:10], "little") / 100.0,
        "battery_pct": data[12],
        "counter": data[13],
    }


def _decode(data: bytes):
    return _decode_pvvx(data) or _decode_atc1441(data)


def _sub_device_name(mac: str) -> str:
    return "atc-" + mac.replace(":", "").lower()


class AtcThermometerBridge:
    def __init__(self, client: mqtt.Client):
        self.client = client
        self._last_counter: dict = {}  # mac -> last published reading's counter

    def on_detect(self, device, advertisement_data) -> None:
        service_data = advertisement_data.service_data.get(ATC_SERVICE_UUID)
        if service_data is None:
            return
        reading = _decode(service_data)
        if reading is None:
            logger.warning(
                f"Unrecognized ATC payload length {len(service_data)} from {device.address} - ignored"
            )
            return

        mac = device.address
        if self._last_counter.get(mac) == reading["counter"]:
            return  # same reading retransmitted, not a new one
        self._last_counter[mac] = reading["counter"]

        name = _sub_device_name(mac)
        logger.info(
            f"{name}: {reading['temp_c']:.1f}C, {reading['humidity_pct']:.1f}%, "
            f"{reading['battery_pct']}% battery"
        )
        self.client.publish(f"sub/{name}/ds/Temperature", f"{reading['temp_c']:.1f}", qos=1)
        self.client.publish(f"sub/{name}/ds/Humidity", f"{reading['humidity_pct']:.1f}", qos=1)
        self.client.publish(f"sub/{name}/ds/Battery", str(reading["battery_pct"]), qos=1)


async def main() -> None:
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()

    bridge = AtcThermometerBridge(client)
    scanner = BleakScanner(detection_callback=bridge.on_detect)
    logger.info("Scanning for ATC thermometers...")
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await scanner.stop()


if __name__ == "__main__":
    asyncio.run(main())
