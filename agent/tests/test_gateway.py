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


class TestExtractSubDeviceRest:
    def test_extracts_rest_after_name(self):
        assert gateway.extract_sub_device_rest("sub/pump3-vibration/ds/Vibration") == "ds/Vibration"

    def test_bare_name_with_nothing_after_returns_none(self):
        assert gateway.extract_sub_device_rest("sub/pump3") is None

    def test_non_matching_prefix_returns_none(self):
        assert gateway.extract_sub_device_rest("ds/AgentCPUUsage") is None


class TestExtractGwTokenAndRest:
    def test_extracts_token_and_rest(self):
        assert gateway.extract_gw_token_and_rest("gw/TOK123/downlink/ds/motor_Speed") == (
            "TOK123", "downlink/ds/motor_Speed"
        )

    def test_bare_prefix_with_no_token_returns_none(self):
        assert gateway.extract_gw_token_and_rest("gw/") is None

    def test_token_with_nothing_after_returns_none(self):
        assert gateway.extract_gw_token_and_rest("gw/TOK123") is None

    def test_non_matching_prefix_returns_none(self):
        assert gateway.extract_gw_token_and_rest("sub/pump3/ds/Vibration") is None


class TestParseEntry:
    def test_plain_token_has_no_role(self):
        assert gateway.parse_entry("TOK123") == ("TOK123", "")

    def test_token_with_role(self):
        assert gateway.parse_entry("TOK123|motor") == ("TOK123", "motor")

    def test_placeholder_has_no_role(self):
        assert gateway.parse_entry(gateway.PLACEHOLDER_TOKEN) == (gateway.PLACEHOLDER_TOKEN, "")

    def test_placeholder_with_preset_role(self):
        assert gateway.parse_entry(f"{gateway.PLACEHOLDER_TOKEN}|motor") == (gateway.PLACEHOLDER_TOKEN, "motor")

    def test_trailing_pipe_with_empty_role_is_the_same_as_no_role(self):
        assert gateway.parse_entry("TOK123|") == ("TOK123", "")

    def test_invalid_role_characters_are_returned_unvalidated(self):
        # parse_entry itself doesn't judge the role - that's
        # build_roled_routing's job (it needs to log/skip, which this
        # low-level split shouldn't need to know about).
        assert gateway.parse_entry("TOK123|bad role!") == ("TOK123", "bad role!")


class TestBuildRoledRouting:
    def test_empty_registry_routes_nothing(self):
        routing = gateway.build_roled_routing({})
        assert routing.tokens() == set()
        assert routing.by_name == {}

    def test_plain_unroled_entry_is_not_part_of_roled_routing(self):
        routing = gateway.build_roled_routing({"pump3": "TOK123"})
        assert routing.tokens() == set()

    def test_single_roled_entry_routes(self):
        routing = gateway.build_roled_routing({"motor-sensor": "TOK123|motor"})
        assert routing.by_name == {"motor-sensor": ("TOK123", "motor")}
        assert routing.by_token == {"TOK123": {"motor": "motor-sensor"}}
        assert routing.tokens() == {"TOK123"}

    def test_two_distinct_roles_sharing_a_token_both_route(self):
        routing = gateway.build_roled_routing(
            {"motor-sensor": "TOK123|motor", "bearing-sensor": "TOK123|bearing"}
        )
        assert routing.by_name == {
            "motor-sensor": ("TOK123", "motor"),
            "bearing-sensor": ("TOK123", "bearing"),
        }
        assert routing.by_token == {"TOK123": {"motor": "motor-sensor", "bearing": "bearing-sensor"}}

    def test_placeholder_token_is_never_routed_even_with_a_role(self):
        routing = gateway.build_roled_routing({"motor-sensor": f"{gateway.PLACEHOLDER_TOKEN}|motor"})
        assert routing.tokens() == set()

    def test_invalid_role_characters_are_skipped_and_logged(self, caplog):
        routing = gateway.build_roled_routing({"motor-sensor": "TOK123|bad role!"})
        assert routing.tokens() == set()
        assert "invalid role" in caplog.text

    def test_shared_token_with_one_entry_missing_a_role_is_a_conflict(self, caplog):
        routing = gateway.build_roled_routing(
            {"motor-sensor": "TOK123|motor", "other-sensor": "TOK123"}
        )
        assert routing.tokens() == set()
        assert "no role" in caplog.text

    def test_shared_token_with_duplicate_roles_skips_only_the_conflicting_pair(self, caplog):
        routing = gateway.build_roled_routing(
            {
                "motor-sensor-1": "TOK123|motor",
                "motor-sensor-2": "TOK123|motor",
                "bearing-sensor": "TOK123|bearing",
            }
        )
        assert "motor-sensor-1" not in routing.by_name
        assert "motor-sensor-2" not in routing.by_name
        assert routing.by_name["bearing-sensor"] == ("TOK123", "bearing")
        assert "motor-sensor-1" in caplog.text and "motor-sensor-2" in caplog.text

    def test_unrelated_token_unaffected_by_a_conflict_on_another_token(self):
        routing = gateway.build_roled_routing(
            {
                "motor-sensor": "TOK123|motor",
                "other-sensor": "TOK123",  # conflict on TOK123
                "fan-sensor": "TOK456|fan",  # unaffected
            }
        )
        assert routing.tokens() == {"TOK456"}


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


