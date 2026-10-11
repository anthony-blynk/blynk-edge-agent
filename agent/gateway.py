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

A registry value can also be "<token>|<role>" - several local sub-device
names sharing one token (e.g. a motor sensor and a bearing sensor on one
physical asset/Blynk device), each with its own datastream-name prefix.
Unroled entries are still bridged directly by mosquitto, same as always;
roled entries go through the agent's own gw/ namespace instead, since
mosquitto can only rewrite topic-string prefixes, not insert a role prefix
into a datastream name or edit batch_ds's JSON body. See
build_roled_routing, _render_roled_gateway_bridge_topics and agent.py's
_handle_sub_device_traffic/_handle_gw_downlink for the full routing story.
"""

import json
import logging
import os
import re
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

# Internal-only namespace for roled sub-devices sharing one Blynk device -
# never touched by anything but the agent itself and the bridge's local
# side (see SUB_DEVICE_ACL_BLOCK's sibling constants below). "gw" for
# "gateway" - short like sub/, and free: every other prefix in this
# project (ds/, sub/, dev/, downlink/, local/, event/, meta/, get/) is
# already spoken for. Unlike sub/<name>/, this is keyed by TOKEN, not by
# local name - see build_roled_routing's own docstring for why.
GW_TOPIC_PREFIX = "gw/"

# A registry value is either "<token>" (unroled, unchanged behaviour) or
# "<token>|<role>" - several local sub-device names can then share one
# Blynk device/token, each owning a distinct datastream-name prefix
# "<role>_" (see build_roled_routing). Role characters are restricted to
# this - anything else is logged and the entry is skipped (not routed),
# rather than failing the whole registry load.
ROLE_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")

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


def extract_sub_device_rest(topic: str) -> Optional[str]:
    """topic is e.g. "sub/pump3-vibration/ds/Vibration" - returns
    "ds/Vibration" (everything after the name segment), or None if there's
    no name segment at all, or nothing after it (a bare "sub/<name>")."""
    name = extract_sub_device_name(topic)
    if name is None:
        return None
    rest = topic[len(SUB_DEVICE_TOPIC_PREFIX) + len(name):]
    return rest[1:] if rest.startswith("/") else None


def extract_gw_token_and_rest(topic: str) -> Optional[tuple]:
    """topic is e.g. "gw/TOKEN123/downlink/ds/motor_Vibration" - returns
    ("TOKEN123", "downlink/ds/motor_Vibration"), or None if topic doesn't
    start with GW_TOPIC_PREFIX or has no segment after the token."""
    if not topic.startswith(GW_TOPIC_PREFIX):
        return None
    rest = topic[len(GW_TOPIC_PREFIX):]
    token, sep, tail = rest.partition("/")
    return (token, tail) if token and sep else None


def parse_entry(value: str) -> tuple:
    """Splits a registry value into (token, role). "TOKEN" or "TOKEN|"
    both mean no role (empty string) - same as before roles existed;
    "TOKEN|role" means role. Doesn't validate the role's characters -
    that's build_roled_routing's job, since what to do with an invalid
    one depends on context (logging, skipping) that this low-level parse
    shouldn't need to know about. Used everywhere a token is read from
    the registry, so PLACEHOLDER_TOKEN/real-token comparisons are always
    against parse_entry(value)[0], never the raw value."""
    token, sep, role = value.partition("|")
    return token.strip(), (role.strip() if sep else "")


def _is_valid_role(role: str) -> bool:
    return bool(ROLE_PATTERN.match(role))


class RoledRouting:
    """Derived view over SubDeviceRegistry.devices for roled (TOKEN|role)
    entries only - see build_roled_routing. Computed fresh each time it's
    needed rather than cached on the registry: lookups are cheap plain
    dict operations against at most ~15-20 entries (the 1024-char registry
    cap keeps the ceiling low), and recomputing avoids a separate cache to
    keep in sync as the registry changes."""

    def __init__(self):
        self.by_name: dict = {}        # local name -> (token, role)
        self.by_token: dict = {}       # token -> {role: local name}
        self.excluded_names: set = set()  # see build_roled_routing

    def tokens(self) -> set:
        return set(self.by_token)

    def names_for_token(self, token: str) -> list:
        return list(self.by_token.get(token, {}).values())

    def resolve_ds_downlink(self, token: str, ds_name: str) -> Optional[tuple]:
        """For a downlink ds name arriving on gw/<token>/downlink/ds/<ds_name>,
        returns (local_name, stripped_ds_name) for the one sub-device whose
        role is the longest matching prefix - "bearing_de_temperature"
        matches role "bearing_de" over "bearing" if both exist on this
        token. None means ds_name doesn't belong to any role on this token
        (an asset-level/unprefixed downlink) - the caller fans that out to
        every name on the token via names_for_token instead."""
        roles = self.by_token.get(token, {})
        for role in sorted(roles, key=len, reverse=True):
            prefix = f"{role}_"
            if ds_name.startswith(prefix):
                return roles[role], ds_name[len(prefix):]
        return None


def build_roled_routing(devices: dict) -> RoledRouting:
    """Groups devices.items() by token (skipping placeholders) and decides
    which roled entries are actually routable. Two distinct conflict
    shapes, both logged and skipped rather than routed ambiguously:

    - Several names share a token but at least one has no role at all -
      ambiguous which one (if any) should own the token's own unprefixed
      identity, so none of them are routed.
    - Several names share a token and two or more have the *same* role -
      only the specific colliding entries are dropped; a third, distinct
      role on the same token still routes fine.

    A single name alone on a token with a valid role routes normally -
    nothing to conflict with yet. A single name alone with no role isn't
    part of this at all; that's just today's plain unroled entry, handled
    by the existing sub/<name>/ bridge path, not here.

    routing.excluded_names (used by build_gateway_connection_block to keep
    the plain unroled path from rendering a name this function rejected)
    covers every name that ever had a role, plus every name that merely
    shared a token with one - even an otherwise-plain "no role" sibling
    loses its normal unroled routing in that case, since letting it quietly
    keep the bare dev/<token> address while its roled sibling is rejected
    would still be routing that token ambiguously, just via a different
    code path."""
    by_token: dict = {}
    for name, value in devices.items():
        token, role = parse_entry(value)
        if token == PLACEHOLDER_TOKEN:
            continue
        by_token.setdefault(token, []).append((name, role))

    routing = RoledRouting()
    for token, entries in by_token.items():
        if len(entries) == 1:
            name, role = entries[0]
            if not role:
                continue
            routing.excluded_names.add(name)
            if not _is_valid_role(role):
                logger.warning(
                    f"Sub-device '{name}' has an invalid role '{role}' "
                    f"(expected [A-Za-z0-9_]+) - not routed"
                )
                continue
            routing.by_name[name] = (token, role)
            routing.by_token.setdefault(token, {})[role] = name
            continue

        for name, _ in entries:
            routing.excluded_names.add(name)

        unroled = sorted(name for name, role in entries if not role)
        if unroled:
            logger.warning(
                f"Sub-devices {', '.join(sorted(n for n, _ in entries))} share a token but "
                f"{', '.join(unroled)} have no role - none of them routed"
            )
            continue

        invalid = [(name, role) for name, role in entries if not _is_valid_role(role)]
        for name, role in invalid:
            logger.warning(
                f"Sub-device '{name}' has an invalid role '{role}' "
                f"(expected [A-Za-z0-9_]+) - not routed"
            )
        valid = [(name, role) for name, role in entries if _is_valid_role(role)]

        roles_seen: dict = {}
        conflicted = set()
        for name, role in valid:
            if role in roles_seen:
                conflicted.add(roles_seen[role])
                conflicted.add(name)
            else:
                roles_seen[role] = name
        if conflicted:
            logger.warning(
                f"Sub-devices {', '.join(sorted(conflicted))} share a token and a role - "
                f"none of the conflicting entries routed"
            )

        for name, role in valid:
            if name in conflicted:
                continue
            routing.by_name[name] = (token, role)
            routing.by_token.setdefault(token, {})[role] = name

    return routing


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


def _render_roled_gateway_bridge_topics(tokens: set) -> str:
    """One block per unique TOKEN used by routable roled entries - not per
    name/role, unlike _render_sub_device_bridge_topics above. That's what
    makes adding a second, non-conflicting sensor to an already-routed
    token a no-op for this rendering (same text in, same text out), so
    MqttBridge.ensure_current's existing byte-diff restart trigger needs
    no special-casing: a bridge restart only happens when the actual set
    of tokens changes, never just because a name/role mapping within an
    already-present token did.

    Only ds/#, batch_ds and event/# out, plus downlink/# in - deliberately
    narrower than the unroled set above. info/mcu, get/# and meta/# are
    not bridged here at all: several sensors sharing one Blynk device
    would stomp each other's info/metadata, and get/* gets no replies
    over mgmt_device anyway (issue #50321). The agent logs once per name
    if a roled sub-device publishes to any of these (see agent.py's
    _handle_sub_device_traffic) - this function has no visibility into
    that, it only ever sees the token.

    local_prefix is GW_TOPIC_PREFIX + token, NOT sub/<name>/ - roled
    uplinks/downlinks are rewritten and republished by the agent itself,
    not bridged directly by mosquitto (which can only rewrite topic-string
    prefixes, not insert a role prefix into a datastream name or edit
    batch_ds's JSON body) - see agent.py's gw/ handlers."""
    lines = []
    for token in sorted(tokens):
        local_prefix = f"{GW_TOPIC_PREFIX}{token}/"
        remote_prefix = f"dev/{token}/"
        lines.append(f"topic downlink/# in 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic ds/# out 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic batch_ds out 1 {local_prefix} {remote_prefix}")
        lines.append(f"topic event/# out 1 {local_prefix} {remote_prefix}")
    return "\n".join(lines) + "\n" if lines else ""


def build_gateway_connection_block(sub_devices: dict, server: str, token: str, template_id: str,
                                    local_username: str, local_password: str) -> str:
    """Returns the second "connection blynk-gateway" block to append to
    BRIDGE_TEMPLATE - empty string when there's nothing to actually route
    (no configured sub-devices yet, every discovered one is still an
    unconfigured placeholder, or every roled entry is conflicting/invalid),
    so a device that's never used gateway mode renders byte-identical to a
    plain, non-gateway device. A registry with no roles at all renders
    byte-identical to before roles existed - build_roled_routing finds
    nothing to route, and unroled renders exactly as it always has."""
    routing = build_roled_routing(sub_devices)

    unroled = {}
    for name, value in sub_devices.items():
        tok, role = parse_entry(value)
        if tok == PLACEHOLDER_TOKEN or name in routing.excluded_names:
            continue
        unroled[name] = tok

    if not unroled and not routing.tokens():
        return ""
    return GATEWAY_CONNECTION_TEMPLATE.format(
        server=server,
        token=token,
        template_id=template_id,
        sub_device_topics=(
            _render_sub_device_bridge_topics(unroled)
            + _render_roled_gateway_bridge_topics(routing.tokens())
        ),
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

# gw/ is the agent<->bridge-only namespace roled sub-device traffic gets
# rewritten onto (see _render_roled_gateway_bridge_topics) - anonymous
# clients (ordinary local apps/sensors, which only ever speak sub/<name>/)
# get no access to it at all, static and gated purely on
# GATEWAY_CAPABILITY_ENABLED like the sub-device blocks above, not on
# whether any current registry entry actually has a role yet - so a later
# console edit adding one doesn't need an agent restart to already have
# permission.
#
# Mirror-image pattern of the project's own downlink/# split: the agent
# writes uplinks (ds/batch_ds/event, rewritten from sub/<name>/...) and
# reads downlinks (republished here by the bridge, for fan-out back to
# sub/<name>/downlink/...); the bridge's local-side connection does the
# opposite - it reads what the agent just published (to relay up to Blynk
# Cloud) and writes what it receives back down from Blynk Cloud.
GW_AGENT_ACL_LINES = (
    "topic write gw/+/ds/#\n"
    "topic write gw/+/batch_ds\n"
    "topic write gw/+/event/#\n"
    "topic read gw/+/downlink/#\n"
)

GW_BRIDGE_ACL_LINES = (
    "topic read gw/+/ds/#\n"
    "topic read gw/+/batch_ds\n"
    "topic read gw/+/event/#\n"
    "topic write gw/+/downlink/#\n"
)
