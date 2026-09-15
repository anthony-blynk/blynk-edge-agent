"""Unit tests for BlynkConfig - load/save/is_provisioned/effective_server.
No device, no Docker, no MQTT - just the config file logic itself."""

import os
import stat
import sys

import pytest

import agent


def _write_env(path, **kv):
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))


class TestLoad:
    def test_missing_template_id_fails(self, tmp_path):
        env_path = tmp_path / "blynk.env"
        _write_env(env_path, BLYNK_SERVER="blynk.cloud")  # no template id

        config = agent.BlynkConfig()
        assert config.load(env_path=env_path) is False

    def test_fully_provisioned(self, tmp_path):
        env_path = tmp_path / "blynk.env"
        _write_env(
            env_path,
            BLYNK_SERVER="blynk.cloud",
            BLYNK_TEMPLATE_ID="TMPL123",
            BLYNK_AUTH_TOKEN="secret-token",
            BLYNK_VENDOR_PREFIX="Acme",
        )

        config = agent.BlynkConfig()
        assert config.load(env_path=env_path) is True
        assert config.is_provisioned() is True
        assert config.server == "blynk.cloud"
        assert config.template_id == "TMPL123"
        assert config.auth_token == "secret-token"
        assert config.vendor_prefix == "Acme"

    def test_template_id_only_not_provisioned(self, tmp_path):
        # The expected pre-BLE-provisioning state: template id present
        # (baked in at install time), server/token blank until the app
        # supplies them.
        env_path = tmp_path / "blynk.env"
        _write_env(env_path, BLYNK_TEMPLATE_ID="TMPL123", BLYNK_SERVER="", BLYNK_AUTH_TOKEN="")

        config = agent.BlynkConfig()
        assert config.load(env_path=env_path) is True
        assert config.is_provisioned() is False

    def test_missing_file_falls_back_to_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BLYNK_TEMPLATE_ID", "TMPL_FROM_ENV")
        monkeypatch.setenv("BLYNK_SERVER", "env.blynk.cloud")
        monkeypatch.setenv("BLYNK_AUTH_TOKEN", "env-token")

        config = agent.BlynkConfig()
        # File doesn't exist at all - load() should still work from the
        # process environment alone.
        assert config.load(env_path=tmp_path / "does-not-exist.env") is True
        assert config.template_id == "TMPL_FROM_ENV"
        assert config.server == "env.blynk.cloud"
        assert config.is_provisioned() is True

    def test_vendor_prefix_defaults_to_blynk(self, tmp_path):
        env_path = tmp_path / "blynk.env"
        _write_env(env_path, BLYNK_TEMPLATE_ID="TMPL123")

        config = agent.BlynkConfig()
        config.load(env_path=env_path)
        assert config.vendor_prefix == "Blynk"


class TestSave:
    def test_round_trips_values(self, tmp_path):
        env_path = tmp_path / "blynk.env"
        config = agent.BlynkConfig()
        config.server = "blynk.cloud"
        config.template_id = "TMPL123"
        config.auth_token = "secret-token"
        config.vendor_prefix = "Acme"

        config.save(env_path=env_path)

        reloaded = agent.BlynkConfig()
        assert reloaded.load(env_path=env_path) is True
        assert reloaded.server == "blynk.cloud"
        assert reloaded.template_id == "TMPL123"
        assert reloaded.auth_token == "secret-token"
        assert reloaded.vendor_prefix == "Acme"

    def test_none_values_write_empty_not_literal_none(self, tmp_path):
        # Regression guard for the `or ''` in save(): an unformatted None
        # would write the literal text "None", which reloads as a truthy
        # non-empty string and defeats is_provisioned().
        env_path = tmp_path / "blynk.env"
        config = agent.BlynkConfig()
        config.template_id = "TMPL123"
        config.server = None
        config.auth_token = None

        config.save(env_path=env_path)

        content = env_path.read_text()
        assert "None" not in content

        reloaded = agent.BlynkConfig()
        reloaded.load(env_path=env_path)
        assert reloaded.is_provisioned() is False

    def test_cleans_up_temp_file(self, tmp_path):
        env_path = tmp_path / "blynk.env"
        config = agent.BlynkConfig()
        config.template_id = "TMPL123"

        config.save(env_path=env_path)

        assert env_path.exists()
        assert not env_path.with_suffix(".new").exists()

    def test_overwrites_existing_file(self, tmp_path):
        env_path = tmp_path / "blynk.env"
        _write_env(env_path, BLYNK_TEMPLATE_ID="OLD", BLYNK_SERVER="old.blynk.cloud")

        config = agent.BlynkConfig()
        config.template_id = "NEW"
        config.server = "new.blynk.cloud"
        config.save(env_path=env_path)

        reloaded = agent.BlynkConfig()
        reloaded.load(env_path=env_path)
        assert reloaded.template_id == "NEW"
        assert reloaded.server == "new.blynk.cloud"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only apply on Linux/Mac")
    def test_chmods_to_owner_only(self, tmp_path):
        env_path = tmp_path / "blynk.env"
        config = agent.BlynkConfig()
        config.template_id = "TMPL123"

        config.save(env_path=env_path)

        mode = stat.S_IMODE(env_path.stat().st_mode)
        assert mode == 0o600


class TestEffectiveServer:
    def test_no_override_returns_configured_server(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "BRIDGE_HOST_OVERRIDE_FILE", tmp_path / "bridge_host_override")
        config = agent.BlynkConfig()
        config.server = "blynk.cloud"

        assert config.effective_server() == "blynk.cloud"

    def test_override_file_takes_precedence(self, tmp_path, monkeypatch):
        override_path = tmp_path / "bridge_host_override"
        override_path.write_text("redirected.blynk.cloud\n")
        monkeypatch.setattr(agent, "BRIDGE_HOST_OVERRIDE_FILE", override_path)

        config = agent.BlynkConfig()
        config.server = "blynk.cloud"

        assert config.effective_server() == "redirected.blynk.cloud"

    def test_empty_override_file_falls_back(self, tmp_path, monkeypatch):
        override_path = tmp_path / "bridge_host_override"
        override_path.write_text("")
        monkeypatch.setattr(agent, "BRIDGE_HOST_OVERRIDE_FILE", override_path)

        config = agent.BlynkConfig()
        config.server = "blynk.cloud"

        assert config.effective_server() == "blynk.cloud"
