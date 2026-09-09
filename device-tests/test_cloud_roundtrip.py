"""Verifies a local ds/# publish actually reaches Blynk Cloud - test_acl.py
only confirms local broker behavior, not that the whole pipeline (local
broker -> mqtt-bridge -> TLS -> Blynk Cloud -> stored datastream value)
actually delivers.

One-time setup per device's Blynk template: create a datastream named
AgentSelfTest (any numeric type) at virtual pin V50 - see conftest.py's
DEFAULT_TEST_DATASTREAM_NAME/PIN and README.md. Override via
TEST_DATASTREAM_NAME/TEST_DATASTREAM_PIN env vars only if a specific
device's template genuinely can't use that fixed pin.
"""

import os
import time

from conftest import DEFAULT_TEST_DATASTREAM_NAME


def test_local_publish_reaches_blynk_cloud(anon_client, device_api):
    datastream_name = os.environ.get("TEST_DATASTREAM_NAME", DEFAULT_TEST_DATASTREAM_NAME)
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
