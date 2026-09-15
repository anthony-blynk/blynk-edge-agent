"""Unit tests for ble_provisioning.py's pure, D-Bus-free helper functions -
access-tech/WiFi-scan formatting, NM failure-reason mapping, static-IP math,
and BLE device-name generation. Everything that actually talks to D-Bus
(NetworkManager/ModemManager/BlueZ) is out of scope here - see the module
docstring discussion; that layer needs a real device (device-tests/), not a
hand-mocked D-Bus object graph."""

import socket
import struct

import pytest

import ble_provisioning as ble


class TestFormatAccessTechnologies:
    def test_single_bit(self):
        assert ble._format_access_technologies(1 << 14) == "LTE"  # LTE

    def test_hspa_matches_real_hardware_reading(self):
        # Confirmed via mmcli directly on real hardware (SIM7070G and
        # SIM7080) this session - the modem's own firmware reports this,
        # not a bug in this formatter.
        assert ble._format_access_technologies(1 << 8) == "HSPA"

    def test_multiple_bits_joined_with_plus(self):
        mask = (1 << 14) | (1 << 8)  # LTE + HSPA
        result = ble._format_access_technologies(mask)
        assert result == "HSPA+LTE"  # dict insertion order: HSPA (1<<8) defined before LTE (1<<14)

    def test_no_bits_set_returns_unknown(self):
        assert ble._format_access_technologies(0) == "unknown"

    def test_unrecognized_bit_contributes_nothing(self):
        # A bit beyond the documented enum shouldn't crash or appear as
        # some garbage name - just gets silently omitted.
        mask = (1 << 14) | (1 << 30)
        assert ble._format_access_technologies(mask) == "LTE"


class TestChannelFromFrequency:
    def test_2_4ghz_channel_1(self):
        assert ble._channel_from_frequency_mhz(2412) == 1

    def test_2_4ghz_channel_13(self):
        assert ble._channel_from_frequency_mhz(2472) == 13

    def test_2_4ghz_channel_14_special_case(self):
        assert ble._channel_from_frequency_mhz(2484) == 14

    def test_5ghz_channel_36(self):
        assert ble._channel_from_frequency_mhz(5180) == 36

    def test_out_of_range_returns_zero(self):
        assert ble._channel_from_frequency_mhz(2400) == 0


class TestApSecurityString:
    def test_wpa3_when_sae_bit_set(self):
        result = ble._ap_security_string(flags=0, wpa_flags=0, rsn_flags=ble.NM_802_11_AP_SEC_KEY_MGMT_SAE)
        assert result == "WPA3"

    def test_wpa2_when_rsn_flags_set_without_sae(self):
        result = ble._ap_security_string(flags=0, wpa_flags=0, rsn_flags=0x1)  # any non-SAE RSN bit
        assert result == "WPA2"

    def test_wpa_when_only_wpa_flags_set(self):
        result = ble._ap_security_string(flags=0, wpa_flags=0x1, rsn_flags=0)
        assert result == "WPA"

    def test_wep_when_only_privacy_flag_set(self):
        result = ble._ap_security_string(flags=ble.NM_802_11_AP_FLAGS_PRIVACY, wpa_flags=0, rsn_flags=0)
        assert result == "WEP"

    def test_open_when_nothing_set(self):
        assert ble._ap_security_string(flags=0, wpa_flags=0, rsn_flags=0) == "OPEN"


class TestMapNmFailureReason:
    @pytest.mark.parametrize("reason_code", [
        ble.NM_DEVICE_STATE_REASON_NO_SECRETS,
        ble.NM_DEVICE_STATE_REASON_SUPPLICANT_DISCONNECT,
        ble.NM_DEVICE_STATE_REASON_SUPPLICANT_TIMEOUT,
    ])
    def test_credential_related_reasons(self, reason_code):
        assert ble._map_nm_failure_reason(reason_code) == "invalid_credentials"

    def test_ssid_not_found(self):
        assert ble._map_nm_failure_reason(ble.NM_DEVICE_STATE_REASON_SSID_NOT_FOUND) == "not_found"

    def test_unknown_code_falls_back_to_timeout(self):
        assert ble._map_nm_failure_reason(999999) == "timeout"


class TestPrefixFromNetmask:
    def test_slash_24(self):
        assert ble._prefix_from_netmask("255.255.255.0") == 24

    def test_slash_16(self):
        assert ble._prefix_from_netmask("255.255.0.0") == 16

    def test_slash_8(self):
        assert ble._prefix_from_netmask("255.0.0.0") == 8

    def test_slash_32(self):
        assert ble._prefix_from_netmask("255.255.255.255") == 32

    def test_invalid_input_falls_back_to_24(self):
        assert ble._prefix_from_netmask("not.an.ip.address") == 24


