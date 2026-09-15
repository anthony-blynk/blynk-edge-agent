"""Unit tests for BlynkAgent - the _on_message dispatch table and the
connectivity watchdog (_handle_bridge_state / _check_connectivity_watchdog).
No real MQTT broker - the paho Client object is constructed but never
connected, exactly as __init__ leaves it."""

import time
from unittest.mock import MagicMock

import pytest

import agent


class FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload


@pytest.fixture
def agent_instance(tmp_path, monkeypatch):
    # _setup_mqtt_client() reads/writes this on construction - point it at
    # a scratch path so tests don't touch a real /opt/blynk.
    monkeypatch.setattr(agent, "LOCAL_BROKER_CREDS_FILE", tmp_path / "local_broker_creds.env")
    config = MagicMock()
    config.template_id = "TMPL123"
    compose_manager = MagicMock()
    bridge = MagicMock()
    instance = agent.BlynkAgent(config, compose_manager, bridge)
    instance.client = MagicMock()  # never actually connect to a broker
    return instance


class TestOnMessageDispatch:
    def test_ota_topic_dispatches(self, agent_instance, monkeypatch):
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_ota_update", handler)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_OTA, '{"url": "http://x"}'))

        handler.assert_called_once_with('{"url": "http://x"}')

    def test_ping_topic_updates_last_cloud_contact(self, agent_instance):
        assert agent_instance.last_cloud_contact is None

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_PING, ""))

        assert agent_instance.last_cloud_contact is not None

    def test_reboot_topic_dispatches(self, agent_instance, monkeypatch):
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_reboot", handler)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_REBOOT, "now"))

        handler.assert_called_once_with("now")

    def test_redirect_topic_dispatches(self, agent_instance, monkeypatch):
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_redirect", handler)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_REDIRECT, "mqtts://new-host:8883"))

        handler.assert_called_once_with("mqtts://new-host:8883")

    def test_reconfigure_topic_dispatches(self, agent_instance, monkeypatch):
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_reconfigure", handler)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_RECONFIGURE, ""))

        handler.assert_called_once()

    def test_diagnostics_enabled_topic_dispatches(self, agent_instance, monkeypatch):
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_diagnostics_enabled", handler)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_DIAGNOSTICS_ENABLED, "1"))

        handler.assert_called_once_with("1")

    def test_bridge_state_topic_dispatches(self, agent_instance, monkeypatch):
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_bridge_state", handler)

        agent_instance._on_message(None, None, FakeMessage(agent_instance._bridge_state_topic, "1"))

        handler.assert_called_once_with("1")

    def test_unhandled_topic_does_nothing(self, agent_instance):
        # Must not raise, must not touch client.publish.
        agent_instance._on_message(None, None, FakeMessage("some/random/topic", "whatever"))
        agent_instance.client.publish.assert_not_called()

    def test_invalid_utf8_payload_does_not_raise(self, agent_instance):
        bad_message = FakeMessage(agent.TOPIC_PING, b"\xff\xfe\x00invalid")
        agent_instance._on_message(None, None, bad_message)  # must not raise


class TestTerminalDispatch:
    def test_terminal_command_dispatches_when_capability_enabled(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "TERMINAL_CAPABILITY_ENABLED", True)
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_terminal_command", handler)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_TERMINAL, "ls"))

        handler.assert_called_once_with("ls")

    def test_terminal_command_replies_disabled_when_capability_off(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "TERMINAL_CAPABILITY_ENABLED", False)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_TERMINAL, "ls"))

        agent_instance.client.publish.assert_called_once()
        topic, payload = agent_instance.client.publish.call_args[0][:2]
        assert topic == "ds/AgentTerminal"
        assert "disabled" in payload

    def test_terminal_enabled_topic_ignored_when_capability_off(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "TERMINAL_CAPABILITY_ENABLED", False)
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_terminal_enabled", handler)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_TERMINAL_ENABLED, "1"))

        handler.assert_not_called()

    def test_terminal_enabled_topic_dispatches_when_capability_on(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent, "TERMINAL_CAPABILITY_ENABLED", True)
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_terminal_enabled", handler)

        agent_instance._on_message(None, None, FakeMessage(agent.TOPIC_TERMINAL_ENABLED, "1"))

        handler.assert_called_once_with("1")

    def test_handle_terminal_command_replies_disabled_when_session_off(self, agent_instance):
        agent_instance.terminal_session_enabled = False

        agent_instance._handle_terminal_command("ls")

        agent_instance.client.publish.assert_called_once_with("ds/AgentTerminal", "[terminal disabled]", qos=1)


