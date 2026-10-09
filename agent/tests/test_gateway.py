"""Unit tests for gateway.py (MQTT Gateway API / sub-device support) and its
integration points in agent.py - SubDeviceRegistry, bridge/ACL rendering,
MqttBridge's gateway username selection, and BlynkAgent's registry-sync /
auto-discovery handlers. No real MQTT broker or Blynk Cloud connection
needed."""

import json

from unittest.mock import MagicMock

import pytest

import agent
import gateway


class TestSubDeviceRegistryLoadFromPayload:
    def test_empty_payload_on_a_fresh_registry_is_a_no_op(self):
        reg = gateway.SubDeviceRegistry()
        assert reg.load_from_payload("") is False
        assert reg.devices == {}

    def test_empty_payload_does_not_clobber_an_already_populated_registry(self):
        # Regression test: a brand-new device's very first get/ds round-trip
        # for this datastream can come back empty (it's never been set) -
        # if a sub-device auto-registered itself (see
        # TestSubDeviceRegistryRegisterIfUnknown) while that round-trip was
        # still in flight, the empty response arriving afterward must not
        # wipe the auto-registered entry back out.
        reg = gateway.SubDeviceRegistry()
        reg.register_if_unknown("warehouse-scanner-1")

        changed = reg.load_from_payload("")

        assert changed is False
        assert reg.devices == {"warehouse-scanner-1": gateway.PLACEHOLDER_TOKEN}

    def test_explicit_empty_object_does_clear_the_registry(self):
        # Distinct from a genuinely empty payload - publishing the literal
        # two-character "{}" is a deliberate, well-formed "clear everything"
        # value, not "no data yet", so it's still applied.
        reg = gateway.SubDeviceRegistry()
        reg.load_from_payload(json.dumps({"pump3": "TOK123"}))

        changed = reg.load_from_payload("{}")

        assert changed is True
        assert reg.devices == {}

    def test_parses_name_token_pairs(self):
        reg = gateway.SubDeviceRegistry()
        changed = reg.load_from_payload(json.dumps({"pump3": "TOK123", "pump7": "TOK789"}))
        assert changed is True
        assert reg.devices == {"pump3": "TOK123", "pump7": "TOK789"}

    def test_malformed_json_does_not_raise(self):
        reg = gateway.SubDeviceRegistry()
        assert reg.load_from_payload("not valid json") is False
        assert reg.devices == {}

    def test_non_object_json_is_rejected(self):
        reg = gateway.SubDeviceRegistry()
        assert reg.load_from_payload(json.dumps(["pump3", "TOK123"])) is False
        assert reg.devices == {}

    def test_entries_missing_name_or_token_are_skipped(self):
        reg = gateway.SubDeviceRegistry()
        reg.load_from_payload(json.dumps({"pump3": "TOK123", "": "TOK789", "pump9": ""}))
        assert reg.devices == {"pump3": "TOK123"}

    def test_whitespace_is_stripped(self):
        reg = gateway.SubDeviceRegistry()
        reg.load_from_payload(json.dumps({" pump3 ": " TOK123 "}))
        assert reg.devices == {"pump3": "TOK123"}

    def test_same_content_again_is_not_a_change(self):
        reg = gateway.SubDeviceRegistry()
        reg.load_from_payload(json.dumps({"pump3": "TOK123"}))
        assert reg.load_from_payload(json.dumps({"pump3": "TOK123"})) is False

    def test_different_content_is_a_change(self):
        reg = gateway.SubDeviceRegistry()
        reg.load_from_payload(json.dumps({"pump3": "TOK123"}))
        assert reg.load_from_payload(json.dumps({"pump3": "TOK123", "pump7": "TOK789"})) is True


class TestSubDeviceRegistryToJson:
    def test_round_trips(self):
        reg = gateway.SubDeviceRegistry()
        reg.load_from_payload(json.dumps({"pump3": "TOK123"}))
        assert json.loads(reg.to_json()) == {"pump3": "TOK123"}


class TestSubDeviceRegistryRegisterIfUnknown:
    def test_adds_new_name_with_placeholder_token(self):
        reg = gateway.SubDeviceRegistry()
        assert reg.register_if_unknown("warehouse-scanner-1") is True
        assert reg.devices == {"warehouse-scanner-1": gateway.PLACEHOLDER_TOKEN}

    def test_known_name_is_a_no_op(self):
        reg = gateway.SubDeviceRegistry()
        reg.load_from_payload(json.dumps({"pump3": "TOK123"}))
        assert reg.register_if_unknown("pump3") is False
        assert reg.devices == {"pump3": "TOK123"}

    def test_already_placeholder_name_is_a_no_op(self):
        reg = gateway.SubDeviceRegistry()
        reg.register_if_unknown("pump3")
        assert reg.register_if_unknown("pump3") is False

    def test_refuses_when_it_would_exceed_the_length_cap(self, monkeypatch):
        reg = gateway.SubDeviceRegistry()
        monkeypatch.setattr(gateway, "MAX_REGISTRY_JSON_LENGTH", 10)
        assert reg.register_if_unknown("a-very-long-sub-device-name") is False
        assert reg.devices == {}


