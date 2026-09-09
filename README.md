# Blynk Edge Agent

Turn any Linux device into a remotely manageable Blynk device.

Connect, monitor, provision, and update it through Blynk - while your applications communicate locally over standard MQTT.

A **Blynk Agent** handles Blynk communication and device control, paired with an **MQTT Bridge** that gives your applications a simple local interface to Blynk. Multiple applications can connect concurrently through one shared, always-on Blynk connection - with authentication, certificates, and connection management handled for you.

- **One-command install** — a single script gets a fresh device fully connected to Blynk.
- **Runs on a diverse range of devices abd OS's** — from Raspberry Pi's, to NVIDIA edge AI platforms, or i.MX8-based industrial gateways. Raspbery Pi OS, Ubuntu, Debian.
- **Flexible provisioning** — Bluetooth provisioning via the Blynk mobile app, or a static token with QR code for pre-configured fleets — and connect seamlessly over Ethernet, WiFi, or cellular.
- **Zero-touch fleet deployment** — [pi-image-builder](pi-image-builder/) pre-bakes a Blynk-ready Raspberry Pi image ; flash it and ship the device; it starts BLE-advertising immediately at power on, no `install.sh`/SSH step needed at all.
- **Any application, any language** — containerize with Docker, or connect directly to the local MQTT Bridge from non-containerized processes.
- **Managed OTA updates** — containerized applications get Blynk Air's fleet-wide managed updates for free.
- **Secure remote access** — a built-in Blynk Terminal gives you a real shell on the device with no VPN needed and no exposed ports.
- **Built-in observability** — automated device metrics gathering and a ready-made Blynk dashboard for monitoring device health.

Where an MCU runs a single firmware, Linux runs many applications and processes side by side — Blynk Edge Agent keeps every benefit of that environment intact while giving all of them effortless, concurrent access to Blynk.

## How it works

```mermaid
flowchart LR
    console(["Blynk Console / App"])
    cloud(["Blynk Cloud"])

    subgraph pi["Linux Device"]
        subgraph compose["docker compose"]
            mqttbridge["mqtt-bridge<br/>local broker :1883"]
            agent["agent<br/>OTA / ping / reboot / redirect"]
            subgraph apps["your app(s) - optional"]
                app1["app 1"]
                app2["app 2"]
                appdots["..."]
            end
        end
        local(["local scripts<br/>e.g. test/*.py"])
    end

    console <--> cloud
    cloud <-->|bridge: TLS, mqttv5| mqttbridge
    agent <--> mqttbridge
    app1 -.-> mqttbridge
    app2 -.-> mqttbridge
    local <--> mqttbridge

    style compose fill:#dbe9ff,stroke:#5b8def,color:#1a1a1a
```

- **mqtt-bridge** and **agent** both run as Docker containers, managed by the same `docker-compose.yml` the agent OTA-updates.
- **mqtt-bridge** bridges the local broker to Blynk Cloud. Only mqtt-bridge holds the Blynk auth token (for that cloud bridge connection) - anything else on the device just connects to the local broker on plain, unauthenticated MQTT. Your own apps never need to know about Blynk credentials at all.
- That local broker is only reachable on the device itself (`127.0.0.1:1883`) - nothing outside the device can connect to it.
- **agent** subscribes to Blynk's downlink control topics: `downlink/ota/json` (downloads, validates, and applies a new `docker-compose.yml`, with automatic rollback on failure), `downlink/ping`, `downlink/reboot`, `downlink/redirect`, and `downlink/reconfigure`.
- You can add your own service(s) to `docker-compose.yml` alongside mqtt-bridge and agent, and/or just run your own programs directly on the device (outside Docker) - either way, they talk to the local broker, which is already bridged to Blynk. See `test/` for minimal pub/sub examples.
- Blynk's own topics (`ds/#`, `downlink/#`, etc. - see [the MQTT API docs](https://docs.blynk.io/en/blynk.cloud-mqtt-api/device-mqtt-api/topic-structure)) are what actually reach Blynk Cloud through the bridge. Your apps are free to use any other topics on the local broker too - those just stay local and never interact with Blynk at all.

## Install

Get up and running on a fresh device with a single command:

