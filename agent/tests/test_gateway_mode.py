"""Unit tests for the MQTT Gateway API support (remote/managed devices) -
CSV parsing, bridge/ACL rendering, MqttBridge's gateway_confirmed
sequencing, and BlynkAgent's metadata fetch/retry handling. No real MQTT
broker or Blynk Cloud connection needed."""

from unittest.mock import MagicMock

import pytest

import agent


class TestParseRemoteDevicesCsv:
    def test_parses_name_and_authtoken_columns(self):
        result = agent._parse_remote_devices_csv("Name,AuthToken\npump3,TOK123\npump7,TOK789\n")

        assert result == {"pump3": "TOK123", "pump7": "TOK789"}

    def test_console_default_headers_with_index_column(self):
        # Direct regression test: confirmed on real hardware that the
        # Blynk console's own Table metadata editor creates an "Index"
        # column too - must be ignored, not mistaken for data.
        result = agent._parse_remote_devices_csv(
            "Index,Name,AuthToken\n0,AntsGatewayDevice1,63cH4PjtDdVhLlCOt_jEAdlSUUN_B8Ag\n"
        )

        assert result == {"AntsGatewayDevice1": "63cH4PjtDdVhLlCOt_jEAdlSUUN_B8Ag"}

    def test_empty_payload_returns_empty_dict(self):
        assert agent._parse_remote_devices_csv("") == {}

    def test_header_only_returns_empty_dict(self):
        assert agent._parse_remote_devices_csv("Name,AuthToken\n") == {}

    def test_row_missing_name_or_token_is_skipped(self):
        result = agent._parse_remote_devices_csv(
            "Name,AuthToken\npump3,TOK123\n,TOK789\npump9,\n"
        )

        assert result == {"pump3": "TOK123"}

    def test_whitespace_around_values_is_stripped(self):
        result = agent._parse_remote_devices_csv("Name,AuthToken\n pump3 , TOK123 \n")

        assert result == {"pump3": "TOK123"}

    def test_malformed_payload_does_not_raise(self):
        # Admin-edited console data, not something to crash the agent over.
        assert agent._parse_remote_devices_csv("not,even,close\nto,a,real,header") == {}

    def test_wrong_column_names_find_nothing_rather_than_guessing(self):
        # Deliberate: a table created with different column names doesn't
        # parse, by design, rather than trying to guess at alternate
        # spellings - see the function's own comment.
        result = agent._parse_remote_devices_csv("name,token\npump3,TOK123\n")

        assert result == {}


class TestRenderRemoteBridgeTopics:
    def test_empty_devices_returns_empty_string(self):
        assert agent._render_remote_bridge_topics({}) == ""

    def test_one_device_renders_all_seven_topic_lines(self):
        rendered = agent._render_remote_bridge_topics({"pump3": "TOK123"})

        assert rendered == (
            "topic downlink/# in 1 remote/pump3/ dev/TOK123/\n"
            "topic ds/# out 1 remote/pump3/ dev/TOK123/\n"
            "topic batch_ds out 1 remote/pump3/ dev/TOK123/\n"
            "topic info/mcu out 1 remote/pump3/ dev/TOK123/\n"
            "topic event/# out 1 remote/pump3/ dev/TOK123/\n"
            "topic get/# out 1 remote/pump3/ dev/TOK123/\n"
            "topic meta/# out 1 remote/pump3/ dev/TOK123/\n"
        )

    def test_multiple_devices_each_get_their_own_block(self):
        rendered = agent._render_remote_bridge_topics({"pump3": "TOKA", "pump7": "TOKB"})

        assert "remote/pump3/ dev/TOKA/" in rendered
        assert "remote/pump7/ dev/TOKB/" in rendered


class TestRenderRemoteAclBlock:
    def test_empty_devices_returns_empty_string(self):
        assert agent._render_remote_acl_block({}, "read") == ""

    def test_downlink_permission_is_applied(self):
        read_block = agent._render_remote_acl_block({"pump3": "TOK123"}, "read")
        write_block = agent._render_remote_acl_block({"pump3": "TOK123"}, "write")

        assert "topic read remote/pump3/downlink/#" in read_block
        assert "topic write remote/pump3/downlink/#" in write_block

    def test_general_topics_are_always_readwrite(self):
        rendered = agent._render_remote_acl_block({"pump3": "TOK123"}, "read")

        assert "topic readwrite remote/pump3/ds/#" in rendered
        assert "topic readwrite remote/pump3/batch_ds" in rendered
        assert "topic readwrite remote/pump3/info/mcu" in rendered
        assert "topic readwrite remote/pump3/event/#" in rendered
        assert "topic readwrite remote/pump3/get/#" in rendered
        assert "topic readwrite remote/pump3/meta/#" in rendered


class TestMqttBridgeUpdateRemoteDevices:
    def _bridge(self):
        return agent.MqttBridge(config=MagicMock())

    def test_first_confirmation_is_always_a_change_even_with_zero_devices(self):
        bridge = self._bridge()

        changed = bridge.update_remote_devices({})

        assert changed is True
        assert bridge.gateway_confirmed is True
        assert bridge.remote_devices == {}

    def test_same_devices_again_is_not_a_change(self):
        bridge = self._bridge()
        bridge.update_remote_devices({"pump3": "TOK123"})

        changed = bridge.update_remote_devices({"pump3": "TOK123"})

        assert changed is False

    def test_different_devices_is_a_change(self):
        bridge = self._bridge()
        bridge.update_remote_devices({"pump3": "TOK123"})

        changed = bridge.update_remote_devices({"pump3": "TOK123", "pump7": "TOK789"})

        assert changed is True
        assert bridge.remote_devices == {"pump3": "TOK123", "pump7": "TOK789"}


