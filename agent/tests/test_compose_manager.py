"""Unit tests for ComposeManager - version parsing and the OTA
download/verify/apply pipeline. Network and docker-compose-apply calls are
mocked; this is testing update_from_url's own decision logic (checksum
verify, YAML validation, skip-if-unchanged), not the real subprocess calls
_validate/_apply_via_helper make - those need a real device (see
device-tests/)."""

import base64
import hashlib
from unittest.mock import MagicMock

import pytest
import yaml

import agent


COMPOSE_YAML = """\
x-stack:
  version: "2.3.4"
services:
  agent:
    image: ghcr.io/anthony-blynk/blynk-agent:2.3.4
"""


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _fake_response(text, headers=None):
    resp = MagicMock()
    resp.text = text
    resp.content = text.encode()
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    return resp


class TestGetVersion:
    def test_from_data_finds_x_prefixed_version(self):
        data = yaml.safe_load(COMPOSE_YAML)
        assert agent.ComposeManager.get_version_from_data(data) == "2.3.4"

    def test_from_data_no_x_key_returns_none(self):
        assert agent.ComposeManager.get_version_from_data({"services": {}}) is None

    def test_from_data_x_key_without_version_returns_none(self):
        data = {"x-stack": {"other_field": "value"}}
        assert agent.ComposeManager.get_version_from_data(data) is None

    def test_get_version_missing_file_returns_none(self, tmp_path):
        manager = agent.ComposeManager(compose_path=tmp_path / "does-not-exist.yml")
        assert manager.get_version() is None

    def test_get_version_reads_real_file(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(COMPOSE_YAML)
        manager = agent.ComposeManager(compose_path=compose_path)
        assert manager.get_version() == "2.3.4"

    def test_get_version_invalid_yaml_returns_none(self, tmp_path):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text("not: valid: yaml: [")
        manager = agent.ComposeManager(compose_path=compose_path)
        assert manager.get_version() is None


class TestUpdateFromUrlChecksum:
    def _manager(self, tmp_path, monkeypatch, validate_result=True, apply_result=True):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(COMPOSE_YAML)  # already at 2.3.4
        manager = agent.ComposeManager(compose_path=compose_path)
        monkeypatch.setattr(manager, "_validate", lambda path: validate_result)
        monkeypatch.setattr(manager, "_apply_via_helper", lambda backup_path: apply_result)
        monkeypatch.setattr(manager, "_backup_existing_file", lambda: None)
        return manager

    def _new_compose_yaml(self, version="2.3.5"):
        return COMPOSE_YAML.replace("2.3.4", version)

    def test_matching_checksum_proceeds(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path, monkeypatch)
        new_yaml = self._new_compose_yaml()
        response = _fake_response(new_yaml, headers={"x-SHA256": _sha256_b64(new_yaml.encode())})
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)

        assert manager.update_from_url("http://example/compose.yml") is True

    def test_mismatched_checksum_rejects(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path, monkeypatch)
        new_yaml = self._new_compose_yaml()
        response = _fake_response(new_yaml, headers={"x-SHA256": _sha256_b64(b"totally different content")})
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)

        assert manager.update_from_url("http://example/compose.yml") is False

    def test_missing_header_warns_but_still_proceeds(self, tmp_path, monkeypatch):
        # Not yet a hard requirement - see the comment in update_from_url.
        manager = self._manager(tmp_path, monkeypatch)
        new_yaml = self._new_compose_yaml()
        response = _fake_response(new_yaml, headers={})
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)

        assert manager.update_from_url("http://example/compose.yml") is True

    def test_checksum_verified_before_yaml_is_even_parsed(self, tmp_path, monkeypatch):
        # A checksum failure should short-circuit before touching YAML
        # parsing/disk at all - garbage content with a deliberately wrong
        # checksum should be rejected, not fail later for being invalid YAML.
        manager = self._manager(tmp_path, monkeypatch)
        garbage = "not valid yaml: [["
        response = _fake_response(garbage, headers={"x-SHA256": _sha256_b64(b"mismatched")})
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)

        assert manager.update_from_url("http://example/compose.yml") is False


class TestUpdateFromUrlValidation:
    def _manager(self, tmp_path, monkeypatch, validate_result=True):
        compose_path = tmp_path / "docker-compose.yml"
        compose_path.write_text(COMPOSE_YAML)
        manager = agent.ComposeManager(compose_path=compose_path)
        monkeypatch.setattr(manager, "_validate", lambda path: validate_result)
        monkeypatch.setattr(manager, "_apply_via_helper", lambda backup_path: True)
        monkeypatch.setattr(manager, "_backup_existing_file", lambda: None)
        return manager

    def test_invalid_yaml_rejected(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path, monkeypatch)
        response = _fake_response("not: valid: yaml: [")
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)

        assert manager.update_from_url("http://example/compose.yml") is False

    def test_missing_x_stack_rejected(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path, monkeypatch)
        response = _fake_response(yaml.safe_dump({"services": {}}))
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)

        assert manager.update_from_url("http://example/compose.yml") is False

    def test_same_version_skips_reapply(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path, monkeypatch)
        response = _fake_response(COMPOSE_YAML)  # identical version to what's on disk
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)
        apply_called = []
        monkeypatch.setattr(manager, "_apply_via_helper", lambda backup_path: apply_called.append(True))

        assert manager.update_from_url("http://example/compose.yml") is True
        assert apply_called == []  # never actually applied - no-op re-apply

    def test_failed_docker_compose_validation_rejected(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path, monkeypatch, validate_result=False)
        new_yaml = COMPOSE_YAML.replace("2.3.4", "2.3.5")
        response = _fake_response(new_yaml)
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)

        assert manager.update_from_url("http://example/compose.yml") is False
        # The original compose file must be left untouched on a validation failure.
        assert manager.get_version() == "2.3.4"

    def test_new_version_applies_and_replaces_file(self, tmp_path, monkeypatch):
        manager = self._manager(tmp_path, monkeypatch)
        new_yaml = COMPOSE_YAML.replace("2.3.4", "2.3.5")
        response = _fake_response(new_yaml)
        monkeypatch.setattr(manager, "_get_with_retry", lambda url: response)

        assert manager.update_from_url("http://example/compose.yml") is True
        assert manager.get_version() == "2.3.5"
