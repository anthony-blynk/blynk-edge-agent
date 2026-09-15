"""Unit tests for _write_acl_file - in particular a direct regression test
for the mosquitto crash-loop bug found and fixed on 2026-09-14: the
chmod/chown on acl.rules must be re-applied on every call, not only when
the file's content happens to change, since something else (install.sh's
own chown -R) can clobber the ownership out from under an otherwise
unchanged file."""

from unittest.mock import MagicMock

import pytest

import agent

CREDS = {
    "BRIDGE_LOCAL_USERNAME": "blynk-bridge",
    "BRIDGE_LOCAL_PASSWORD": "bridge-pw",
    "AGENT_LOCAL_USERNAME": "blynk-agent",
    "AGENT_LOCAL_PASSWORD": "agent-pw",
}


@pytest.fixture
def acl_file(tmp_path, monkeypatch):
    path = tmp_path / "conf.d" / "acl.rules"
    monkeypatch.setattr(agent, "ACL_FILE", path)
    monkeypatch.setattr(agent.os, "chmod", MagicMock())
    # os.chown doesn't exist at all on Windows (it's POSIX-only) - raising=False
    # lets this run in local Windows dev too, not just Linux CI.
    monkeypatch.setattr(agent.os, "chown", MagicMock(), raising=False)
    return path


class TestContentChangeDetection:
    def test_fresh_file_returns_true_and_writes_content(self, acl_file):
        assert agent._write_acl_file(CREDS) is True
        assert acl_file.exists()
        content = acl_file.read_text()
        assert "user blynk-bridge" in content
        assert "user blynk-agent" in content

    def test_unchanged_content_returns_false(self, acl_file):
        agent._write_acl_file(CREDS)  # first write
        assert agent._write_acl_file(CREDS) is False  # identical creds, no change

    def test_changed_content_returns_true_and_rewrites(self, acl_file):
        agent._write_acl_file(CREDS)
        new_creds = dict(CREDS, BRIDGE_LOCAL_USERNAME="different-bridge-user")

        assert agent._write_acl_file(new_creds) is True
        assert "user different-bridge-user" in acl_file.read_text()


class TestPermissionsSelfHeal:
    """Direct regression coverage for the real crash-loop bug: mosquitto
    (uid 1883) failed to open acl.rules because install.sh's chown -R had
    clobbered its ownership back to the installing user - only surfacing
    on mqtt-bridge's *next* restart, since mosquitto only reads acl_file at
    process start. The fix re-applies chmod/chown unconditionally so the
    agent self-heals this on its own next check, regardless of what
    clobbered it in between."""

    def test_chmod_and_chown_applied_on_fresh_write(self, acl_file):
        agent._write_acl_file(CREDS)

        agent.os.chmod.assert_called_once_with(acl_file, 0o600)
        agent.os.chown.assert_called_once_with(acl_file, 1883, 1883)

    def test_chmod_and_chown_reapplied_even_when_content_unchanged(self, acl_file):
        agent._write_acl_file(CREDS)
        agent.os.chmod.reset_mock()
        agent.os.chown.reset_mock()

        # Same creds - content_changed is False - but this is exactly the
        # call pattern that happens on every agent restart via
        # MqttBridge.ensure_current(), and it must still fix ownership if
        # something external clobbered it since the last check.
        result = agent._write_acl_file(CREDS)

        assert result is False
        agent.os.chmod.assert_called_once_with(acl_file, 0o600)
        agent.os.chown.assert_called_once_with(acl_file, 1883, 1883)

    def test_chown_failure_is_swallowed_not_raised(self, acl_file):
        # Best-effort: this container may not be able to chown to an
        # arbitrary uid (e.g. non-Linux dev environments) - must not crash
        # the caller (MqttBridge.ensure_current, on the agent's main path).
        agent.os.chown.side_effect = PermissionError("not permitted")

        result = agent._write_acl_file(CREDS)  # must not raise

        assert result is True
