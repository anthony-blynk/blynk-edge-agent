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


class TestProbeCloudReachable:
    """Direct regression coverage for the bug found this session: the
    bridge-state notification is routinely missed right after a mosquitto
    restart (confirmed on real hardware, three separate devices), which
    without an independent check leaves a healthy device stuck falsely
    believing it's disconnected forever, since its own recovery action
    (restarting mqtt-bridge) re-triggers the exact same missed
    notification every time."""

    def test_returns_true_when_connection_succeeds(self, agent_instance, monkeypatch):
        agent_instance.config.effective_server.return_value = "fra1.blynk.cloud"
        create_connection = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(agent.socket, "create_connection", create_connection)

        assert agent_instance._probe_cloud_reachable() is True
        create_connection.assert_called_once_with(("fra1.blynk.cloud", agent.BLYNK_CLOUD_MQTT_PORT), timeout=3.0)

    def test_returns_false_when_connection_raises_oserror(self, agent_instance, monkeypatch):
        agent_instance.config.effective_server.return_value = "fra1.blynk.cloud"
        monkeypatch.setattr(agent.socket, "create_connection", MagicMock(side_effect=OSError("refused")))

        assert agent_instance._probe_cloud_reachable() is False

    def test_returns_false_when_no_server_configured(self, agent_instance, monkeypatch):
        agent_instance.config.effective_server.return_value = None
        create_connection = MagicMock()
        monkeypatch.setattr(agent.socket, "create_connection", create_connection)

        assert agent_instance._probe_cloud_reachable() is False
        create_connection.assert_not_called()


class TestOnConnectFalsePositiveRecovery:
    """_on_connect uses the probe above to correct a stale presumed-
    disconnected state right when it's most likely to be wrong - see the
    comment above the call site for the real-hardware timing race."""

    def _stub_publishers(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent_instance, "_publish_device_info", MagicMock())
        monkeypatch.setattr(agent_instance, "_publish_system_info", MagicMock())

    def test_clears_presumed_disconnected_state_when_probe_succeeds(self, agent_instance, monkeypatch):
        self._stub_publishers(agent_instance, monkeypatch)
        monkeypatch.setattr(agent_instance, "_probe_cloud_reachable", MagicMock(return_value=True))
        agent_instance._bridge_disconnected_since = time.time() - 500
        agent_instance._bridge_dns_refresh_attempted = True

        agent_instance._on_connect(agent_instance.client, None, None, 0, None)

        assert agent_instance._bridge_disconnected_since is None
        assert agent_instance._bridge_dns_refresh_attempted is False

    def test_leaves_state_untouched_when_probe_fails(self, agent_instance, monkeypatch):
        self._stub_publishers(agent_instance, monkeypatch)
        monkeypatch.setattr(agent_instance, "_probe_cloud_reachable", MagicMock(return_value=False))
        since = time.time() - 500
        agent_instance._bridge_disconnected_since = since

        agent_instance._on_connect(agent_instance.client, None, None, 0, None)

        assert agent_instance._bridge_disconnected_since == since

    def test_does_not_probe_when_already_believed_connected(self, agent_instance, monkeypatch):
        self._stub_publishers(agent_instance, monkeypatch)
        probe = MagicMock()
        monkeypatch.setattr(agent_instance, "_probe_cloud_reachable", probe)
        agent_instance._bridge_disconnected_since = None

        agent_instance._on_connect(agent_instance.client, None, None, 0, None)

        probe.assert_not_called()


class TestRunReprovisioningFallbackProbe:
    """_run_reprovisioning's post-attempt fallback used to resubscribe and
    hope for a redelivered retained value - confirmed on real hardware that
    doesn't reliably work here either, so it now uses the same direct
    probe instead."""

    def test_successful_reprovisioning_clears_outage(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent.ble_provisioning, "provision", MagicMock(return_value=True))
        agent_instance._bridge_disconnected_since = time.time() - 500

        agent_instance._run_reprovisioning()

        assert agent_instance._bridge_disconnected_since is None
        assert agent_instance._reprovisioning is False

    def test_failed_reprovisioning_clears_outage_when_probe_finds_it_healthy(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent.ble_provisioning, "provision", MagicMock(return_value=False))
        monkeypatch.setattr(agent_instance, "_probe_cloud_reachable", MagicMock(return_value=True))
        agent_instance._bridge_disconnected_since = time.time() - 500
        agent_instance._bridge_dns_refresh_attempted = True

        agent_instance._run_reprovisioning()

        assert agent_instance._bridge_disconnected_since is None
        assert agent_instance._bridge_dns_refresh_attempted is False

    def test_failed_reprovisioning_restarts_clock_when_probe_also_fails(self, agent_instance, monkeypatch):
        monkeypatch.setattr(agent.ble_provisioning, "provision", MagicMock(return_value=False))
        monkeypatch.setattr(agent_instance, "_probe_cloud_reachable", MagicMock(return_value=False))
        agent_instance._bridge_disconnected_since = time.time() - 500

        before = time.time()
        agent_instance._run_reprovisioning()

        assert agent_instance._bridge_disconnected_since >= before


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


