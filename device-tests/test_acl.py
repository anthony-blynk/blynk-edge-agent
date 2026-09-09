"""Verifies the local broker's ACL (see agent/agent.py's ACL_TEMPLATE and
the README's Security section): ordinary local traffic stays anonymous,
while downlink/# is restricted to the bridge (write-only) and agent
(read-only) identities. Run on the device itself - see README.md.

Read access is checked via actual message delivery, not the SUBACK reason
code - confirmed on real hardware that mosquitto grants the SUBACK for a
wildcard subscription like `downlink/#` regardless of ACL, and instead
filters which individual messages get delivered. Write access (PUBACK
reason codes) is a reliable signal and checked directly."""

from conftest import RC_NOT_AUTHORIZED, RC_SUCCESS


def test_anonymous_can_publish_ds(anon_client):
    reason_code = anon_client.publish_and_wait("ds/TestACL", "1")
    assert reason_code == RC_SUCCESS


def test_anonymous_can_read_ds(anon_client):
    anon_client.subscribe_and_wait("ds/TestACL")
    anon_client.publish_and_wait("ds/TestACL", "read-check")
    message = anon_client.wait_for_message()
    assert message is not None
    assert message.payload == b"read-check"


def test_anonymous_cannot_publish_downlink(anon_client):
    reason_code = anon_client.publish_and_wait("downlink/TestACL", "1")
    assert reason_code == RC_NOT_AUTHORIZED


def test_anonymous_cannot_read_downlink(anon_client, bridge_client):
    anon_client.subscribe_and_wait("downlink/#")
    bridge_client.publish_and_wait("downlink/TestACL", "delivery-check")
    assert anon_client.wait_for_message() is None


def test_bridge_can_publish_downlink(bridge_client):
    reason_code = bridge_client.publish_and_wait("downlink/TestACL", "1")
    assert reason_code == RC_SUCCESS


def test_bridge_cannot_read_downlink(bridge_client):
    # Bridge is the only identity with write access to downlink/#, so its
    # own read restriction can only be checked against its own publish -
    # a broker enforcing read ACL per-subscriber independently of who
    # published should still withhold delivery here.
    bridge_client.subscribe_and_wait("downlink/#")
    bridge_client.publish_and_wait("downlink/TestACL", "delivery-check")
    assert bridge_client.wait_for_message() is None


def test_agent_can_read_downlink(bridge_client, agent_client):
    agent_client.subscribe_and_wait("downlink/#")
    bridge_client.publish_and_wait("downlink/TestACL", "delivery-check")
    message = agent_client.wait_for_message()
    assert message is not None
    assert message.payload == b"delivery-check"


def test_agent_cannot_publish_downlink(agent_client):
    reason_code = agent_client.publish_and_wait("downlink/TestACL", "1")
    assert reason_code == RC_NOT_AUTHORIZED
