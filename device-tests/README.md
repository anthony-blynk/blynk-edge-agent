# device-tests

An on-device verification suite - not CI (there's no real-hardware CI for this project), but a repeatable replacement for manually SSHing in and eyeballing raw mosquitto logs each time a change needs testing on real hardware. Add a test whenever a new feature needs the same kind of real-device verification, rather than doing it ad hoc again.

**Must run on the device itself** - the tests talk to the local broker at `127.0.0.1:1883` and read local config files (`/opt/blynk/blynk.env`, `/opt/blynk/mqtt-bridge/local_broker_creds.env`) that only exist on a device that's actually run this stack.

## Usage

The usual flow when testing an `-rcN` build on a real device:

```
cd ~/blynk-edge-agent   # first time: git clone https://github.com/anthony-blynk/blynk-edge-agent.git ~/blynk-edge-agent
git pull                # picks up whichever commit's rc you just deployed - version bump and code land in the same commit
pip install -r device-tests/requirements.txt
pytest device-tests -v
```

## What's here

- `test_acl.py` - the local broker's ACL (see the top-level README's Security section): ordinary local traffic (`ds/#` etc.) stays anonymous, `downlink/#` is write-only for the bridge's identity and read-only for the agent's, and both are denied to anonymous clients. Uses MQTT5 explicitly (unlike this project's own client code, which defaults to v3.1.1) specifically so a denied publish/subscribe shows up as a real reason code - under plain MQTT v3.1.1 a denied QoS 1 publish still gets an ordinary-looking PUBACK, indistinguishable from success to the publishing client. Read access is checked via actual message delivery, not the SUBACK reason code - confirmed on real hardware that mosquitto grants the SUBACK for a wildcard subscription like `downlink/#` regardless of ACL, and filters delivery instead.
- `test_cloud_roundtrip.py` - confirms a local `ds/#` publish actually reaches Blynk Cloud, not just the local broker (`test_acl.py` can't tell you that). **Skipped unless set up first** (see below), since it needs a real datastream and its pin - the HTTP Device API has no by-name lookup the way the MQTT API's `ds/<name>` topics do.

### One-time setup for `test_cloud_roundtrip.py`

1. In this device's Blynk template, create a datastream to use as the test's own scratch value (any numeric type, e.g. `AgentSelfTest` as an Integer) - note its virtual pin (e.g. `V50`).
2. Set two env vars before running pytest:
   ```
   export TEST_DATASTREAM_NAME=AgentSelfTest
   export TEST_DATASTREAM_PIN=V50
   pytest device-tests -v
   ```
   Without `TEST_DATASTREAM_PIN` set, this one test is skipped rather than failed - the rest of the suite (`test_acl.py`) still runs normally.

## Not yet covered

- The cloud round-trip test only covers a **local publish reaching the cloud** - not the reverse direction (an API-driven datastream update reaching the device), which would exercise a real downlink path the same way toggling a console Switch widget does.
- Diagnostics-on-by-default, redirect handling, OTA apply/rollback, remote terminal - straightforward to add following the same pattern once there's a concrete need to re-verify them on real hardware.
