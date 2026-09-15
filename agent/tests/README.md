# agent/tests

Unit tests for `agent.py`'s own logic - `BlynkConfig`, `ComposeManager`, `_write_acl_file`, etc. No device, no Docker, no MQTT broker, no network required; runs anywhere in a few hundred milliseconds. Contrast with `device-tests/` at the repo root, which needs a real, already-provisioned device to run against at all.

Add a test here whenever a bug is found in logic that doesn't actually need real hardware to exercise - several real bugs this project has shipped (an OTA checksum bypass, a non-atomic config write, a permissions bug that crash-looped mosquitto) lived entirely in this category, and only got caught by manually testing on real hardware because nothing here existed yet to catch them faster.

## Usage

```
python3 -m venv ~/agent-tests-venv
~/agent-tests-venv/bin/pip install -r agent/tests/requirements.txt

~/agent-tests-venv/bin/python -m pytest agent/tests -v
```

Runs on any Linux/Mac/Windows dev machine - doesn't need to be a Pi, doesn't need `/opt/blynk` to exist. A couple of tests that assert real POSIX permission bits (e.g. `chmod 0600`) are skipped outside Linux/Mac, since Windows has no equivalent to assert against.
