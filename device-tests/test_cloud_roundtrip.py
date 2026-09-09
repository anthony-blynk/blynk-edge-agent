"""Verifies a local ds/# publish actually reaches Blynk Cloud - test_acl.py
only confirms local broker behavior, not that the whole pipeline (local
broker -> mqtt-bridge -> TLS -> Blynk Cloud -> stored datastream value)
actually delivers.

Needs a real datastream already created in this device's Blynk template
(any type that accepts a numeric string is fine), and two env vars set
before running pytest:
  TEST_DATASTREAM_NAME - the datastream's name, e.g. AgentSelfTest (used
                          for the local ds/<name> publish topic)
  TEST_DATASTREAM_PIN  - that same datastream's virtual pin, e.g. V50
                          (the HTTP Device API has no by-name lookup - see
                          conftest.py's DeviceAPI)
Skipped entirely if TEST_DATASTREAM_PIN isn't set - see README.md.
"""

import os
import time


def test_local_publish_reaches_blynk_cloud(anon_client, device_api):
    datastream_name = os.environ.get("TEST_DATASTREAM_NAME", "AgentSelfTest")
    value = str(int(time.time()))
    anon_client.publish_and_wait(f"ds/{datastream_name}", value)

    deadline = time.time() + 15
    seen = None
    while time.time() < deadline:
        seen = device_api.get_value()
        if seen == value:
            break
        time.sleep(1)

    assert seen == value, f"expected {value!r} to reach Blynk Cloud, last saw {seen!r}"
