"""Unit tests for the MQTT Gateway API support (remote/managed devices) -
REMOTE_DEVICES_FILE loading, bridge/ACL rendering, and MqttBridge.ensure_current's
gateway username selection. No real MQTT broker or Blynk Cloud connection needed."""

import json

from unittest.mock import MagicMock

import pytest

import agent


class TestLoadRemoteDevices:
    def test_missing_file_returns_empty_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "REMOTE_DEVICES_FILE", tmp_path / "remote_devices.json")

        assert agent._load_remote_devices() == {}

    def test_parses_name_token_pairs(self, tmp_path, monkeypatch):
        path = tmp_path / "remote_devices.json"
        path.write_text(json.dumps({"pump3": "TOK123", "pump7": "TOK789"}))
        monkeypatch.setattr(agent, "REMOTE_DEVICES_FILE", path)

        assert agent._load_remote_devices() == {"pump3": "TOK123", "pump7": "TOK789"}

    def test_malformed_json_does_not_raise(self, tmp_path, monkeypatch):
        path = tmp_path / "remote_devices.json"
        path.write_text("not valid json")
        monkeypatch.setattr(agent, "REMOTE_DEVICES_FILE", path)

        assert agent._load_remote_devices() == {}

    def test_non_object_json_returns_empty_dict(self, tmp_path, monkeypatch):
        path = tmp_path / "remote_devices.json"
        path.write_text(json.dumps(["pump3", "TOK123"]))
        monkeypatch.setattr(agent, "REMOTE_DEVICES_FILE", path)

        assert agent._load_remote_devices() == {}

    def test_entries_missing_name_or_token_are_skipped(self, tmp_path, monkeypatch):
        path = tmp_path / "remote_devices.json"
        path.write_text(json.dumps({"pump3": "TOK123", "": "TOK789", "pump9": ""}))
        monkeypatch.setattr(agent, "REMOTE_DEVICES_FILE", path)

        assert agent._load_remote_devices() == {"pump3": "TOK123"}

    def test_whitespace_around_values_is_stripped(self, tmp_path, monkeypatch):
        path = tmp_path / "remote_devices.json"
        path.write_text(json.dumps({" pump3 ": " TOK123 "}))
        monkeypatch.setattr(agent, "REMOTE_DEVICES_FILE", path)

        assert agent._load_remote_devices() == {"pump3": "TOK123"}


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


class TestMqttBridgeEnsureCurrentGatewayUsername:
    """Regression coverage for the gateway username selection: mgmt_device
    only when the capability is enabled AND REMOTE_DEVICES_FILE actually
    lists at least one device - never from the capability flag alone."""

    @pytest.fixture
    def bridge(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "BRIDGE_CONF_DIR", tmp_path / "conf.d")
        monkeypatch.setattr(agent, "BRIDGE_CONF_FILE", tmp_path / "conf.d" / "blynk-bridge.conf")
        monkeypatch.setattr(agent, "ACL_FILE", tmp_path / "conf.d" / "acl.rules")
        monkeypatch.setattr(agent, "LOCAL_BROKER_CREDS_FILE", tmp_path / "local_broker_creds.env")
        monkeypatch.setattr(agent, "REMOTE_DEVICES_FILE", tmp_path / "remote_devices.json")
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
        agent.REMOTE_DEVICES_FILE.write_text(json.dumps({"pump3": "TOK123"}))

        bridge.ensure_current()

        assert "remote_username device" in agent.BRIDGE_CONF_FILE.read_text()

    def test_remote_username_stays_device_when_no_devices_registered(self, bridge, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", True)
        # No REMOTE_DEVICES_FILE written at all.

        bridge.ensure_current()

        assert "remote_username device" in agent.BRIDGE_CONF_FILE.read_text()

    def test_remote_username_switches_when_enabled_and_devices_registered(self, bridge, monkeypatch):
        monkeypatch.setattr(agent, "GATEWAY_CAPABILITY_ENABLED", True)
        agent.REMOTE_DEVICES_FILE.write_text(json.dumps({"pump3": "TOK123"}))

        bridge.ensure_current()

        content = agent.BRIDGE_CONF_FILE.read_text()
        assert "remote_username mgmt_device" in content
        assert "remote/pump3/ dev/TOK123/" in content
