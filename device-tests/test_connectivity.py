"""Confirms Blynk Cloud currently sees this device as connected - the
simplest possible smoke test that the mqtt-bridge's actual cloud connection
is up, not just the local broker (test_acl.py never leaves the device).
Only needs the device's own token, no datastream/pin setup required."""

from conftest import is_device_connected


def test_device_is_connected(blynk_config):
    assert is_device_connected(blynk_config["BLYNK_SERVER"], blynk_config["BLYNK_AUTH_TOKEN"])