class TestExtractSubDeviceName:
    def test_extracts_name_from_topic(self):
        assert gateway.extract_sub_device_name("sub/pump3-vibration/ds/Vibration") == "pump3-vibration"

    def test_non_matching_prefix_returns_none(self):
        assert gateway.extract_sub_device_name("ds/AgentCPUUsage") is None

    def test_bare_prefix_with_no_name_returns_none(self):
        assert gateway.extract_sub_device_name("sub/") is None


class TestRenderSubDeviceBridgeTopics:
    def test_empty_devices_returns_empty_string(self):
        assert gateway._render_sub_device_bridge_topics({}) == ""

    def test_one_device_renders_all_seven_topic_lines(self):
        rendered = gateway._render_sub_device_bridge_topics({"pump3": "TOK123"})

        assert rendered == (
            "topic downlink/# in 1 sub/pump3/ dev/TOK123/\n"
            "topic ds/# out 1 sub/pump3/ dev/TOK123/\n"
            "topic batch_ds out 1 sub/pump3/ dev/TOK123/\n"
            "topic info/mcu out 1 sub/pump3/ dev/TOK123/\n"
            "topic event/# out 1 sub/pump3/ dev/TOK123/\n"
            "topic get/# out 1 sub/pump3/ dev/TOK123/\n"
            "topic meta/# out 1 sub/pump3/ dev/TOK123/\n"
        )

    def test_multiple_devices_each_get_their_own_block(self):
        rendered = gateway._render_sub_device_bridge_topics({"pump3": "TOKA", "pump7": "TOKB"})

        assert "sub/pump3/ dev/TOKA/" in rendered
        assert "sub/pump7/ dev/TOKB/" in rendered


class TestBuildGatewayConnectionBlock:
    def test_empty_devices_returns_empty_string(self):
        assert gateway.build_gateway_connection_block(
            {}, server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
            local_username="u", local_password="p",
        ) == ""

    def test_placeholder_only_registry_returns_empty_string(self):
        devices = {"new-scanner": gateway.PLACEHOLDER_TOKEN}
        assert gateway.build_gateway_connection_block(
            devices, server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
            local_username="u", local_password="p",
        ) == ""

    def test_real_device_renders_connection_block(self):
        rendered = gateway.build_gateway_connection_block(
            {"pump3": "TOK123"}, server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
            local_username="u", local_password="p",
        )
        assert "connection blynk-gateway" in rendered
        assert "remote_username mgmt_device" in rendered
        assert "sub/pump3/ dev/TOK123/" in rendered

    def test_placeholder_entries_are_excluded_from_a_mixed_registry(self):
        devices = {"pump3": "TOK123", "new-scanner": gateway.PLACEHOLDER_TOKEN}
        rendered = gateway.build_gateway_connection_block(
            devices, server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
            local_username="u", local_password="p",
        )
        assert "sub/pump3/" in rendered
        assert "new-scanner" not in rendered


class TestMqttBridgeEnsureCurrentGatewayUsername:
    """Regression coverage for the separate gateway bridge connection: it's
    only rendered (as its own "connection blynk-gateway" block, mgmt_device
    auth) when the capability is enabled AND the registry actually has at
    least one real (non-placeholder) device - never from the capability
    flag alone."""

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
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", False)
        bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123"}))

        bridge.ensure_current()

        assert "remote_username device" in agent.BRIDGE_CONF_FILE.read_text()

    def test_remote_username_stays_device_when_no_devices_registered(self, bridge, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)

        bridge.ensure_current()

        assert "remote_username device" in agent.BRIDGE_CONF_FILE.read_text()

    def test_remote_username_stays_device_for_placeholder_only_registry(self, bridge, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)
        bridge.sub_device_registry.register_if_unknown("new-scanner")

        bridge.ensure_current()

        assert "remote_username device" in agent.BRIDGE_CONF_FILE.read_text()
        assert "remote_username mgmt_device" not in agent.BRIDGE_CONF_FILE.read_text()

    def test_remote_username_switches_when_enabled_and_devices_registered(self, bridge, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)
        bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123"}))

        bridge.ensure_current()

        content = agent.BRIDGE_CONF_FILE.read_text()
        assert "remote_username mgmt_device" in content
        assert "sub/pump3/ dev/TOK123/" in content

    def test_gateway_block_is_a_separate_connection_not_merged_into_primary(self, bridge, monkeypatch):
        # Regression test for the real-hardware mosquitto bug: a bare
        # ("topic ds/# out 1") entry and a prefixed one for the same
        # topic/direction/qos on the SAME connection get silently
        # deduplicated, skipping the prefixed entry entirely. Keeping two
        # distinct "connection" blocks avoids that; this pins both blocks'
        # presence and that the primary's own topics stay unprefixed.
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)
        bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123"}))

        bridge.ensure_current()

        content = agent.BRIDGE_CONF_FILE.read_text()
        assert content.count("connection blynk-cloud") == 1
        assert content.count("connection blynk-gateway") == 1
        assert "topic ds/# out 1\n" in content  # primary's own, unprefixed
        assert "topic ds/# out 1 sub/pump3/ dev/TOK123/" in content

    def test_acl_does_not_depend_on_registry_content(self, bridge, monkeypatch):
        # The sub-device ACL lines are static (gated only by
        # GATEWAY_CAPABILITY_ENABLED) - adding/removing a specific
        # sub-device must not trigger an ACL rewrite/restart on its own.
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)
        bridge.ensure_current()
        acl_after_first = agent.ACL_FILE.read_text()

        bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123"}))
        bridge.ensure_current()
        acl_after_second = agent.ACL_FILE.read_text()

        assert acl_after_first == acl_after_second
        assert "sub/+/ds/#" in acl_after_first


