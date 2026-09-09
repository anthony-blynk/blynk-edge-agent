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

- `test_acl.py` - the local broker's ACL (see the top-level README's Security section): ordinary local traffic (`ds/#` etc.) stays anonymous, `downlink/#` is write-only for the bridge's identity and read-only for the agent's, and both are denied to anonymous clients. Uses MQTT5 explicitly (unlike this project's own client code, which defaults to v3.1.1) specifically so a denied publish/subscribe shows up as a real reason code - under plain MQTT v3.1.1 a denied QoS 1 publish still gets an ordinary-looking PUBACK, indistinguishable from success to the publishing client.

## Not yet covered

- **Cloud round-trip verification** (does a local publish actually reach Blynk Cloud, does an API-driven datastream update actually reach the device) - Blynk's HTTP Device API only addresses a datastream by virtual pin number or numeric `dataStreamId`, not by name the way the MQTT API's `ds/<name>` topics do, so this needs a small one-time pin/ID mapping this project doesn't otherwise track anywhere - not yet built.
- Diagnostics-on-by-default, redirect handling, OTA apply/rollback, remote terminal - straightforward to add following the same pattern as `test_acl.py` once there's a concrete need to re-verify them on real hardware.
