"""Shared fixtures for the on-device verification suite - must run on the
device itself (needs 127.0.0.1:1883 and the local config files below), not
in CI. See README.md in this directory for usage."""

import os
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest
import requests
from dotenv import dotenv_values

BLYNK_ENV = Path("/opt/blynk/blynk.env")
BROKER_CREDS_ENV = Path("/opt/blynk/mqtt-bridge/local_broker_creds.env")
BROKER_HOST = "127.0.0.1"
BROKER_PORT = 1883
CALLBACK_TIMEOUT = 5.0
# MQTT5 reason codes (see the MQTT v5 spec) - only the ones this suite
# actually asserts on.
RC_SUCCESS = 0
RC_NOT_AUTHORIZED = 135


def _rc_value(reason_code):
    # paho-mqtt 2.x's MQTT5 ReasonCode objects compare fine against a plain
    # int directly (`==` is implemented against .value), but not every paho
    # version's repr/behavior has been confirmed against this project's own
    # exact pin - normalize explicitly rather than assume.
    return getattr(reason_code, "value", reason_code)


@pytest.fixture(scope="session")
def blynk_config():
    values = dotenv_values(BLYNK_ENV)
    required = ("BLYNK_SERVER", "BLYNK_AUTH_TOKEN", "BLYNK_TEMPLATE_ID")
    missing = [k for k in required if not values.get(k)]
    if missing:
        pytest.skip(f"{BLYNK_ENV} is missing {missing} - is this device provisioned?")
    return values


@pytest.fixture(scope="session")
def broker_creds():
    values = dotenv_values(BROKER_CREDS_ENV)
    required = ("BRIDGE_LOCAL_USERNAME", "BRIDGE_LOCAL_PASSWORD", "AGENT_LOCAL_USERNAME", "AGENT_LOCAL_PASSWORD")
    missing = [k for k in required if not values.get(k)]
    if missing:
        pytest.skip(f"{BROKER_CREDS_ENV} is missing {missing} - has the stack started at least once?")
    return values