class TestBlynkAgentGatewayIntegration:
    @pytest.fixture
    def agent_instance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "LOCAL_BROKER_CREDS_FILE", tmp_path / "local_broker_creds.env")
        config = MagicMock()
        config.template_id = "TMPL123"
        bridge = agent.MqttBridge(config=config)
        instance = agent.BlynkAgent(config, MagicMock(), bridge)
        instance.client = MagicMock()
        monkeypatch.setattr(instance, "_publish_device_info", MagicMock())
        monkeypatch.setattr(instance, "_publish_system_info", MagicMock())
        monkeypatch.setattr(instance, "_probe_cloud_reachable", MagicMock(return_value=False))
        return instance

    def test_on_connect_requests_registry_and_subscribes_when_enabled(self, agent_instance, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)

        agent_instance._on_connect(agent_instance.client, None, None, 0, None)

        agent_instance.client.publish.assert_any_call("get/ds", "AgentSubDeviceRegistry", qos=1)
        agent_instance.client.subscribe.assert_any_call("sub/+/#", qos=1)

    def test_on_connect_does_nothing_gateway_related_when_disabled(self, agent_instance, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", False)

        agent_instance._on_connect(agent_instance.client, None, None, 0, None)

        for call in agent_instance.client.publish.call_args_list:
            assert call.args[:2] != ("get/ds", "AgentSubDeviceRegistry")
        for call in agent_instance.client.subscribe.call_args_list:
            assert call.args[0] != "sub/+/#"

    def test_handle_sub_device_registry_reapplies_on_change(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent_instance.bridge, "ensure_current", MagicMock())

        agent_instance._handle_sub_device_registry(json.dumps({"pump3": "TOK123"}))

        assert agent_instance.bridge.sub_device_registry.devices == {"pump3": "TOK123"}
        agent_instance.bridge.ensure_current.assert_called_once_with(force_restart=True)

    def test_handle_sub_device_registry_skips_reapply_when_unchanged(self, agent_instance, monkeypatch):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123"}))
        monkeypatch.setattr(agent_instance.bridge, "ensure_current", MagicMock())

        agent_instance._handle_sub_device_registry(json.dumps({"pump3": "TOK123"}))

        agent_instance.bridge.ensure_current.assert_not_called()

    def test_handle_sub_device_traffic_auto_registers_unknown_name(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent_instance.bridge, "ensure_current", MagicMock())

        agent_instance._handle_sub_device_traffic("sub/warehouse-scanner-1/ds/BarcodeScanned")

        assert agent_instance.bridge.sub_device_registry.devices == {
            "warehouse-scanner-1": gateway.PLACEHOLDER_TOKEN
        }
        agent_instance.client.publish.assert_any_call(
            "ds/AgentSubDeviceRegistry",
            json.dumps({"warehouse-scanner-1": gateway.PLACEHOLDER_TOKEN}),
            qos=1,
        )
        agent_instance.bridge.ensure_current.assert_called_once_with(force_restart=True)

    def test_handle_sub_device_traffic_known_name_is_a_no_op(self, agent_instance, monkeypatch):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123"}))
        monkeypatch.setattr(agent_instance.bridge, "ensure_current", MagicMock())

        agent_instance._handle_sub_device_traffic("sub/pump3/ds/Vibration")

        agent_instance.bridge.ensure_current.assert_not_called()
        agent_instance.client.publish.assert_not_called()