```
curl -fsSL https://raw.githubusercontent.com/anthony-blynk/blynk-edge-agent/master/install.sh | bash
```

Installs Docker if needed, prompts for this device's Blynk server/template/auth token, and starts the stack. This only needs to run once per device — see [Updating](#updating).

## Testing the connection

Once a device is provisioned, a quick way to confirm the bridge is actually working end-to-end - publish straight to a datastream from the device's own shell and watch it show up on the Blynk dashboard. No complicated authetication needed, thats all dealt with by the Agent:

First, if you don't have paho-mqtt installed yet:
```
sudo apt install python3-paho-mqtt
```
Then you can publish to the Blynk datastream with:
```
python3 -c "import paho.mqtt.publish as publish; publish.single('ds/Test', payload='42', hostname='localhost', port=1883, qos=1)"
```

Create a datastream named `Test` (type Integer) for the device's template first, or swap in any datastream you've already created - port `1883` assumes nothing else on the device is already using it (see [Troubleshooting](#troubleshooting) below if it is). See `test/` for more complete pub/sub examples.

## WiFi provisioning

A device with no stored Blynk auth token (`blynk.env` left blank during `install.sh`, or after a `downlink/reconfigure`) advertises over Bluetooth LE and runs the Blynk app's provisioning flow ("Blynk.Inject"): the app scans, connects, requests the device's real network interfaces, and - for a WiFi interface - requests a scan and lets the user pick an SSID, enter a password, and optionally set a static IP. The device configures WiFi and the Blynk connection through NetworkManager over D-Bus (the host's, not the container's own - reached over the same D-Bus socket already used for BlueZ), reporting live status back over the same BLE link the whole time, including specific failure reasons (e.g. a wrong password) so the app can prompt for corrected credentials without needing to reconnect.

If a previously-provisioned device later loses connectivity for a sustained period (WiFi credentials changed at the router, for example), it automatically re-enters this same BLE provisioning flow **in place** - without a reboot, and without discarding the already-stored Blynk auth token - so the app only needs to supply corrected network info. This is checked roughly every minute against `$SYS/broker/connection/blynk-bridge-{template_id}/state` (mqtt-bridge's own bridge connection-state notification), and only triggers after the bridge has been down continuously for several minutes, to ride out brief blips rather than reprovisioning on the first hiccup.

## Diagnostics and system info

At startup, the agent publishes static system facts as datastreams: device model, OS, kernel version, architecture, total memory, total disk (`AgentDeviceModel`, `AgentOS`, `AgentKernel`, `AgentArchitecture`, `AgentTotalMemory`, `AgentTotalDisk`). These are plain datastreams rather than Blynk's metadata fields - metadata is the better semantic fit for static facts like these, but dashboard widgets currently can't display metadata fields, only datastreams, so datastreams are what's actually usable on the dashboard.

Live health metrics - CPU usage, memory usage, disk usage, temperature, uptime, and which network interface is actually carrying traffic right now (`AgentCPUUsage`, `AgentMemUsage`, `AgentDiskUsage`, `AgentTemperature`, `AgentUptime`, `AgentConnectionType`, `AgentIPAddress`, `AgentSignalQuality`) - report every 60s while enabled via a console Switch widget bound to an `AgentDiagnosticsEnabled` datastream, **on by default**. The agent asks Blynk for the current on/off state each time it (re)starts rather than assuming a default, so a restarted agent picks back up whatever you last set rather than silently reverting. The system/CPU/memory/disk/temperature/uptime metrics are read directly from `/proc`, `/sys`, and Python's standard library - no extra dependency; connection type/IP/signal quality come from NetworkManager (and ModemManager for cellular signal) over the same D-Bus connection already used for BLE provisioning.

`AgentConnectionType` reflects NetworkManager's own "primary connection" concept - whichever interface currently has the best route, not just "some interface happens to be connected" - so on a device with Ethernet, WiFi, and cellular all simultaneously active, it reports whichever one is actually carrying traffic. `AgentSignalQuality` (0-100) only applies to WiFi and cellular - there's no such concept for a wired Ethernet link, so it's simply not published in that case. `AgentIPAddress` is the primary connection's own IPv4 address.

To set it up, create these datastreams for the device's template: the six system-info fields above (String), `AgentCPUUsage`/`AgentMemUsage`/`AgentDiskUsage`/`AgentSignalQuality` (Double, 0-100), `AgentTemperature` (Double, 0-110), `AgentUptime` (Double, seconds - a Label widget with a custom mapping/formatter reads better than raw seconds), `AgentConnectionType`/`AgentIPAddress` (String), and `AgentDiagnosticsEnabled` (Integer, 0-1, with a Switch widget - reporting runs even without this widget added at all, since the agent treats no stored value as "on"; add the widget if you want to be able to turn it off). Add Label/Gauge/History Graph widgets bound to whichever of these you want visible on the dashboard.

## Remote terminal (on by default for now)

Blynk's [Terminal widget](https://docs.blynk.io/en/blynk.console/widgets-console/terminal) can give you a real shell on the device, entirely over the same outbound connection the agent already uses - no inbound port, no VPN, nothing exposed to the network beyond what's already there for Blynk itself. Commands run via `nsenter` into the host's own namespaces, so `pwd`/`ls`/`ps`/etc. reflect the actual device, not just the agent's own container.

This is a real shell with real access, so it's behind two independent switches rather than one:

- **Capability** - a `docker-compose.yml` environment variable (`AGENT_TERMINAL_ENABLED` on the `agent` service), only changeable via an OTA push or a manual edit on the device itself. This is deliberate: compromising your Blynk account credentials alone should never be enough to get a shell on a device that never had this turned on - that requires a second, harder action. **Currently defaults to `true` in the tracked `docker-compose.yml`**, so the feature is obvious and easy to try - set it to `false` (or remove the line) before deploying to a production fleet, or for any device you don't want this on at all, since at that point no Switch toggle in Blynk can turn it back on without an OTA push or a manual edit here.
- **Session** - a Switch widget bound to an `AgentTerminalEnabled` datastream, for quick on/off without needing an OTA push every time you actually want to use it. This is the one that matters day to day - turn it off when you're not actively using the terminal.

Both need to be on for commands to run - the terminal always replies with a `[terminal disabled: ...]` message explaining which one is off, rather than silently doing nothing either way.

To set it up, create two datastreams for the device's template - `AgentTerminal` (String, with a Terminal widget) and `AgentTerminalEnabled` (Integer, 0-1, with a Switch widget). The capability itself is already on by default (see above); flip the Switch widget on when you want to actually use it.

## Security

This is a security-focused project, not just a wiring exercise - the local broker and the OTA path are both treated as trust boundaries:

- **The local broker stays fully anonymous for ordinary traffic, but `downlink/#` is locked down.** Only mqtt-bridge's own local connection (write) and the agent's own local connection (read) can touch it, each authenticating with its own per-device credentials generated on first boot and never shared with anything else on the device. Without this, any local process could forge a fake OTA/reboot/terminal command just by publishing to the same topic the real bridge uses. Every other local topic (`ds/#`, `event/#`, etc.) stays anonymous - your own apps never need credentials to use Blynk.
- **Only mqtt-bridge ever holds the real Blynk auth token.** The agent, your own apps, and anything else on the device only ever talk to the local broker - never the actual Blynk credentials.
- **The local broker is never exposed to the network** - it only listens on `127.0.0.1`, so nothing outside the device can reach it regardless of the ACL above.
- **The cloud connection is TLS** (`mqttv5` over `8883`, with the CA bundle verified) - never plaintext.
- **Remote Terminal access needs two independent things to be true, not one** - a capability gate in `docker-compose.yml` (only changeable via an OTA push or a manual edit on the device) and a per-session Switch widget in the app. Compromising your Blynk account credentials alone is never enough to get a shell on a device that never had the capability turned on - see [Remote terminal](#remote-terminal-on-by-default-for-now) above.
- **Pre-baked images ship with Remote Terminal off, not on** - see [pi-image-builder](pi-image-builder/). A zero-touch image gets flashed and shipped to devices no installer necessarily sets eyes on again, so a live shell capability shouldn't default to on the way it does for `install.sh`'s manual, single-device setup. No baked-in password or SSH key either - one shared secret across an entire fleet would be a single point of compromise; the per-device Blynk Terminal is the actual answer, opt-in per device.
- **OTA pushes are validated before being applied**, and roll back automatically if the new stack fails to come up - a malformed or broken `docker-compose.yml` doesn't get to replace a device's working config.

This is a young, actively-developed project rather than an independently audited one - if you find something that concerns you, please open an issue.

## Updating

Updates go through Blynk OTA, not by re-running `install.sh`. Grab the latest [`docker-compose.yml`](docker-compose.yml) (merging in your own additions if you've customized it) and upload it through your Blynk console's OTA feature for that device.

## Pre-baked images for zero-touch deployment

[`pi-image-builder`](pi-image-builder/) builds a ready-to-flash Raspberry Pi image (Pi 5 or Compute Module 4) with Docker, this project's stack, and the device's server/template ID already filled in - no auth token baked in (that's the one genuinely per-device secret, and still comes from the Blynk app during BLE provisioning). Flash it, power the device on with no network connection at all, and it starts BLE-advertising immediately - skipping `install.sh`/SSH entirely, which matters at real fleet scale where SSH-ing into every device individually isn't practical.

Confirmed working end-to-end on real hardware for both a Pi 5 and a Compute Module 4 (including a Lite/non-wireless CM4 using USB Bluetooth and WiFi dongles instead of onboard radios) - see [`pi-image-builder/README.md`](pi-image-builder/README.md) for the build/flash steps and hardware-specific notes.

## Troubleshooting

### `port is already allocated` on `mqtt-bridge` (something else already uses 1883)

`install.sh` now checks for this itself before starting the stack and prompts for an alternate host port if 1883 is already taken - if you hit this anyway (an existing `docker-compose.yml` from before that check existed, or `ss` wasn't available to detect it), here's the fix. Confirmed on a device already running Node-RED with its own MQTT broker, and separately on a RAK LoRaWAN gateway already running its own mosquitto for its packet-forwarder/AWS IoT Core bridge. The `agent`/`mqtt-bridge` containers still connect to each other over Docker's internal network regardless of any host-side port mapping - the `127.0.0.1:1883:1883` mapping in `docker-compose.yml` only exists for convenience access from outside Docker (e.g. the [Testing the connection](#testing-the-connection) snippet above, or `test/*.py`). Safe to remap without affecting the bridge itself:

```
sudo sed -i 's/127.0.0.1:1883:1883/127.0.0.1:18830:1883/' /opt/blynk/docker-compose.yml
docker compose -f /opt/blynk/docker-compose.yml up -d
```

Use the new port for any host-side script connecting directly to the broker.

### BLE provisioning: device won't advertise / registering the advertisement fails

Raspberry Pi kernels around `6.18.34` have a Bluetooth regression that breaks BLE advertising entirely - the agent logs `Failed to register advertisement`, and `sudo btmon` or `sudo journalctl -u bluetooth` shows `Invalid Parameters (0x0d)` on `Add Extended Advertising Data`. Confirmed and fixed upstream: [raspberrypi/linux#7473](https://github.com/raspberrypi/linux/issues/7473), fix commit `58d810354de1b`, first shipped in Linux **6.18.36**.

- Check `apt-cache policy raspberrypi-kernel` (or `linux-image-*`) - if a version ≥6.18.36 is offered, a normal `sudo apt upgrade` fixes this and nothing else below is needed.
- If a fixed version isn't in apt yet, pull it directly:
  ```
  sudo rpi-update
  sudo reboot
  ```
- To instead roll back to the exact kernel this project was tested against while waiting for a fixed release (`6.12.47+rpt`, confirmed working):
  ```
  sudo rpi-update 6d1da66a7b1358c9cd324286239f37203b7ce25c
  sudo reboot
  ```
  That commit is from [raspberrypi/rpi-firmware](https://github.com/raspberrypi/rpi-firmware), not `raspberrypi/firmware` - the two repos look similar but only `rpi-firmware` commits work with `rpi-update`. After rolling back, hold the broken kernel packages so a routine `apt upgrade` doesn't reintroduce the bug (check `apt list --installed | grep linux-image` for your exact package names first):
  ```
  sudo apt-mark hold linux-image-6.18.34+rpt-rpi-2712 linux-image-6.18.34+rpt-rpi-v8 linux-image-rpi-2712 linux-image-rpi-v8
  ```
