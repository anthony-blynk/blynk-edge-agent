"""Modbus RTU -> Blynk gateway, talking to the edge-agent's local MQTT
bridge (see ../../../README.md) instead of connecting to Blynk Cloud
directly - no credentials, template id or BLE pairing here at all, same as
every other example in this project; only mqtt-bridge ever holds a real
token. This is the only module that imports paho; bus.py is the only one
that imports pymodbus - so Gateway/Config/Codec stay testable without either.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

import paho.mqtt.client as mqtt

from . import GATEWAY_VERSION
from .bus import Bus
from .gateway import Gateway

logger = logging.getLogger("modbus_gateway")


class _LocalDownlinkMqtt:
    """Adapts Gateway's calls (written against this device's own real
    topics, same as the standalone proof-of-concept this was ported from)
    onto the edge-agent's actual local ones. Only subscribe/unsubscribe
    for downlink/ds/<name> need translating, onto
    local/blynk/downlink/ds/<name> - the edge-agent's anonymous-safe
    mirror of it (see ../../../README.md's "Testing the connection" and
    Security sections). downlink/ds/<name> itself is never delivered to
    an anonymous local subscriber like this one - only the agent's own
    credentialed connection can read it. Everything else (ds/<name>,
    get/ds, event/<code>) is already this device's real, anonymous local
    topic, unaffected."""

    LOCAL_PREFIX = "local/blynk/downlink/ds/"
    REAL_PREFIX = "downlink/ds/"

    def __init__(self, client):
        self._client = client

    def _localize(self, topic: str) -> str:
        return self.LOCAL_PREFIX + topic[len(self.REAL_PREFIX):] if topic.startswith(self.REAL_PREFIX) else topic

    def subscribe(self, topic, qos=0):
        return self._client.subscribe(self._localize(topic), qos=qos)

    def unsubscribe(self, topic):
        return self._client.unsubscribe(self._localize(topic))

    def publish(self, topic, payload=None, qos=0):
        return self._client.publish(topic, payload, qos=qos)

    def is_connected(self) -> bool:
        return self._client.is_connected()


def _delocalize(topic: str) -> str:
    """The reverse of _LocalDownlinkMqtt._localize, for an incoming message
    - Gateway.handle_message expects the real downlink/ds/<name> shape."""
    prefix = _LocalDownlinkMqtt.LOCAL_PREFIX
    return _LocalDownlinkMqtt.REAL_PREFIX + topic[len(prefix):] if topic.startswith(prefix) else topic


MQTT_HOST = os.getenv("MQTT_HOST", "mqtt-bridge")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
# Comma-separated for a multi-port gateway, in the order the config's
# "ports" refers to them (ports[0], ports[1], ...)
MODBUS_PORTS = [p.strip() for p in os.getenv("MODBUS_PORT", "/dev/ttyUSB0").split(",") if p.strip()]
STATE_DIR = os.getenv("MODBUS_STATE_DIR", "/var/lib/modbus-gateway")
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")


def _exit_on_thread_crash(args) -> None:
    """A poll thread dying (bus I/O bug, unexpected pymodbus exception) must
    take the whole process down so Docker restarts it, rather than leave a
    gateway that's silently stopped polling some or all of its ports."""
    if issubclass(args.exc_type, SystemExit):
        return
    logger.critical(f"Background thread {args.thread.name if args.thread else '?'} crashed - exiting",
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    os._exit(1)


def main() -> None:
    logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not DEBUG:
        logging.getLogger("pymodbus").setLevel(logging.CRITICAL)  # it logs every timeout itself
    threading.excepthook = _exit_on_thread_crash
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    os.makedirs(STATE_DIR, exist_ok=True)
    logger.info(f"modbus_gateway {GATEWAY_VERSION}, ports {', '.join(MODBUS_PORTS)}, "
                f"mqtt {MQTT_HOST}:{MQTT_PORT}")

    buses = [Bus(port) for port in MODBUS_PORTS]
    gateway = Gateway(buses, state_path=os.path.join(STATE_DIR, "modbus_config.json"))

    def on_connect(c, u, flags, reason_code, props):
        logger.info("MQTT connected - syncing config")
        gateway.on_connected()

    def on_disconnect(c, u, flags, reason_code, props):
        logger.warning(f"MQTT disconnected: {reason_code}")

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = lambda c, u, msg: gateway.handle_message(_delocalize(msg.topic), msg.payload)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    gateway.attach_mqtt(_LocalDownlinkMqtt(client))

    client.connect(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    try:
        # Port 0 polls on this (main) thread; any further port gets its own
        gateway.run_forever(threading.Event())
    finally:
        logger.info("Shutting down")
        client.disconnect()
        client.loop_stop()
        for bus in buses:
            bus.close()


if __name__ == "__main__":
    main()