class TestOnMessageBinaryRouting:
    def test_upload_topic_bypasses_utf8_decode(self, agent_instance, monkeypatch):
        # Regression guard: raw file bytes are essentially never valid
        # UTF-8, so this topic must be routed to _handle_local_upload
        # BEFORE the generic decode-as-text path every other topic goes
        # through - not after, or every real upload would be silently
        # rejected as "Failed to decode message payload".
        handler = MagicMock()
        monkeypatch.setattr(agent_instance, "_handle_local_upload", handler)
        invalid_utf8 = b"\xff\xfe\x00\x01not valid utf-8 image bytes"

        agent_instance._on_message(None, None, FakeMessage("local/blynk/upload/photo.jpg", invalid_utf8))

        handler.assert_called_once_with("local/blynk/upload/photo.jpg", invalid_utf8)


class TestHandleLocalUpload:
    def _fake_response(self, status_code=200, text=""):
        response = MagicMock()
        response.status_code = status_code
        response.text = text
        return response

    def test_success_publishes_resulting_url(self, agent_instance, monkeypatch):
        agent_instance.config.effective_server.return_value = "fra1.blynk.cloud"
        agent_instance.config.auth_token = "test-token"
        response = self._fake_response(200, text="https://fra1.blynk.cloud/device_uploads/1/abc.png\n")
        monkeypatch.setattr(agent.requests, "post", lambda *a, **kw: response)

        agent_instance._handle_local_upload("local/blynk/upload/photo.jpg", b"fake-image-bytes")

        agent_instance.client.publish.assert_called_once_with(
            agent.TOPIC_LOCAL_UPLOAD_RESULT,
            "https://fra1.blynk.cloud/device_uploads/1/abc.png",
            qos=1,
        )

    def test_uses_filename_from_topic_and_real_credentials(self, agent_instance, monkeypatch):
        agent_instance.config.effective_server.return_value = "fra1.blynk.cloud"
        agent_instance.config.auth_token = "test-token"
        captured = {}

        def fake_post(url, params=None, files=None, **kwargs):
            captured["url"] = url
            captured["params"] = params
            captured["files"] = files
            return self._fake_response(200, text="https://x/y.png")

        monkeypatch.setattr(agent.requests, "post", fake_post)

        agent_instance._handle_local_upload("local/blynk/upload/photo.jpg", b"fake-bytes")

        assert captured["url"] == "https://fra1.blynk.cloud/external/api/upload"
        assert captured["params"] == {"token": "test-token"}
        assert captured["files"]["upfile"][0] == "photo.jpg"
        assert captured["files"]["upfile"][1] == b"fake-bytes"

    def test_no_filename_in_topic_falls_back_to_timestamp(self, agent_instance, monkeypatch):
        agent_instance.config.effective_server.return_value = "fra1.blynk.cloud"
        agent_instance.config.auth_token = "test-token"
        captured = {}
        monkeypatch.setattr(
            agent.requests, "post",
            lambda url, params=None, files=None, **kw: captured.update(files=files) or self._fake_response(200, "https://x/y"),
        )

        agent_instance._handle_local_upload("local/blynk/upload/", b"fake-bytes")

        assert captured["files"]["upfile"][0].startswith("upload_")

    def test_empty_payload_rejected_without_calling_blynk(self, agent_instance, monkeypatch):
        post = MagicMock()
        monkeypatch.setattr(agent.requests, "post", post)

        agent_instance._handle_local_upload("local/blynk/upload/photo.jpg", b"")

        post.assert_not_called()
        published = agent_instance.client.publish.call_args[0][1]
        assert "error" in published

    def test_blynk_error_response_surfaced(self, agent_instance, monkeypatch):
        agent_instance.config.effective_server.return_value = "fra1.blynk.cloud"
        agent_instance.config.auth_token = "bad-token"
        response = self._fake_response(400, text='{"error":{"message":"Invalid token."}}')
        monkeypatch.setattr(agent.requests, "post", lambda *a, **kw: response)

        agent_instance._handle_local_upload("local/blynk/upload/photo.jpg", b"fake-bytes")

        published = agent_instance.client.publish.call_args[0][1]
        assert "error" in published
        assert "Invalid token" in published

    def test_network_failure_surfaced_not_raised(self, agent_instance, monkeypatch):
        agent_instance.config.effective_server.return_value = "fra1.blynk.cloud"
        agent_instance.config.auth_token = "test-token"

        def raise_connection_error(*a, **kw):
            raise agent.requests.exceptions.ConnectionError("DNS failure")

        monkeypatch.setattr(agent.requests, "post", raise_connection_error)

        agent_instance._handle_local_upload("local/blynk/upload/photo.jpg", b"fake-bytes")  # must not raise

        published = agent_instance.client.publish.call_args[0][1]
        assert "error" in published