class TestDiagnosticsEnabledToggle:
    def test_enable(self, agent_instance):
        agent_instance._handle_diagnostics_enabled("1")
        assert agent_instance.diagnostics_enabled is True

    def test_disable(self, agent_instance):
        agent_instance._handle_diagnostics_enabled("0")
        assert agent_instance.diagnostics_enabled is False


class TestBridgeStateAndWatchdog:
    """Direct regression coverage for two real bugs found and fixed this
    session: the watchdog defaulting to None (never engaging on a bridge
    that's never once connected - see the long comment on
    _bridge_disconnected_since's initialization) and the tiered DNS-refresh
    recovery added on top of the original full-reprovisioning fallback."""

    def test_starts_as_disconnected_since_construction(self, agent_instance):
        # Not None - see __init__'s own comment for why this default matters.
        assert agent_instance._bridge_disconnected_since is not None

    def test_receiving_connected_clears_the_outage(self, agent_instance):
        agent_instance._bridge_disconnected_since = 12345.0
        agent_instance._bridge_dns_refresh_attempted = True

        agent_instance._handle_bridge_state("1")

        assert agent_instance._bridge_disconnected_since is None
        assert agent_instance._bridge_dns_refresh_attempted is False

    def test_receiving_disconnected_starts_the_clock_only_once(self, agent_instance):
        agent_instance._bridge_disconnected_since = None

        agent_instance._handle_bridge_state("0")
        first_timestamp = agent_instance._bridge_disconnected_since
        assert first_timestamp is not None

        # A second "0" while already disconnected must NOT reset the clock -
        # otherwise a bridge that keeps reporting itself down would never
        # actually reach either grace period.
        agent_instance._handle_bridge_state("0")
        assert agent_instance._bridge_disconnected_since == first_timestamp

    def test_watchdog_does_nothing_while_connected(self, agent_instance):
        agent_instance._bridge_disconnected_since = None

        agent_instance._check_connectivity_watchdog()

        agent_instance.bridge.refresh_dns.assert_not_called()

    def test_watchdog_does_nothing_while_reprovisioning(self, agent_instance):
        agent_instance._bridge_disconnected_since = time.time() - 10000
        agent_instance._reprovisioning = True

        agent_instance._check_connectivity_watchdog()

        agent_instance.bridge.refresh_dns.assert_not_called()

    def test_watchdog_does_not_fire_before_dns_refresh_threshold(self, agent_instance):
        agent_instance._bridge_disconnected_since = time.time() - 10  # well under 90s

        agent_instance._check_connectivity_watchdog()

        agent_instance.bridge.refresh_dns.assert_not_called()

    def test_watchdog_fires_dns_refresh_once_past_threshold(self, agent_instance):
        agent_instance._bridge_disconnected_since = time.time() - (agent.BRIDGE_DNS_REFRESH_GRACE_PERIOD + 1)

        agent_instance._check_connectivity_watchdog()

        agent_instance.bridge.refresh_dns.assert_called_once()
        assert agent_instance._bridge_dns_refresh_attempted is True

    def test_watchdog_does_not_refresh_dns_twice_for_same_outage(self, agent_instance):
        agent_instance._bridge_disconnected_since = time.time() - (agent.BRIDGE_DNS_REFRESH_GRACE_PERIOD + 1)

        agent_instance._check_connectivity_watchdog()
        agent_instance._check_connectivity_watchdog()  # second tick, same outage

        agent_instance.bridge.refresh_dns.assert_called_once()

    def test_watchdog_reprovisions_past_full_grace_period(self, agent_instance, monkeypatch):
        start_reprovisioning = MagicMock()
        monkeypatch.setattr(agent_instance, "_start_reprovisioning", start_reprovisioning)
        agent_instance._bridge_disconnected_since = time.time() - (agent.BRIDGE_DISCONNECT_GRACE_PERIOD + 1)

        agent_instance._check_connectivity_watchdog()

        start_reprovisioning.assert_called_once()

    def test_watchdog_does_not_reprovision_before_full_grace_period(self, agent_instance, monkeypatch):
        start_reprovisioning = MagicMock()
        monkeypatch.setattr(agent_instance, "_start_reprovisioning", start_reprovisioning)
        # past the DNS-refresh threshold but not the full reprovisioning one
        agent_instance._bridge_disconnected_since = time.time() - (agent.BRIDGE_DNS_REFRESH_GRACE_PERIOD + 1)

        agent_instance._check_connectivity_watchdog()

        start_reprovisioning.assert_not_called()