class TestMqttBridgeEnsureCurrentGatewayUsername:
    """Direct regression coverage for the fail-safe sequencing: switching to
    mgmt_device must never happen just because AGENT_GATEWAY_ENABLED is set
    locally - only after a confirmed metadata round-trip (gateway_confirmed),
    since a wrongly-rejected mgmt_device connection would take down the
    bridge's entire connection, not just gateway traffic."""

    @pytest.fixture
    def bridge(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "BRIDGE_CONF_DIR", tmp_path / "conf.d")
        monkeypatch.setattr(agent, "BRIDGE_CONF_FILE", tmp_path / "conf.d" / "blynk-bridge.conf")
        monkeypatch.setattr(agent, "ACL_FILE", tmp_path / "conf.d" / "acl.rules")
        monkeypatch.setattr(agent, "LOCAL_BROKER_CREDS_FILE", tmp_path / "local_broker_creds.env")
        monkeypatch.setattr(agent.os, "chown", MagicMock(), raising=False)
        config = MagicMock()
        config.effective_server.return_value = "fra1.blynk.cloud"
        config.auth_token = "GATEWAYTOKEN"
        config.template_id = "TMPL123"
        instance = agent.MqttBridge(config=config)
        monkeypatch.setattr(instance, "_restart_mqtt_bridge", MagicMock())
        return instance

    def test_remote_username_stays_device_when_capability_disabled(self, bridge, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", False)
        bridge.update_remote_devices({"pump3": "TOK123"})  # confirmed, but capability is off

        bridge.ensure_current()

        assert "remote_username device" in agent.BRIDGE_CONF_FILE.read_text()

    def test_remote_username_stays_device_when_not_yet_confirmed(self, bridge, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", True)
        # gateway_confirmed is still False - no metadata round-trip yet.

        bridge.ensure_current()

        assert "remote_username device" in agent.BRIDGE_CONF_FILE.read_text()

    def test_remote_username_switches_once_confirmed_and_enabled(self, bridge, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", True)
        bridge.update_remote_devices({"pump3": "TOK123"})

        bridge.ensure_current()

        content = agent.BRIDGE_CONF_FILE.read_text()
        assert "remote_username mgmt_device" in content
        assert "remote/pump3/ dev/TOK123/" in content


class TestBlynkAgentGatewayMetadataFetch:
    @pytest.fixture
    def agent_instance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "LOCAL_BROKER_CREDS_FILE", tmp_path / "local_broker_creds.env")
        config = MagicMock()
        config.template_id = "TMPL123"
        bridge = MagicMock()
        bridge.gateway_confirmed = False
        instance = agent.BlynkAgent(config, MagicMock(), bridge)
        instance.client = MagicMock()
        monkeypatch.setattr(instance, "_publish_device_info", MagicMock())
        monkeypatch.setattr(instance, "_publish_system_info", MagicMock())
        monkeypatch.setattr(instance, "_probe_cloud_reachable", MagicMock(return_value=False))
        return instance

    def test_on_connect_requests_metadata_when_capability_enabled(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", True)

        agent_instance._on_connect(agent_instance.client, None, None, 0, None)

        agent_instance.client.publish.assert_any_call("get/meta", "RemoteDevices", qos=1)

    def test_on_connect_does_not_request_metadata_when_capability_disabled(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", False)

        agent_instance._on_connect(agent_instance.client, None, None, 0, None)

        for call in agent_instance.client.publish.call_args_list:
            assert call.args[:2] != ("get/meta", "RemoteDevices")

    def test_handle_remote_devices_meta_updates_bridge_and_reapplies(self, agent_instance):
        agent_instance.bridge.update_remote_devices.return_value = True

        agent_instance._handle_remote_devices_meta("Name,AuthToken\npump3,TOK123\n")

        agent_instance.bridge.update_remote_devices.assert_called_once_with({"pump3": "TOK123"})
        agent_instance.bridge.ensure_current.assert_called_once_with(force_restart=True)

    def test_handle_remote_devices_meta_skips_reapply_when_unchanged(self, agent_instance):
        agent_instance.bridge.update_remote_devices.return_value = False

        agent_instance._handle_remote_devices_meta("Name,AuthToken\npump3,TOK123\n")

        agent_instance.bridge.ensure_current.assert_not_called()

    def test_retry_sends_request_while_unconfirmed_and_connected(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", True)
        agent_instance.bridge.gateway_confirmed = False
        agent_instance._connected = True

        agent_instance._retry_gateway_metadata_if_unconfirmed()

        agent_instance.client.publish.assert_called_once_with("get/meta", "RemoteDevices", qos=1)

    def test_retry_does_nothing_once_confirmed(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", True)
        agent_instance.bridge.gateway_confirmed = True
        agent_instance._connected = True

        agent_instance._retry_gateway_metadata_if_unconfirmed()

        agent_instance.client.publish.assert_not_called()

    def test_retry_does_nothing_when_capability_disabled(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", False)
        agent_instance.bridge.gateway_confirmed = False
        agent_instance._connected = True

        agent_instance._retry_gateway_metadata_if_unconfirmed()

        agent_instance.client.publish.assert_not_called()

    def test_retry_does_nothing_while_disconnected(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", True)
        agent_instance.bridge.gateway_confirmed = False
        agent_instance._connected = False

        agent_instance._retry_gateway_metadata_if_unconfirmed()

        agent_instance.client.publish.assert_not_called()
