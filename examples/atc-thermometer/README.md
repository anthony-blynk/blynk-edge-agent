# ATC BLE Thermometer example

Passively scans for Xiaomi Mijia (LYWSD03MMC and similar) sensors flashed
with the [atc_MiThermometer](https://github.com/pvvx/ATC_MiThermometer)
custom firmware, and publishes each one as its own, independent Blynk
device - using the edge-agent's
[MQTT Gateway](../../README.md#mqtt-gateway-sub-devices) feature, not this
device's own identity. No pairing/connecting to the sensors at all - their
temperature/humidity/battery readings are broadcast directly in plain BLE
advertisements, read passively via `bleak`.

**Fully automatic multi-sensor support** - unlike the
[USB Barcode Scanner](../usb-barcode-scanner/) example, there's no
"can't tell two identical devices apart" problem here: every sensor
broadcasts its own unique MAC address, which becomes its sub-device
registry name (`atc-<mac, no colons>`, e.g. `atc-a4c138ae77da`). Combined
with the gateway's auto-discovery, this means plugging in any number of
these sensors just works - each one registers itself the moment it's first
seen, no per-sensor configuration needed on this device at all.

Supports both of the firmware's known advertisement layouts (the original
13-byte "atc1441" format and the newer 14-byte "pvvx" format) - whichever
one a given sensor is actually using is determined by payload length, not
assumed from the firmware name. Only publishes when a sensor's own packet
counter changes, not on every advertisement - these broadcast far more
often than their actual reading interval.

## Prerequisites (all one-time, manual)

This relies entirely on the edge-agent's Gateway feature already being set
up and working on this device - if you haven't done that yet, start with
the main README's [MQTT Gateway](../../README.md#mqtt-gateway-sub-devices)
section first. Specifically, for **each** sensor you want to bridge:

1. **A Blynk Enterprise org**, with "Enable for Gateway API" turned on for
   whatever template fits these sensors.
2. **A separate Blynk device** created for that sensor (console or HTTP
   device-creation API), under that template, with `Temperature`,
   `Humidity`, and `Battery` datastreams (all Number/Double type).
3. **A registry entry** - or just let it auto-register: plug the sensor in
   (power it on, within range), and this device will add it to
   `AgentSubDeviceRegistry` itself with a placeholder token the first time
   it sees a reading. Replace the placeholder with that sensor's own real
   auth token once it shows up.
4. **`AGENT_GATEWAY_ENABLED=true`** on this device (see the main README).

## Enabling on a device

Published as a prebuilt image (`ghcr.io/anthony-blynk/atc-thermometer`) -
nothing to build yourself. Uncomment the `atc-thermometer` service in the
root [`docker-compose.yml`](../../docker-compose.yml), then
`docker compose up -d`. No per-sensor environment variables to fill in -
every sensor in range is picked up automatically.

**Don't push this uncommented via a fleet-wide OTA update** - same
reasoning as every other example here: a device without a Bluetooth
adapter, or one already relying on it for something incompatible, would
fail to start this service. This is a per-device, opt-in example, enabled
by hand on that device's own deployed copy of the file.

Not yet verified end-to-end against real hardware - the BLE decode itself
was (confirmed real, plausible temperature/humidity readings from an
actual sensor), but this packaged container - including whether it needs
`privileged: true` the way the barcode scanner example does, and whether
continuous BLE scanning interferes with this device's own BLE
provisioning - hasn't had a real run yet. Treat the first real attempt as
iteration, same as every other hardware-facing example in this project.