class TestHandleRedirect:
    def test_empty_payload_ignored(self, agent_instance):
        agent_instance._handle_redirect("   ")

        agent_instance.bridge.apply_redirect.assert_not_called()

    def test_plain_mqtts_uri_extracts_hostname(self, agent_instance):
        # Confirmed on real hardware: Blynk Cloud's actual redirect payload
        # is a plain mqtts://host:port URI, not JSON.
        agent_instance._handle_redirect("mqtts://fra1.blynk.cloud:8883")

        agent_instance.bridge.apply_redirect.assert_called_once_with("fra1.blynk.cloud")

    def test_bare_hostname_with_no_scheme(self, agent_instance):
        agent_instance._handle_redirect("fra1.blynk.cloud")

        agent_instance.bridge.apply_redirect.assert_called_once_with("fra1.blynk.cloud")

    def test_json_payload_with_host_key(self, agent_instance):
        agent_instance._handle_redirect('{"host": "lon1.blynk.cloud"}')

        agent_instance.bridge.apply_redirect.assert_called_once_with("lon1.blynk.cloud")

    def test_json_payload_with_server_key_fallback(self, agent_instance):
        agent_instance._handle_redirect('{"server": "lon1.blynk.cloud"}')

        agent_instance.bridge.apply_redirect.assert_called_once_with("lon1.blynk.cloud")

    def test_json_payload_with_address_key_fallback(self, agent_instance):
        agent_instance._handle_redirect('{"address": "lon1.blynk.cloud"}')

        agent_instance.bridge.apply_redirect.assert_called_once_with("lon1.blynk.cloud")

    def test_invalid_json_ignored(self, agent_instance):
        agent_instance._handle_redirect('{"host": not valid json')

        agent_instance.bridge.apply_redirect.assert_not_called()

    def test_json_missing_usable_host_ignored(self, agent_instance):
        agent_instance._handle_redirect('{"unrelated_field": "value"}')

        agent_instance.bridge.apply_redirect.assert_not_called()


class TestRunTerminalCommand:
    def _fake_result(self, stdout="", stderr=""):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = stderr
        return result

    def test_success_publishes_combined_output(self, agent_instance, monkeypatch):
        monkeypatch.setattr(
            agent.subprocess, "run",
            lambda *a, **kw: self._fake_result(stdout="hello\n", stderr=""),
        )

        agent_instance._run_terminal_command("echo hello")

        agent_instance.client.publish.assert_called_once_with("ds/AgentTerminal", "hello", qos=1)

    def test_empty_output_reports_no_output(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **kw: self._fake_result())

        agent_instance._run_terminal_command("true")

        agent_instance.client.publish.assert_called_once_with("ds/AgentTerminal", "(no output)", qos=1)

    def test_long_output_chunked_at_255_chars(self, agent_instance, monkeypatch):
        long_output = "x" * 600
        monkeypatch.setattr(
            agent.subprocess, "run",
            lambda *a, **kw: self._fake_result(stdout=long_output),
        )

        agent_instance._run_terminal_command("yes x")

        calls = agent_instance.client.publish.call_args_list
        assert len(calls) == 3  # 600 chars / 255 -> 3 chunks
        assert all(call.args[0] == "ds/AgentTerminal" for call in calls)
        rejoined = "".join(call.args[1] for call in calls)
        assert rejoined == long_output

    def test_timeout_reports_partial_output(self, agent_instance, monkeypatch):
        def raise_timeout(*a, **kw):
            raise agent.subprocess.TimeoutExpired(cmd="sh", timeout=60, output=b"partial output", stderr=b"")

        monkeypatch.setattr(agent.subprocess, "run", raise_timeout)

        agent_instance._run_terminal_command("sleep 999")

        published = agent_instance.client.publish.call_args[0][1]
        assert "timed out" in published
        assert "partial output" in published

    def test_unexpected_exception_reports_error(self, agent_instance, monkeypatch):
        def raise_error(*a, **kw):
            raise RuntimeError("nsenter not found")

        monkeypatch.setattr(agent.subprocess, "run", raise_error)

        agent_instance._run_terminal_command("ls")

        published = agent_instance.client.publish.call_args[0][1]
        assert "error" in published
        assert "nsenter not found" in published

    def test_uses_nsenter_into_host_pid_1(self, agent_instance, monkeypatch):
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return self._fake_result(stdout="ok")

        monkeypatch.setattr(agent.subprocess, "run", fake_run)

        agent_instance._run_terminal_command("ls -la")

        assert captured_cmd[:3] == ["nsenter", "--target", "1"]
        assert captured_cmd[-1] == "ls -la"
