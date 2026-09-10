# device-tests

An on-device verification suite - not CI (there's no real-hardware CI for this project), but a repeatable replacement for manually SSHing in and eyeballing raw mosquitto logs each time a change needs testing on real hardware. Add a test whenever a new feature needs the same kind of real-device verification, rather than doing it ad hoc again.

**Must run on the device itself** - the tests talk to the local broker at `127.0.0.1` (whatever host port it's actually on - auto-detected from `docker-compose.yml`, same as some gateways needing a remapped port instead of the default `1883`; override with `MQTT_BROKER_PORT` if that detection ever needs it) and read local config files (`/opt/blynk/blynk.env`, `/opt/blynk/mqtt-bridge/local_broker_creds.env`) that only exist on a device that's actually run this stack.

## Usage

The usual flow when testing an `-rcN` build on a real device:

```
cd ~/blynk-edge-agent   # first time: git clone https://github.com/anthony-blynk/blynk-edge-agent.git ~/blynk-edge-agent
git pull                # picks up whichever commit's rc you just deployed - version bump and code land in the same commit

# first time on this device only:
sudo apt install -y python3-pip python3-venv
python3 -m venv ~/device-tests-venv
~/device-tests-venv/bin/pip install -r device-tests/requirements.txt

~/device-tests-venv/bin/python -m pytest device-tests -v
```

A venv, not a bare `pip install`/system Python - confirmed on real hardware that newer Debian/Raspberry Pi OS (Bookworm+) refuses a system-wide `pip install` outright (PEP 668's "externally-managed-environment"). `--break-system-packages` would work around it, but risks whatever else on the device depends on its system Python packages - a venv avoids that entirely and is Debian's own recommended fix.

### Running it against several devices at once

`run_fleet.py` SSHes into every device listed in a config file, runs the same suite on each (cloning/updating the repo and setting up its venv itself, same as the manual steps above), and prints a pass/fail summary - saves manually SSHing into each device individually when testing a release against a Jetson, a CompuLab, a Pi5, a CM4, etc. Run this from your own machine, not a device:

```
pip install -r device-tests/fleet_requirements.txt
cp device-tests/fleet.example.json device-tests/fleet.json   # then edit with real hosts/credentials
python3 device-tests/run_fleet.py device-tests/fleet.json
```

`fleet.json` is gitignored - it can hold real SSH passwords, which must never be committed. Prefer key-based auth (the default when a device entry has neither `password` nor `key_filename` - paramiko falls back to the system's SSH agent/default keys) over passwords where you can. Each run does a `git fetch` + hard reset to `origin/master` in the device's own checkout - it's meant to be a disposable, always-fresh clone dedicated to running tests, not somewhere to keep ad hoc edits.

## What's here

- `test_acl.py` - the local broker's ACL (see the top-level README's Security section): ordinary local traffic (`ds/#` etc.) stays anonymous, `downlink/#` is write-only for the bridge's identity and read-only for the agent's, and both are denied to anonymous clients. Uses MQTT5 explicitly (unlike this project's own client code, which defaults to v3.1.1) specifically so a denied publish/subscribe shows up as a real reason code - under plain MQTT v3.1.1 a denied QoS 1 publish still gets an ordinary-looking PUBACK, indistinguishable from success to the publishing client. Read access is checked via actual message delivery, not the SUBACK reason code - confirmed on real hardware that mosquitto grants the SUBACK for a wildcard subscription like `downlink/#` regardless of ACL, and filters delivery instead.
- `test_connectivity.py` - the simplest possible smoke test: confirms Blynk Cloud currently sees this device as connected (`isHardwareConnected`), i.e. the mqtt-bridge's actual cloud connection is up, not just the local broker. Confirmed on real hardware that this reflects the real connection (stopping mqtt-bridge flips it to false, and it stays false across repeated HTTP calls made while down) rather than just whatever the HTTP API's own recent activity happens to be.
- `test_cloud_roundtrip.py` - confirms a local `ds/#` publish actually reaches Blynk Cloud, not just the local broker (`test_acl.py` can't tell you that). Needs a real datastream and its pin to do this, since the HTTP Device API has no by-name lookup the way the MQTT API's `ds/<name>` topics do - see setup below.

### One-time setup for `test_cloud_roundtrip.py`

In each device's Blynk template, create a datastream named `AgentSelfTest` (any numeric type) at virtual pin `V100` - a fixed convention (same as this project's other `Agent*` datastreams having a fixed documented type), not something to look up or customize per device. `V100` is deliberately well clear of the low-numbered pins this project's other `Agent*` datastreams and most templates' own pins tend to use. No env vars needed for the normal case. If a specific template genuinely can't use pin `V100`, override with `TEST_DATASTREAM_NAME`/`TEST_DATASTREAM_PIN` env vars before running pytest for that device only.

(The obvious alternative - looking the pin up by name automatically - needs Blynk's Platform API `GET /api/v1/organization/template/datastreams`, which requires an account-level Bearer/JWT login, not the device's own token. Putting that on a device would be a real security downgrade versus this project's "devices only ever hold their own per-device token" design, just to avoid a fixed pin convention.)

## Not yet covered

- The cloud round-trip test only covers a **local publish reaching the cloud** - not the reverse direction (an API-driven datastream update reaching the device), which would exercise a real downlink path the same way toggling a console Switch widget does.
- Diagnostics-on-by-default (e.g. restart the agent container and confirm a diagnostics datastream gets a fresh value within ~70s), remote terminal round-trip (an API-driven `AgentTerminal` update with a safe echo-style command, checked for the expected reply - only if the terminal capability + switch are already on, never turning them on itself) - straightforward to add following the same pattern once there's a concrete need to re-verify them on real hardware.
- OTA apply/rollback and redirect handling are both genuinely state-changing on a live device (recreating containers, moving the real cloud connection) - deliberately not routine auto-run tests here. Redirect's payload-parsing logic specifically could be isolated as a plain unit test with no device involved at all, rather than exercising the real side effect.
- BLE/WiFi provisioning needs an actual phone/BLE central to drive it - out of scope for this SSH-and-pytest setup.
