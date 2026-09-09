"""Verifies the local broker's ACL (see agent/agent.py's ACL_TEMPLATE and
the README's Security section): ordinary local traffic stays anonymous,
while downlink/# is restricted to the bridge (write-only) and agent
(read-only) identities. Run on the device itself - see README.md."""

from conftest import RC_NOT_AUTHORIZED, RC_SUCCESS


def test_anonymous_can_publish_ds(anon_client):
    reason_code = anon_client.publish_and_wait("ds/TestACL", "1")
    assert reason_code == RC_SUCCESS


def test_anonymous_can_subscribe_ds(anon_client):
    reason_codes = anon_client.subscribe_and_wait("ds/TestACL")
    assert all(rc < 128 for rc in reason_codes)


def test_anonymous_cannot_publish_downlink(anon_client):
    reason_code = anon_client.publish_and_wait("downlink/TestACL", "1")
    assert reason_code == RC_NOT_AUTHORIZED


def test_anonymous_cannot_subscribe_downlink(anon_client):
    reason_codes = anon_client.subscribe_and_wait("downlink/#")
    assert all(rc >= 128 for rc in reason_codes)


def test_bridge_can_publish_downlink(bridge_client):
    reason_code = bridge_client.publish_and_wait("downlink/TestACL", "1")
    assert reason_code == RC_SUCCESS


def test_bridge_cannot_subscribe_downlink(bridge_client):
    reason_codes = bridge_client.subscribe_and_wait("downlink/#")
    assert all(rc >= 128 for rc in reason_codes)


def test_agent_can_subscribe_downlink(agent_client):
    reason_codes = agent_client.subscribe_and_wait("downlink/#")
    assert all(rc < 128 for rc in reason_codes)


def test_agent_cannot_publish_downlink(agent_client):
    reason_code = agent_client.publish_and_wait("downlink/TestACL", "1")
    assert reason_code == RC_NOT_AUTHORIZED
