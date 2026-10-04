# USB Barcode Scanner example

Reads a USB barcode scanner running in "keyboard wedge" mode and publishes
each scan as its own, independent Blynk device - using the edge-agent's
[MQTT Gateway](../../README.md#mqtt-gateway-remote-devices) feature, not this
device's own identity. Reads the scanner directly via Linux's `evdev` (not
by capturing keystrokes typed into a focused window), so scans never leak
into a terminal or any other app running on the host.

**Numeric barcodes only in this version** (UPC/EAN and similar) - a scan
containing letters logs a warning per unmapped key and otherwise drops them.

**Single scanner only in this version.** Some scanner chipsets don't expose
a unique USB serial number (confirmed on at least one real unit - its
`/dev/input/by-id/...` path has no serial in it at all), so there's currently
no reliable way to tell two identical scanners apart. Running two different
*models* at once would work fine already; two identical ones would not.

## Prerequisites (all one-time, manual)

This relies entirely on the edge-agent's Gateway feature already being set
up and working on this device - if you haven't done that yet, start with
the main README's [MQTT Gateway](../../README.md#mqtt-gateway-remote-devices)
section first. Specifically, you need:

1. **A Blynk Enterprise org**, with "Enable for Gateway API" turned on for
   whatever template this scanner will use.
2. **A separate Blynk device** created for the scanner (console or HTTP
   device-creation API), under that template, with a `BarcodeScanned`
   datastream (String type) - bind it to a Label/Terminal widget if you want
   to see scans live on a dashboard.
3. **A registry entry** for it in this device's `/opt/blynk/remote_devices.json`
   - e.g. `{"warehouse-scanner-1": "<that device's own real auth token>"}`.
4. **`AGENT_GATEWAY_ENABLED=true`** on this device (see the main README).

## Finding your scanner's device path

```bash
ls -l /dev/input/by-id/
```

Look for an entry ending in `-event-kbd` - that's the stable path to use for
`SCANNER_DEVICE_PATH` below (survives reboots/replugs, unlike `/dev/input/eventN`
which can renumber).

## Enabling on a device

Published as a prebuilt image (`ghcr.io/anthony-blynk/usb-barcode-scanner`) -
nothing to build yourself. Uncomment the `usb-barcode-scanner` service in the
root [`docker-compose.yml`](../../docker-compose.yml), fill in
`SCANNER_DEVICE_PATH` and `REMOTE_DEVICE_NAME` for your actual scanner/registry
entry, then `docker compose up -d`.

**Don't push this uncommented via a fleet-wide OTA update** - same reasoning
as the [Edge AI Face Detector](../edge-ai-face-detector/) example: a device
without that exact scanner plugged in would fail to start this service. This
is a per-device, opt-in example, enabled by hand on that device's own
deployed copy of the file.

Verified end-to-end on real hardware: a scanned barcode shows up correctly
on its registered device's own `BarcodeScanned` datastream in the Blynk
console.
