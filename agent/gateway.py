"""
Blynk MQTT Gateway API support (sub-devices) - lets this device act as a
gateway for other, separate local devices (e.g. wireless sensors with no
direct internet connection of their own), each appearing in Blynk Cloud as
its own independent device. See docs.blynk.io's MQTT Gateway API and this
project's own README ("MQTT Gateway" section) for the full picture.

The registry of sub-devices (name -> real auth token) lives in a Blynk
datastream (AgentSubDeviceRegistry, String type, JSON {"name": "token", ...})
on this device itself - not a local file, and not Blynk metadata (a Table
metadata field was tried first; real-hardware testing found Table fields are
defined at the template level and shared across every device using it,
unusable for a per-gateway list). A regular datastream reuses the exact same
ds/#, get/ds, downlink/ds/# mechanism already proven throughout this project
(AgentDiagnosticsEnabled, AgentTerminalEnabled), rather than a rarely-used
specialized field type - and lets a sub-device get auto-registered (with a
placeholder token) the moment it's plugged in, showing up as "needs
configuring" wherever the registry is viewed, rather than needing to already
know every sub-device's name in advance.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Same capability-gate shape as Terminal (docker-compose.yml/OTA-only, never
# settable from Blynk itself) - see docs.blynk.io's MQTT Gateway API.
# Requires a Blynk Enterprise org and the template's own "Enable for Gateway
# API" console setting - separate, Blynk-side prerequisites this flag alone
# can't satisfy.
GATEWAY_CAPABILITY_ENABLED = os.getenv("AGENT_GATEWAY_ENABLED", "false").lower() == "true"

# Local topic namespace for a registered gateway sub-device - mirrors the
# project's own ds/#, downlink/# etc. one level down, e.g.
# sub/pump3-vibration/ds/Vibration. Never bridged directly; mosquitto's
# bridge rewrites it to/from dev/{token}/... for the specific registered
# device - see build_gateway_connection_block.
SUB_DEVICE_TOPIC_PREFIX = "sub/"

REGISTRY_DATASTREAM = "AgentSubDeviceRegistry"
TOPIC_SUB_DEVICE_REGISTRY = f"downlink/ds/{REGISTRY_DATASTREAM}"

# Blynk Cloud's own default datastream String-type size cap, confirmed via
# its HTTP API ("Max length is 1024 symbols") - the JSON registry has to
# fit in this, capping how many sub-devices one gateway can realistically
# register (roughly 15-20 depending on name/token length). Confirmed with
# Blynk's own server developers that this is server-configurable, not a
# hard platform-wide constant - a self-hosted Enterprise server can raise
# it, so this is an env var (defaulting to the public Blynk Cloud value)
# rather than a hardcoded one, to avoid needlessly capping a server that's
# actually configured for more.
MAX_REGISTRY_JSON_LENGTH = int(os.getenv("AGENT_SUB_DEVICE_REGISTRY_MAX_LENGTH", "1024"))

# Placeholder token for an auto-discovered sub-device nobody's configured
# yet - deliberately not a plausible real token, so it's obviously a
# "needs configuring" marker wherever it's viewed (e.g. a web dashboard
# tab), and so _render_sub_device_bridge_topics below can reliably skip it
# rather than trying to wire up a nonsensical dev/???/... remap.
PLACEHOLDER_TOKEN = "???"


class SubDeviceRegistry:
    """In-memory, agent-owned copy of the AgentSubDeviceRegistry datastream's
    value - the single source of truth MqttBridge renders from, kept in sync
    by two independent writers: a console-originated edit arriving via
    downlink (load_from_payload) and this device auto-discovering a new,
    not-yet-configured sub-device (register_if_unknown). Starts empty; the
    real value arrives asynchronously after BlynkAgent requests it with
    get/ds at connect - any sub-device traffic sent before that response is
    simply not routed yet, the same bootstrap window the old metadata-based
    design had, now lower-stakes since gateway traffic lives on its own
    bridge connection (see build_gateway_connection_block)."""

    def __init__(self):
        self.devices: dict = {}

    def load_from_payload(self, payload: str) -> bool:
        """Replaces the registry wholesale from a downlink value (console-
        edited, or this device's own prior publish echoed back). Malformed
        payloads just mean "nothing usable here" rather than a crash - this
        is ultimately admin-edited data, not something to be strict about.
        Returns whether the registry actually changed.

        An empty payload is deliberately treated the same way (left alone,
        not applied as "cloud says: empty registry") - this datastream
        having never been set at all (a brand new device's very first
        get/ds round-trip) looks identical to a genuinely empty one on the
        wire, and this device's own in-memory registry may already be
        non-empty by the time that response arrives (e.g. a sub-device
        auto-registered itself - see register_if_unknown - while the
        round-trip was still in flight). Applying an empty value here would
        silently wipe that back out. A deliberate "clear everything" still
        works fine - just publish the literal two-character "{}", which
        isn't an empty payload."""
        if not payload.strip():
            return False
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            logger.warning(f"Could not parse {REGISTRY_DATASTREAM} payload: {e}")
            return False
        if not isinstance(data, dict):
            logger.warning(f"{REGISTRY_DATASTREAM} must contain a JSON object of name:token pairs")
            return False
        devices = {
            str(name).strip(): str(token).strip()
            for name, token in data.items()
            if str(name).strip() and str(token).strip()
        }
        changed = devices != self.devices
        self.devices = devices
        return changed

    def to_json(self) -> str:
        return json.dumps(self.devices)

    def register_if_unknown(self, name: str) -> bool:
        """Auto-discovery: called whenever local traffic is seen under a
        sub-device name not currently in the registry. Adds it with
        PLACEHOLDER_TOKEN so it shows up in the registry (and whatever web
        dashboard tab views it) as "needs configuring", without ever wiring
        up real cloud routing for it - see _render_sub_device_bridge_topics'
        own placeholder skip. Refuses (logs a warning, no-op) rather than
        silently overflowing Blynk's own 1024-char datastream limit."""
        if name in self.devices:
            return False
        candidate = dict(self.devices, **{name: PLACEHOLDER_TOKEN})
        candidate_json = json.dumps(candidate)
        if len(candidate_json) > MAX_REGISTRY_JSON_LENGTH:
            logger.warning(
                f"Cannot auto-register sub-device '{name}' - {REGISTRY_DATASTREAM} "
                f"would exceed Blynk's {MAX_REGISTRY_JSON_LENGTH}-char datastream limit"
            )
            return False
        self.devices = candidate
        logger.info(f"Auto-registered new sub-device '{name}' pending configuration (placeholder token)")
        return True


def extract_sub_device_name(topic: str) -> Optional[str]:
    """topic is e.g. "sub/pump3-vibration/ds/Vibration" - returns
    "pump3-vibration", or None if topic doesn't actually start with
    SUB_DEVICE_TOPIC_PREFIX or has no name segment."""
    if not topic.startswith(SUB_DEVICE_TOPIC_PREFIX):
        return None
    rest = topic[len(SUB_DEVICE_TOPIC_PREFIX):]
    name = rest.split("/", 1)[0]
    return name or None


# A second, separate bridge connection for gateway traffic - NOT merged into
# the primary identity's own BRIDGE_TEMPLATE/"connection blynk-cloud" block.
# Confirmed on real hardware that mosquitto's bridge duplicate-topic check
# only compares local_prefix/remote_prefix when BOTH sides are non-NULL;
# blynk-cloud's own bare topics (e.g. "topic ds/# out 1", no prefix - NULL)
# silently "matched" a sub-device's prefixed entry for the same topic/
# direction/qos ("topic ds/# out 1 sub/<name>/ dev/<token>/") and got
# skipped as a duplicate, meaning gateway topics never actually registered
# despite a correctly-rendered file. Keeping gateway topics on their own
# connection means every entry on it always has both prefixes set, so that
# comparison works correctly - and as a bonus, a bad mgmt_device auth now
# only takes down gateway traffic, not blynk-cloud's own connection too.
GATEWAY_CONNECTION_TEMPLATE = """\
connection blynk-gateway
address {server}:8883
bridge_protocol_version mqttv50
remote_username mgmt_device
remote_password {token}
remote_clientid blynk-bridge-gateway-{template_id}
bridge_cafile /etc/ssl/certs/ca-certificates.crt
cleansession true
try_private false
local_username {local_username}
local_password {local_password}

{sub_device_topics}
"""


def _render_sub_device_bridge_topics(sub_devices: dict) -> str:
    """Per-device topic remap lines appended to GATEWAY_CONNECTION_TEMPLATE -
    same topic set as the primary identity's own block in agent.py's
    BRIDGE_TEMPLATE, rewritten from local sub/<name>/ to remote dev/<token>/
    via mosquitto's local-prefix/remote-prefix bridge syntax (see
    docs.blynk.io's MQTT Gateway API) so each registered device appears in
    Blynk Cloud as its own, independent device rather than sharing this
    one's identity. Entries still carrying PLACEHOLDER_TOKEN (auto-
    discovered, not yet configured) are already filtered out by the caller -
    see build_gateway_connection_block."""
    lines = []
    for name, token in sub_devices.items():
        local_prefix = f"{SUB_DEVICE_TOPIC_PREFIX}{name}/"
        remote_prefix = f"dev/{token}/"
        lines.append(f"topic downlink/# in 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic ds/# out 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic batch_ds out 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic info/mcu out 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic event/# out 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic get/# out 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic meta/# out 1 {local_prefix} {remote_prefix}")
    return "\n".join(lines) + "\n" if lines else ""


def build_gateway_connection_block(sub_devices: dict, server: str, token: str, template_id: str,
                                    local_username: str, local_password: str) -> str:
    """Returns the second "connection blynk-gateway" block to append to
    BRIDGE_TEMPLATE - empty string when there's nothing to actually route
    (no configured sub-devices yet, or every discovered one is still an
    unconfigured placeholder), so a device that's never used gateway mode
    renders byte-identical to a plain, non-gateway device."""
    routable = {name: tok for name, tok in sub_devices.items() if tok != PLACEHOLDER_TOKEN}
    if not routable:
        return ""
    return GATEWAY_CONNECTION_TEMPLATE.format(
        server=server,
        token=token,
        template_id=template_id,
        sub_device_topics=_render_sub_device_bridge_topics(routable),
        local_username=local_username,
        local_password=local_password,
    )


# Static - deliberately NOT generated per-registered-device the way the
# bridge topics above still have to be. Two reasons: mosquitto's bridge
# duplicate-topic quirk (see GATEWAY_CONNECTION_TEMPLATE's own comment)
# doesn't apply to plain ACL rules at all, so a wildcard `sub/+/...` rule
# here is simpler without any downside - and critically, auto-discovery
# *requires* it: an unconfigured sub-device's very first publish needs to
# actually be allowed through to this device's own subscription before the
# agent can ever notice and register it, which a per-registered-name-only
# ACL would have denied outright before the entry existed to register it.
SUB_DEVICE_ACL_BLOCK = """\
topic readwrite sub/+/ds/#
topic readwrite sub/+/batch_ds
topic readwrite sub/+/info/mcu
topic readwrite sub/+/event/#
topic readwrite sub/+/get/#
topic readwrite sub/+/meta/#
topic read sub/+/downlink/#
"""

# The bridge's own local-side connection is the only thing that republishes
# a genuine cloud-originated downlink command locally - same asymmetric
# reasoning as the project's own downlink/# split.
SUB_DEVICE_BRIDGE_ACL_LINE = "topic write sub/+/downlink/#\n"

# The agent's own local identity needs read access to the whole sub-device
# namespace for auto-discovery (BlynkAgent._on_connect subscribes to
# "sub/+/#") - it never needs write access here, it only ever writes to its
# own ds/AgentSubDeviceRegistry.
SUB_DEVICE_AGENT_ACL_LINE = "topic read sub/+/#\n"
