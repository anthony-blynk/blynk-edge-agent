"""Unit tests for the OTA apply/rollback path - ComposeManager._apply_via_helper
(the detached-helper-container handoff) and run_apply_only (the module-level
entry point that detached helper actually runs, including its rollback
logic). subprocess/docker calls and MQTT publishing are mocked - this is
testing the decision logic, not real Docker/network behavior."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agent


def _fake_proc(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class TestApplyViaHelper:
    def test_launches_detached_helper_with_own_image_and_network(self, tmp_path, monkeypatch):
        manager = agent.ComposeManager(compose_path=tmp_path / "docker-compose.yml")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["docker", "inspect"] and "{{.Config.Image}}" in cmd:
                return _fake_proc(stdout="ghcr.io/anthony-blynk/blynk-agent:2.3.4\n")
            if cmd[:2] == ["docker", "inspect"]:
                return _fake_proc(stdout="blynk_default\n")
            if cmd[:2] == ["docker", "run"]:
                return _fake_proc(returncode=0)
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(agent.subprocess, "run", fake_run)

        result = manager._apply_via_helper(backup_path=tmp_path / "backup.yml")

        assert result is True
        run_cmd = calls[-1]
        assert "--network" in run_cmd
        assert "blynk_default" in run_cmd
        assert "ghcr.io/anthony-blynk/blynk-agent:2.3.4" in run_cmd
        assert run_cmd[-1] == str(tmp_path / "backup.yml")
        assert "--apply-only" in run_cmd

    def test_missing_own_image_falls_back_to_direct_apply(self, tmp_path, monkeypatch):
        manager = agent.ComposeManager(compose_path=tmp_path / "docker-compose.yml")
        monkeypatch.setattr(agent.subprocess, "run", lambda *a, **kw: _fake_proc(stdout=""))
        direct_apply = MagicMock(return_value=True)
        monkeypatch.setattr(manager, "_run_docker_compose", direct_apply)

        result = manager._apply_via_helper(backup_path=None)

        assert result is True
        direct_apply.assert_called_once()

    def test_no_network_omits_network_flag(self, tmp_path, monkeypatch):
        manager = agent.ComposeManager(compose_path=tmp_path / "docker-compose.yml")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "{{.Config.Image}}" in cmd:
                return _fake_proc(stdout="ghcr.io/anthony-blynk/blynk-agent:2.3.4\n")
            if cmd[:2] == ["docker", "inspect"]:
                return _fake_proc(stdout="")  # no network found
            return _fake_proc(returncode=0)

        monkeypatch.setattr(agent.subprocess, "run", fake_run)

        manager._apply_via_helper(backup_path=None)

        run_cmd = calls[-1]
        assert "--network" not in run_cmd

    def test_docker_run_failure_returns_false(self, tmp_path, monkeypatch):
        manager = agent.ComposeManager(compose_path=tmp_path / "docker-compose.yml")

        def fake_run(cmd, **kwargs):
            if "{{.Config.Image}}" in cmd:
                return _fake_proc(stdout="ghcr.io/anthony-blynk/blynk-agent:2.3.4\n")
            if cmd[:2] == ["docker", "inspect"]:
                return _fake_proc(stdout="blynk_default\n")
            return _fake_proc(returncode=1, stderr="no such container")

        monkeypatch.setattr(agent.subprocess, "run", fake_run)

        assert manager._apply_via_helper(backup_path=None) is False

    def test_empty_backup_path_passes_empty_string(self, tmp_path, monkeypatch):
        # run_apply_only treats an empty arg as "no backup given" and falls
        # back to globbing BACKUP_DIR - the helper just needs to pass
        # through whatever it was given, including None -> "".
        manager = agent.ComposeManager(compose_path=tmp_path / "docker-compose.yml")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "{{.Config.Image}}" in cmd:
                return _fake_proc(stdout="ghcr.io/anthony-blynk/blynk-agent:2.3.4\n")
            if cmd[:2] == ["docker", "inspect"]:
                return _fake_proc(stdout="blynk_default\n")
            return _fake_proc(returncode=0)

        monkeypatch.setattr(agent.subprocess, "run", fake_run)

        manager._apply_via_helper(backup_path=None)

        assert calls[-1][-1] == ""


@pytest.fixture
def apply_only_env(tmp_path, monkeypatch):
    """run_apply_only constructs ComposeManager()/BlynkConfig() with no
    arguments - their default paths are bound to the real COMPOSE_FILE/
    ENV_FILE at class-definition time, so simply monkeypatching those module
    constants afterward does NOT redirect an already-defined default
    parameter (confirmed - default arg values are evaluated once, at def
    time, not per-call). Replacing the classes themselves with factories
    sidesteps this entirely."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("x-stack:\n  version: \"1.0.0\"\n")

    monkeypatch.setattr(agent, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(agent, "COMPOSE_FILE", compose_file)
    monkeypatch.setattr(agent, "publish_device_info_once", MagicMock())

    fake_compose_manager = MagicMock()
    monkeypatch.setattr(agent, "ComposeManager", lambda: fake_compose_manager)
    fake_config = MagicMock()
    monkeypatch.setattr(agent, "BlynkConfig", lambda: fake_config)

    return SimpleNamespace(
        backup_dir=backup_dir,
        compose_file=compose_file,
        compose_manager=fake_compose_manager,
        config=fake_config,
    )


class TestRunApplyOnly:
    def test_success_publishes_info_and_skips_rollback(self, apply_only_env):
        apply_only_env.compose_manager._run_docker_compose.return_value = True

        agent.run_apply_only("")

        agent.publish_device_info_once.assert_called_once_with(
            apply_only_env.config, apply_only_env.compose_manager
        )

    def test_failure_rolls_back_to_given_backup(self, apply_only_env, monkeypatch):
        apply_only_env.compose_manager._run_docker_compose.return_value = False
        backup_path = apply_only_env.backup_dir / "docker-compose_backup_20260101_000000.yml"
        backup_path.write_text("x-stack:\n  version: \"0.9.0\"\n")

        agent.run_apply_only(str(backup_path))

        # The backup content should now be what's at COMPOSE_FILE.
        assert apply_only_env.compose_file.read_text() == backup_path.read_text()
        # And a second apply attempt was made using the restored file.
        assert apply_only_env.compose_manager._run_docker_compose.call_count == 2

    def test_failure_with_no_backup_arg_finds_latest_in_backup_dir(self, apply_only_env):
        apply_only_env.compose_manager._run_docker_compose.return_value = False
        older = apply_only_env.backup_dir / "docker-compose_backup_20260101_000000.yml"
        older.write_text("older")
        newer = apply_only_env.backup_dir / "docker-compose_backup_20260102_000000.yml"
        newer.write_text("newer")

        agent.run_apply_only("")  # no backup_arg - must glob BACKUP_DIR itself

        assert apply_only_env.compose_file.read_text() == "newer"

    def test_failure_with_nonexistent_backup_arg_falls_back_to_glob(self, apply_only_env):
        apply_only_env.compose_manager._run_docker_compose.return_value = False
        real_backup = apply_only_env.backup_dir / "docker-compose_backup_20260101_000000.yml"
        real_backup.write_text("the real one")

        agent.run_apply_only(str(apply_only_env.backup_dir / "does-not-exist.yml"))

        assert apply_only_env.compose_file.read_text() == "the real one"

    def test_failure_with_no_backup_available_still_publishes_info(self, apply_only_env):
        apply_only_env.compose_manager._run_docker_compose.return_value = False
        # backup_dir is empty - nothing to roll back to.

        agent.run_apply_only("")

        agent.publish_device_info_once.assert_called_once()
