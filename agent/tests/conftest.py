"""Shared fixtures for agent.py's unit tests - pure logic, no real device,
no real Docker/MQTT/network needed. Contrast with device-tests/ at the repo
root, which needs to run against a real, already-provisioned device."""

import sys
from pathlib import Path

# agent.py is a flat script-style module (not a package), living one
# directory up from this file - not on sys.path by default when pytest is
# invoked from elsewhere (e.g. the repo root), so `import agent` would
# otherwise fail.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Strip any real BLYNK_* environment variables for every test, so a
    test's behavior can never depend on whatever happens to be set in the
    shell/CI runner it's executed in - only on what each test explicitly
    arranges via env_path fixtures or monkeypatch.setenv."""
    for key in ("BLYNK_SERVER", "BLYNK_AUTH_TOKEN", "BLYNK_TEMPLATE_ID", "BLYNK_VENDOR_PREFIX"):
        monkeypatch.delenv(key, raising=False)