class MqttProbe:
    """A one-shot MQTT5 client for a single publish or subscribe, blocking
    until the broker's own ack (with its real reason code) arrives or a
    timeout passes - bridges paho's callback-driven API into a plain
    synchronous result a test can assert on directly. MQTT5 is used
    deliberately (not this project's own v3.1.1-default client code) so a
    denied publish/subscribe is visible as an actual reason code rather than
    an indistinguishable-from-success bare ack, which is all v3.1.1 gives a
    publishing client."""

    def __init__(self, username=None, password=None):
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
        if username:
            self.client.username_pw_set(username, password)
        self._connected = threading.Event()
        self._messages = []
        self._messages_lock = threading.Lock()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(BROKER_HOST, BROKER_PORT, keepalive=10)
        self.client.loop_start()
        if not self._connected.wait(CALLBACK_TIMEOUT):
            raise TimeoutError("Never connected to the local broker")

    def _on_message(self, client, userdata, message):
        with self._messages_lock:
            self._messages.append(message)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if _rc_value(reason_code) == RC_SUCCESS:
            self._connected.set()

    def publish_and_wait(self, topic, payload="1", qos=1):
        done = threading.Event()
        result = {}

        def on_publish(client, userdata, mid, reason_code, properties):
            result["reason_code"] = _rc_value(reason_code)
            done.set()

        self.client.on_publish = on_publish
        self.client.publish(topic, payload, qos=qos)
        if not done.wait(CALLBACK_TIMEOUT):
            raise TimeoutError(f"No PUBACK for {topic}")
        return result["reason_code"]

    def subscribe_and_wait(self, topic, qos=1):
        done = threading.Event()
        result = {}

        def on_subscribe(client, userdata, mid, reason_code_list, properties):
            result["reason_codes"] = [_rc_value(rc) for rc in reason_code_list]
            done.set()

        self.client.on_subscribe = on_subscribe
        self.client.subscribe(topic, qos=qos)
        if not done.wait(CALLBACK_TIMEOUT):
            raise TimeoutError(f"No SUBACK for {topic}")
        return result["reason_codes"]

    def wait_for_message(self, timeout=2.0):
        """Blocks up to `timeout` for any message to arrive on a topic this
        client has subscribed to; returns the message, or None if nothing
        arrived. None is the expected, passing outcome when testing that
        read access was actually denied - mosquitto typically grants the
        SUBACK for a wildcard subscription like `downlink/#` regardless of
        ACL (confirmed on real hardware: `subscribe_and_wait` alone reported
        success even for a client with no read grant), and instead filters
        which individual messages actually get delivered - so message
        delivery, not the SUBACK reason code, is the real signal for
        whether read access exists."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._messages_lock:
                if self._messages:
                    return self._messages.pop(0)
            time.sleep(0.05)
        return None

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def anon_client():
    probe = MqttProbe()
    yield probe
    probe.close()


@pytest.fixture
def bridge_client(broker_creds):
    probe = MqttProbe(broker_creds["BRIDGE_LOCAL_USERNAME"], broker_creds["BRIDGE_LOCAL_PASSWORD"])
    yield probe
    probe.close()


@pytest.fixture
def agent_client(broker_creds):
    probe = MqttProbe(broker_creds["AGENT_LOCAL_USERNAME"], broker_creds["AGENT_LOCAL_PASSWORD"])
    yield probe
    probe.close()


class DeviceAPI:
    """Thin wrapper over Blynk's Device HTTP(S) API - just enough to read a
    datastream's current value. Deliberately builds the URL by hand rather
    than via requests' query-param encoding: Blynk's `get` format is a bare
    key with no `=value` (e.g. `?token=...&V0`), not an ordinary key=value
    pair, and the pin/dataStreamId is the only identifier it accepts - the
    HTTP API has no by-name lookup the way the MQTT API's `ds/<name>` topics
    do (confirmed against Blynk's own docs)."""

    def __init__(self, server, token, pin):
        self.base = f"https://{server}/external/api"
        self.token = token
        self.pin = pin

    def get_raw(self):
        response = requests.get(f"{self.base}/get?token={self.token}&{self.pin}", timeout=10)
        if response.status_code != 200:
            # requests' own raise_for_status() drops the body, which is
            # exactly where Blynk puts the actual reason (e.g. "Wrong pin
            # format" vs "dataStream doesn't exist") - surface it instead
            # of a bare status code.
            raise RuntimeError(f"Device API GET failed ({response.status_code}): {response.text}")
        return response.text

    def get_value(self):
        # The exact successful response shape isn't documented anywhere
        # seen (only error bodies are) - tolerate a bare value, a quoted
        # string, or a single-element JSON array, rather than assume one.
        return self.get_raw().strip().strip("[]").strip('"')


# Fixed convention for this suite's own scratch datastream, the same way
# the top-level README fixes exact types for the Agent* datastreams rather
# than discovering them - not something end users are meant to customize
# per template. The HTTP Device API has no by-name lookup and the account-
# level API that *does* return a template's name->pin mapping
# (GET /api/v1/organization/template/datastreams) needs a Bearer/JWT
# account login, not the device's own token - putting that on a device
# would be a real security downgrade versus this project's "devices only
# ever hold their own per-device token" design, just to avoid a fixed pin
# convention. Override via env var only if a specific device's template
# genuinely can't use this pin.
DEFAULT_TEST_DATASTREAM_NAME = "AgentSelfTest"
DEFAULT_TEST_DATASTREAM_PIN = "V100"


@pytest.fixture(scope="session")
def device_api(blynk_config):
    pin = os.environ.get("TEST_DATASTREAM_PIN", DEFAULT_TEST_DATASTREAM_PIN)
    return DeviceAPI(blynk_config["BLYNK_SERVER"], blynk_config["BLYNK_AUTH_TOKEN"], pin)
