# Modbus RTU Gateway example

Polls Modbus RTU slaves on an RS485 bus and publishes them as datastreams
on **this device's own** Blynk identity (no MQTT Gateway/sub-device feature
needed, unlike the [ATC Thermometer](../atc-thermometer/) and
[USB Barcode Scanner](../usb-barcode-scanner/) examples) - writes downlink
values back to coils/holding registers. The register map isn't hardcoded:
it's the `ModbusConfig` datastream, edited live from the Blynk web console
through the bundled [`widget/modbus-console.html`](widget/modbus-console.html)
widget - add points, pick their Modbus address/type/scaling, probe a slave
or scan for one, all without touching this device again. The format (and
`ModbusStatus`/`ModbusProbe`/`ModbusProbeResult`) is the contract in
[`docs/config-format.md`](docs/config-format.md).

Ported from a standalone proof-of-concept that did its own BLE pairing and
held its own Blynk credentials - here it's just another app talking to the
edge-agent's local MQTT bridge, same as every other example in this
project.

## Prerequisites (all one-time, manual)

1. **Datastreams on this device's own template**: `ModbusConfig` (String,
   default value `{"v":2,"rev":0,"poll_s":5,"ports":[{"baud":9600,"pts":[]}]}`
   - Blynk doesn't answer `get/ds` without one), `ModbusStatus` (String),
   `ModbusProbe` (String), `ModbusProbeResult` (String), plus one datastream
   (String, unless you know you want Double) per point you plan to add
   (e.g. `Temperature`, `Humidity`, `Relay`) - datastreams can't be created
   from the widget at runtime.
2. **The widget**, added to this template's dashboard: in the web console's
   dashboard editor, add a Web Console / HTML widget and paste in
   [`widget/modbus-console.html`](widget/modbus-console.html), then open its
   settings and assign all four control datastreams above plus every point
   datastream you created.
3. **Your RS485 adapter's device path** - `ls -l /dev/serial/by-id/` (USB
   adapter) or the hardware's own fixed path (e.g. `/dev/ttymxc0` on a
   CompuLab IOT-GATE-iMX8) - for `MODBUS_PORT` below.

## Enabling on a device

Published as a prebuilt image (`ghcr.io/anthony-blynk/modbus-gateway`) -
nothing to build yourself. Uncomment the `modbus-gateway` service in the
root [`docker-compose.yml`](../../docker-compose.yml), fill in
`MODBUS_PORT` for your actual adapter, then `docker compose up -d`.

**Don't push this uncommented via a fleet-wide OTA update** - same
reasoning as every other example here: a device with no RS485 adapter at
that path would fail to start this service. This is a per-device, opt-in
example, enabled by hand on that device's own deployed copy of the file.

Once it's running, open the widget: it starts empty (`ModbusConfig` with no
points), add a point by name/slave id/table/address, hit "Probe" to confirm
it reads back the value you expect, then save - the gateway applies the new
config immediately (no restart) and starts publishing that point.

**Requires the edge-agent's downlink relay** (not yet in a released agent
version as of this writing - see the main [README's Security
section](../../README.md#security)): editing the config, probing, and
writing to an actuator point (like `Relay`) all arrive as a downlink to
`ModbusConfig`/`ModbusProbe`/the point's own name, which this container
receives via the agent's `local/blynk/downlink/ds/<name>` mirror, not the
real `downlink/ds/<name>` (never delivered to an anonymous local app like
this one) - see [`modbus_gateway/__main__.py`](modbus_gateway/__main__.py)'s
`_LocalDownlinkMqtt` adapter. Until a device's agent has that relay, saving
a config from the widget will appear to succeed (it's just a datastream
write) but the gateway will never see it.

**Verification status**: the gateway logic itself (bus locking, backoff,
the config format, the widget) was tested against real Modbus hardware (an
XY-MD02 temp/humidity sensor and a relay, on a CompuLab IOT-GATE-iMX8) in
the source proof-of-concept. The adapter's topic translation was verified
with scratch tests driving the real `Gateway` class end-to-end (subscribe,
an incoming config push, applying it) against a fake MQTT client, and the
agent's relay itself was separately verified on that same CompuLab against
a real Blynk Cloud push. The full chain - a real widget save reaching this
container's real RS485 hardware through a relay-enabled agent - hasn't yet
been run together in one pass; that's the next thing to confirm once an
agent release with the relay exists.

## Multiple RS485 ports

Give `MODBUS_PORT` as a comma-separated list (e.g.
`/dev/ttyUSB0,/dev/ttyUSB1`) and map every device under `devices:` in the
compose service - the order is what the config's `ports[0]`, `ports[1]`
refer to, and each port is polled on its own thread.

## Persistent state

The last accepted config (so the gateway polls immediately on restart,
without waiting for Blynk) and write-confirmed values/open alerts live
under `/var/lib/modbus-gateway` inside the container, backed by the
`modbus_gateway_data` named volume in `docker-compose.yml` - without that
volume, every container recreation starts from an empty config again.

## Behaviour worth knowing

- One lock around the bus: polls, downlink writes and probes never
  interleave. Probes run on the poll thread between point reads.
- A slave that times out 3 times in a row is skipped for 5s, doubling to
  60s, until it answers again; its points show `offline (backoff)` in
  `ModbusStatus.bad`. Writes and probes ignore the backoff.
- Readings are not published (or queued) while the local MQTT bridge is
  down - a config edit made while disconnected is picked up on reconnect
  via `get/ds`.
- After a successful write, the value decoded back from what was written is
  published as the confirmed state (so `"1.0"` echoes as `"1"`, and a
  setpoint rounded to the register's resolution shows the rounded value).