class TestRenderRoledGatewayBridgeTopics:
    def test_empty_tokens_returns_empty_string(self):
        assert gateway._render_roled_gateway_bridge_topics(set()) == ""

    def test_one_token_renders_four_topic_lines_only(self):
        rendered = gateway._render_roled_gateway_bridge_topics({"TOK123"})

        assert rendered == (
            "topic downlink/# in 1 gw/TOK123/ dev/TOK123/\n"
            "topic ds/# out 1 gw/TOK123/ dev/TOK123/\n"
            "topic batch_ds out 1 gw/TOK123/ dev/TOK123/\n"
            "topic event/# out 1 gw/TOK123/ dev/TOK123/\n"
        )
        # Deliberately not forwarded for roled entries - see the module
        # docstring and agent.py's _rewrite_and_republish_uplink.
        assert "info/mcu" not in rendered
        assert "get/#" not in rendered
        assert "meta/#" not in rendered

    def test_multiple_tokens_each_get_one_block(self):
        rendered = gateway._render_roled_gateway_bridge_topics({"TOKA", "TOKB"})

        assert "gw/TOKA/ dev/TOKA/" in rendered
        assert "gw/TOKB/ dev/TOKB/" in rendered


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

    def test_unroled_only_registry_is_byte_identical_to_pre_role_rendering(self):
        # Regression test: adding role support must not change rendering
        # at all for a registry that never uses it.
        devices = {"pump3": "TOK123", "pump7": "TOK789"}
        kwargs = dict(server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
                      local_username="u", local_password="p")

        rendered = gateway.build_gateway_connection_block(devices, **kwargs)

        expected = gateway.GATEWAY_CONNECTION_TEMPLATE.format(
            server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
            sub_device_topics=gateway._render_sub_device_bridge_topics(devices),
            local_username="u", local_password="p",
        )
        assert rendered == expected
        assert "gw/" not in rendered

    def test_mixed_unroled_and_roled_registry_renders_both(self):
        devices = {"pump3": "TOK123", "motor-sensor": "TOK456|motor"}
        rendered = gateway.build_gateway_connection_block(
            devices, server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
            local_username="u", local_password="p",
        )
        assert "sub/pump3/ dev/TOK123/" in rendered
        assert "gw/TOK456/ dev/TOK456/" in rendered

    def test_two_names_sharing_a_token_render_one_gw_block(self):
        devices = {"motor-sensor": "TOK123|motor", "bearing-sensor": "TOK123|bearing"}
        rendered = gateway.build_gateway_connection_block(
            devices, server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
            local_username="u", local_password="p",
        )
        assert rendered.count("gw/TOK123/") == 4  # one 4-line block, not two

    def test_adding_a_second_name_to_an_already_routed_token_does_not_change_rendering(self):
        # This is what lets MqttBridge.ensure_current's existing byte-diff
        # restart trigger decide "no restart needed" without any special
        # casing - the roled block is keyed by the set of tokens, not by
        # which names/roles currently map to each one.
        kwargs = dict(server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
                      local_username="u", local_password="p")
        before = gateway.build_gateway_connection_block({"motor-sensor": "TOK123|motor"}, **kwargs)
        after = gateway.build_gateway_connection_block(
            {"motor-sensor": "TOK123|motor", "bearing-sensor": "TOK123|bearing"}, **kwargs
        )
        assert before == after

    def test_conflicting_roled_entries_are_excluded(self):
        devices = {"motor-sensor": "TOK123|motor", "other-sensor": "TOK123"}
        rendered = gateway.build_gateway_connection_block(
            devices, server="fra1.blynk.cloud", token="GWTOK", template_id="TMPL1",
            local_username="u", local_password="p",
        )
        assert rendered == ""


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

    def test_gw_acl_lines_present_when_capability_enabled(self, bridge, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)

        bridge.ensure_current()

        acl = agent.ACL_FILE.read_text()
        assert "topic write gw/+/ds/#" in acl
        assert "topic write gw/+/batch_ds" in acl
        assert "topic write gw/+/event/#" in acl
        assert "topic read gw/+/downlink/#" in acl
        assert "topic read gw/+/ds/#" in acl
        assert "topic read gw/+/batch_ds" in acl
        assert "topic read gw/+/event/#" in acl
        assert "topic write gw/+/downlink/#" in acl

    def test_gw_acl_lines_absent_when_capability_disabled(self, bridge, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", False)

        bridge.ensure_current()

        assert "gw/" not in agent.ACL_FILE.read_text()

    def test_gw_acl_present_even_with_no_roled_entries_yet(self, bridge, monkeypatch):
        # Static and gated purely on the capability flag, not on whether
        # any current registry entry actually has a role - so a later
        # console edit adding one doesn't need an agent restart to already
        # have permission.
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)
        bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123"}))

        bridge.ensure_current()

        assert "topic write gw/+/ds/#" in agent.ACL_FILE.read_text()

    def test_adding_second_name_to_an_already_routed_token_does_not_restart(self, bridge, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)
        bridge.sub_device_registry.load_from_payload(json.dumps({"motor-sensor": "TOK123|motor"}))
        bridge.ensure_current()
        bridge._restart_mqtt_bridge.reset_mock()

        bridge.sub_device_registry.load_from_payload(
            json.dumps({"motor-sensor": "TOK123|motor", "bearing-sensor": "TOK123|bearing"})
        )
        bridge.ensure_current()

        bridge._restart_mqtt_bridge.assert_not_called()

    def test_adding_a_new_roled_token_does_restart(self, bridge, monkeypatch):
        monkeypatch.setattr(gateway, "GATEWAY_CAPABILITY_ENABLED", True)
        bridge.sub_device_registry.load_from_payload(json.dumps({"motor-sensor": "TOK123|motor"}))
        bridge.ensure_current()
        bridge._restart_mqtt_bridge.reset_mock()

        bridge.sub_device_registry.load_from_payload(
            json.dumps({"motor-sensor": "TOK123|motor", "fan-sensor": "TOK456|fan"})
        )
        bridge.ensure_current()

        bridge._restart_mqtt_bridge.assert_called_once()


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

    def test_registry_reload_updates_roled_routing_immediately(self, agent_instance, monkeypatch):
        # Uplink rewriting must see a brand new roled entry right away -
        # not just after whatever restart ensure_current may or may not
        # trigger, since routing is recomputed fresh from the registry on
        # every lookup (see gateway.build_roled_routing), not cached.
        monkeypatch.setattr(agent_instance.bridge, "ensure_current", MagicMock())

        agent_instance._handle_sub_device_registry(json.dumps({"motor-sensor": "TOK123|motor"}))
        agent_instance._handle_sub_device_traffic("sub/motor-sensor/ds/Vibration", "42")

        agent_instance.client.publish.assert_any_call("gw/TOK123/ds/motor_Vibration", "42", qos=1)

    def test_handle_sub_device_traffic_auto_registers_unknown_name(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent_instance.bridge, "ensure_current", MagicMock())

        agent_instance._handle_sub_device_traffic("sub/warehouse-scanner-1/ds/BarcodeScanned", "42")

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

        agent_instance._handle_sub_device_traffic("sub/pump3/ds/Vibration", "42")

        agent_instance.bridge.ensure_current.assert_not_called()
        agent_instance.client.publish.assert_not_called()

    def test_handle_sub_device_traffic_auto_registers_regardless_of_payload(self, agent_instance, monkeypatch):
        # Auto-discovery must not start caring about payload content just
        # because the method's signature grew a payload parameter for the
        # new roled-rewrite path - an empty payload still registers.
        monkeypatch.setattr(agent_instance.bridge, "ensure_current", MagicMock())

        agent_instance._handle_sub_device_traffic("sub/warehouse-scanner-1/ds/BarcodeScanned", "")

        assert agent_instance.bridge.sub_device_registry.devices == {
            "warehouse-scanner-1": gateway.PLACEHOLDER_TOKEN
        }

    def test_handle_sub_device_traffic_ignores_its_own_downlink_echo(self, agent_instance, monkeypatch):
        # The agent's own sub/+/# auto-discovery subscription also receives
        # back its own downlink fan-out republishes (see
        # _handle_gw_downlink) - must not be treated as auto-discovery
        # traffic or fed into the roled-rewrite path.
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))
        monkeypatch.setattr(agent_instance.bridge, "ensure_current", MagicMock())

        agent_instance._handle_sub_device_traffic("sub/pump3/downlink/ds/Speed", "42")

        agent_instance.client.publish.assert_not_called()
        agent_instance.bridge.ensure_current.assert_not_called()

    def test_handle_sub_device_traffic_rewrites_roled_ds_uplink(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_sub_device_traffic("sub/pump3/ds/Vibration", "42")

        agent_instance.client.publish.assert_called_once_with("gw/TOK123/ds/motor_Vibration", "42", qos=1)

    def test_handle_sub_device_traffic_rewrites_roled_batch_ds_uplink(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_sub_device_traffic(
            "sub/pump3/batch_ds", json.dumps({"Vibration": 1, "Temperature": 2})
        )

        agent_instance.client.publish.assert_called_once_with(
            "gw/TOK123/batch_ds", json.dumps({"motor_Vibration": 1, "motor_Temperature": 2}), qos=1
        )

    def test_handle_sub_device_traffic_drops_malformed_batch_ds_json(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_sub_device_traffic("sub/pump3/batch_ds", "not valid json")

        agent_instance.client.publish.assert_not_called()

    def test_handle_sub_device_traffic_drops_non_object_batch_ds_json(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_sub_device_traffic("sub/pump3/batch_ds", json.dumps([1, 2, 3]))

        agent_instance.client.publish.assert_not_called()

    def test_handle_sub_device_traffic_passes_event_through_unprefixed(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_sub_device_traffic("sub/pump3/event/offline", "message")

        agent_instance.client.publish.assert_called_once_with("gw/TOK123/event/offline", "message", qos=1)

    def test_handle_sub_device_traffic_drops_info_mcu_for_roled_entry(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_sub_device_traffic("sub/pump3/info/mcu", "{}")

        agent_instance.client.publish.assert_not_called()

    def test_handle_sub_device_traffic_drops_meta_for_roled_entry(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_sub_device_traffic("sub/pump3/meta/color", "red")

        agent_instance.client.publish.assert_not_called()

    def test_handle_sub_device_traffic_logs_dropped_topic_only_once_per_name(self, agent_instance, caplog):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_sub_device_traffic("sub/pump3/info/mcu", "{}")
        agent_instance._handle_sub_device_traffic("sub/pump3/meta/color", "red")

        assert sum("isn't forwarded" in r.message for r in caplog.records) == 1

    def test_handle_sub_device_traffic_unroled_entry_is_not_rewritten(self, agent_instance):
        # Unroled traffic is bridged directly by mosquitto - the agent has
        # nothing to do with it once it's past auto-discovery.
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123"}))

        agent_instance._handle_sub_device_traffic("sub/pump3/ds/Vibration", "42")

        agent_instance.client.publish.assert_not_called()


class TestHandleGwDownlink:
    @pytest.fixture
    def agent_instance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "LOCAL_BROKER_CREDS_FILE", tmp_path / "local_broker_creds.env")
        config = MagicMock()
        config.template_id = "TMPL123"
        bridge = agent.MqttBridge(config=config)
        instance = agent.BlynkAgent(config, MagicMock(), bridge)
        instance.client = MagicMock()
        return instance

    def test_prefixed_ds_downlink_goes_only_to_the_matching_sensor(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(
            json.dumps({"motor-sensor": "TOK123|motor", "bearing-sensor": "TOK123|bearing"})
        )

        agent_instance._handle_gw_downlink("gw/TOK123/downlink/ds/motor_Speed", "100")

        agent_instance.client.publish.assert_called_once_with("sub/motor-sensor/downlink/ds/Speed", "100", qos=1)

    def test_longest_matching_role_wins(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(
            json.dumps({"bearing": "TOK123|bearing", "bearing-de": "TOK123|bearing_de"})
        )

        agent_instance._handle_gw_downlink("gw/TOK123/downlink/ds/bearing_de_temperature", "42")

        agent_instance.client.publish.assert_called_once_with("sub/bearing-de/downlink/ds/temperature", "42", qos=1)

    def test_unprefixed_ds_downlink_fans_out_to_every_sensor_on_the_token(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(
            json.dumps({"motor-sensor": "TOK123|motor", "bearing-sensor": "TOK123|bearing"})
        )

        agent_instance._handle_gw_downlink("gw/TOK123/downlink/ds/Enabled", "1")

        agent_instance.client.publish.assert_any_call("sub/motor-sensor/downlink/ds/Enabled", "1", qos=1)
        agent_instance.client.publish.assert_any_call("sub/bearing-sensor/downlink/ds/Enabled", "1", qos=1)
        assert agent_instance.client.publish.call_count == 2

    def test_non_ds_downlink_fans_out_to_every_sensor_on_the_token(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(
            json.dumps({"motor-sensor": "TOK123|motor", "bearing-sensor": "TOK123|bearing"})
        )

        agent_instance._handle_gw_downlink("gw/TOK123/downlink/reset", "")

        agent_instance.client.publish.assert_any_call("sub/motor-sensor/downlink/reset", "", qos=1)
        agent_instance.client.publish.assert_any_call("sub/bearing-sensor/downlink/reset", "", qos=1)

    def test_unknown_token_is_ignored(self, agent_instance):
        agent_instance.bridge.sub_device_registry.load_from_payload(json.dumps({"pump3": "TOK123|motor"}))

        agent_instance._handle_gw_downlink("gw/SOMEOTHERTOKEN/downlink/ds/motor_Speed", "1")

        agent_instance.client.publish.assert_not_called()

    def test_malformed_topic_is_ignored(self, agent_instance):
        agent_instance._handle_gw_downlink("gw/", "1")

        agent_instance.client.publish.assert_not_called()
