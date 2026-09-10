#!/usr/bin/env python3
"""Runs device-tests against every device listed in a config file, over
SSH, and prints a pass/fail summary - so testing several different devices
(a Jetson, a CompuLab, a Pi5, a CM4, ...) before a release doesn't mean
manually SSHing into each one individually and reading its output by hand.

Run this from your own machine, not on a device - it's the orchestrator,
device-tests/ itself is what actually runs on each device (see its own
README for what that requires locally).

Usage:
    pip install -r device-tests/fleet_requirements.txt
    cp device-tests/fleet.example.json device-tests/fleet.json   # then edit - see README.md
    python3 device-tests/run_fleet.py device-tests/fleet.json

fleet.json is gitignored deliberately - it can contain real SSH passwords,
which must never end up committed. Prefer key-based auth (the default if
neither "password" nor "key_filename" is given - paramiko falls back to
the system's SSH agent/default keys) over passwords where you can.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import paramiko

REMOTE_REPO_DIR = "~/blynk-edge-agent"
REMOTE_VENV_DIR = f"{REMOTE_REPO_DIR}/.device-tests-venv"

# git reset --hard, not git pull: this is meant to be a disposable,
# always-fresh checkout dedicated to running tests, not somewhere to
# preserve ad hoc edits made directly on a device (confirmed necessary in
# practice - conftest.py got hand-debugged in place on a real device
# earlier in this project's testing) - a plain `git pull` would just fail
# outright on any such local change instead of silently overwriting it,
# breaking an otherwise-unattended multi-device run.
REMOTE_SCRIPT = f"""
set -e
if [ -d {REMOTE_REPO_DIR}/.git ]; then
  cd {REMOTE_REPO_DIR}
  git fetch origin
  git reset --hard origin/master
else
  git clone https://github.com/anthony-blynk/blynk-edge-agent.git {REMOTE_REPO_DIR}
  cd {REMOTE_REPO_DIR}
fi
if [ ! -x {REMOTE_VENV_DIR}/bin/pip ]; then
  # Not just "does the directory exist" - confirmed on real hardware that
  # a venv creation failure (e.g. missing python3-venv, so ensurepip can't
  # bootstrap pip) still leaves a partial venv directory behind, which
  # would otherwise be silently treated as already-set-up on every
  # subsequent run and fail later with a confusing "pip: No such file".
  rm -rf {REMOTE_VENV_DIR}
  python3 -m venv {REMOTE_VENV_DIR}
fi
{REMOTE_VENV_DIR}/bin/pip install -q -r device-tests/requirements.txt
{REMOTE_VENV_DIR}/bin/python -m pytest device-tests -v
"""


def run_on_device(device: dict) -> tuple[bool, str]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        "hostname": device["host"],
        "username": device["user"],
        "timeout": 15,
    }
    if "password" in device:
        connect_kwargs["password"] = device["password"]
    if "key_filename" in device:
        connect_kwargs["key_filename"] = str(Path(device["key_filename"]).expanduser())

    try:
        client.connect(**connect_kwargs)
    except Exception as e:
        return False, f"SSH connection failed: {e}"

    try:
        _, stdout, stderr = client.exec_command(REMOTE_SCRIPT, timeout=300)
        output = stdout.read().decode(errors="replace") + stderr.read().decode(errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code == 0, output
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", help="Path to a fleet config JSON file (see fleet.example.json)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print full remote output for every device, not just failing ones")
    args = parser.parse_args()

    devices = json.loads(Path(args.config).read_text())

    results = []
    for device in devices:
        name = device.get("name", device["host"])
        print(f"--- {name} ({device['host']}) ---", flush=True)
        ok, output = run_on_device(device)
        results.append((name, ok))
        if args.verbose or not ok:
            print(output)
        print(f"{'PASSED' if ok else 'FAILED'}\n", flush=True)

    print("=" * 40)
    print("Summary:")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    if any(not ok for _, ok in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