class TestIpv4StrToUint32:
    def test_matches_independent_little_endian_computation(self):
        # inet_aton packs the octets in the order written, so "1.1.168.192"
        # -> bytes [1, 1, 168, 192]. struct "=I" (native order) on the
        # little-endian hosts this project actually targets (x86/ARM,
        # always little-endian in practice) reads byte[0] as the LEAST
        # significant byte - computed here independently of struct/socket
        # to avoid a tautological test.
        expected = 1 + (1 << 8) + (168 << 16) + (192 << 24)
        assert ble._ipv4_str_to_uint32("1.1.168.192") == expected

    def test_round_trips_back_to_original_bytes(self):
        addr = "8.8.4.4"
        value = ble._ipv4_str_to_uint32(addr)
        # Re-encoding with the same native-order format must reconstruct
        # the exact bytes inet_aton produced for this address.
        assert struct.pack("=I", value) == socket.inet_aton(addr)


class TestIpv4Settings:
    def test_no_ip_means_auto_dhcp(self):
        settings = ble._ipv4_settings({})
        assert settings["method"].value == "auto"
        assert "address-data" not in settings

    def test_static_ip_sets_manual_method_and_address(self):
        settings = ble._ipv4_settings({"ip": "192.168.1.50", "mask": "255.255.255.0"})

        assert settings["method"].value == "manual"
        address_data = settings["address-data"].value
        assert address_data[0]["address"].value == "192.168.1.50"
        assert address_data[0]["prefix"].value == 24

    def test_gateway_included_when_present(self):
        settings = ble._ipv4_settings({"ip": "192.168.1.50", "gw": "192.168.1.1"})
        assert settings["gateway"].value == "192.168.1.1"

    def test_gateway_omitted_when_absent(self):
        settings = ble._ipv4_settings({"ip": "192.168.1.50"})
        assert "gateway" not in settings

    def test_dns_servers_included_when_present(self):
        settings = ble._ipv4_settings({"ip": "192.168.1.50", "dns": "8.8.8.8", "dns2": "8.8.4.4"})
        dns_values = settings["dns"].value
        assert len(dns_values) == 2
        assert dns_values[0] == ble._ipv4_str_to_uint32("8.8.8.8")
        assert dns_values[1] == ble._ipv4_str_to_uint32("8.8.4.4")

    def test_dns_omitted_when_absent(self):
        settings = ble._ipv4_settings({"ip": "192.168.1.50"})
        assert "dns" not in settings

    def test_default_prefix_is_slash_24(self):
        settings = ble._ipv4_settings({"ip": "192.168.1.50"})  # no "mask" given
        assert settings["address-data"].value[0]["prefix"].value == 24


class TestDeviceSerial:
    def test_reads_serial_from_cpuinfo(self, monkeypatch):
        import io

        cpuinfo = "processor\t: 0\nSerial\t\t: 1000000012345678\n"

        def fake_open(path, *a, **kw):
            if path == "/proc/cpuinfo":
                return io.StringIO(cpuinfo)
            raise OSError("unexpected path in test")

        monkeypatch.setattr("builtins.open", fake_open)

        assert ble._device_serial() == "1000000012345678"

    def test_falls_back_to_hostname_when_missing(self, monkeypatch):
        def fake_open(path, *a, **kw):
            raise OSError("no such file")

        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr(ble.socket, "gethostname", lambda: "myhost123")

        assert ble._device_serial() == "myhost123"


class TestShortSuffix:
    def test_is_deterministic(self):
        assert ble._short_suffix("some-unique-id") == ble._short_suffix("some-unique-id")

    def test_default_length_is_four(self):
        assert len(ble._short_suffix("some-unique-id")) == 4

    def test_custom_length(self):
        assert len(ble._short_suffix("some-unique-id", length=6)) == 6

    def test_only_uses_confusable_free_alphabet(self):
        suffix = ble._short_suffix("some-unique-id", length=20)
        assert all(c in ble.NAME_ALPHABET for c in suffix)
        # 0/O/1/I/L are deliberately excluded (visually confusable).
        assert not any(c in suffix for c in "01ILO")

    def test_different_inputs_usually_differ(self):
        assert ble._short_suffix("device-a") != ble._short_suffix("device-b")


class TestDeviceName:
    def test_format_with_default_prefix(self, monkeypatch):
        monkeypatch.setattr(ble, "_device_serial", lambda: "fixed-serial")

        name = ble.device_name()

        assert name == f"Blynk Device-{ble._short_suffix('fixed-serial')}"

    def test_format_with_custom_vendor_prefix(self, monkeypatch):
        monkeypatch.setattr(ble, "_device_serial", lambda: "fixed-serial")

        name = ble.device_name(vendor_prefix="Acme")

        assert name.startswith("Acme Device-")
