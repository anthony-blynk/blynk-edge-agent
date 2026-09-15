"""Unit tests for the _read_* diagnostics functions - these feed the live
CPU/mem/disk/temp/model datastreams on the dashboard. They read hardcoded
/proc and /sys paths directly, so real files are faked by intercepting
open() for exactly those paths and falling through to the real open() for
everything else (so pytest's own file access, imports, etc. keep working
normally)."""

import builtins
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agent


@pytest.fixture
def fake_files(monkeypatch):
    """Use like: fake_files({"/proc/loadavg": "0.50 0.40 0.30 1/200 1234\\n"}).
    A value of None simulates that path not existing (OSError on open)."""
    real_open = builtins.open

    def _install(content_by_path):
        def fake_open(path, *args, **kwargs):
            key = str(path)
            if key in content_by_path:
                content = content_by_path[key]
                if content is None:
                    raise OSError(f"simulated missing file: {key}")
                return io.StringIO(content)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fake_open)

    return _install


class TestReadCpuUsagePercent:
    def test_computes_load_over_cores(self, fake_files, monkeypatch):
        fake_files({"/proc/loadavg": "2.0 1.5 1.0 3/300 5678\n"})
        monkeypatch.setattr(agent.os, "cpu_count", lambda: 4)

        # load1=2.0, cores=4 -> 50%
        assert agent._read_cpu_usage_percent() == pytest.approx(50.0)

    def test_caps_at_100_percent(self, fake_files, monkeypatch):
        fake_files({"/proc/loadavg": "8.0 1.5 1.0 3/300 5678\n"})
        monkeypatch.setattr(agent.os, "cpu_count", lambda: 2)

        # load1=8.0, cores=2 -> 400% uncapped, must clamp to 100
        assert agent._read_cpu_usage_percent() == 100.0

    def test_missing_file_returns_none(self, fake_files):
        fake_files({"/proc/loadavg": None})

        assert agent._read_cpu_usage_percent() is None

    def test_no_cpu_count_defaults_to_one_core(self, fake_files, monkeypatch):
        fake_files({"/proc/loadavg": "0.5 0.4 0.3 1/200 1234\n"})
        monkeypatch.setattr(agent.os, "cpu_count", lambda: None)

        assert agent._read_cpu_usage_percent() == pytest.approx(50.0)


class TestReadMemUsagePercent:
    def test_uses_mem_available_not_mem_free(self, fake_files):
        # MemFree would read artificially low next to disk-cache-heavy
        # MemAvailable - this is a deliberate design choice, see the
        # function's own comment.
        meminfo = (
            "MemTotal:        1000000 kB\n"
            "MemFree:          100000 kB\n"
            "MemAvailable:     600000 kB\n"
        )
        fake_files({"/proc/meminfo": meminfo})

        # (1000000 - 600000) / 1000000 * 100 = 40%, not the ~90% MemFree would imply
        assert agent._read_mem_usage_percent() == pytest.approx(40.0)

    def test_missing_mem_available_returns_none(self, fake_files):
        fake_files({"/proc/meminfo": "MemTotal: 1000000 kB\n"})

        assert agent._read_mem_usage_percent() is None

    def test_missing_file_returns_none(self, fake_files):
        fake_files({"/proc/meminfo": None})

        assert agent._read_mem_usage_percent() is None


class TestReadMemTotalMb:
    def test_converts_kb_to_mb(self, fake_files):
        fake_files({"/proc/meminfo": "MemTotal:        2048000 kB\n"})

        assert agent._read_mem_total_mb() == pytest.approx(2000.0)

    def test_missing_file_returns_none(self, fake_files):
        fake_files({"/proc/meminfo": None})

        assert agent._read_mem_total_mb() is None


class TestReadTemperatureC:
    def test_converts_millidegrees(self, fake_files):
        fake_files({"/sys/class/thermal/thermal_zone0/temp": "45500\n"})

        assert agent._read_temperature_c() == pytest.approx(45.5)

    def test_missing_sensor_returns_none(self, fake_files):
        fake_files({"/sys/class/thermal/thermal_zone0/temp": None})

        assert agent._read_temperature_c() is None


class TestReadOsPrettyName:
    def test_prefers_host_mount(self, fake_files):
        fake_files({
            "/host/etc/os-release": 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n',
            "/etc/os-release": 'PRETTY_NAME="Alpine Linux"\n',
        })

        assert agent._read_os_pretty_name() == "Debian GNU/Linux 13 (trixie)"

    def test_falls_back_when_host_mount_absent(self, fake_files):
        fake_files({
            "/host/etc/os-release": None,
            "/etc/os-release": 'PRETTY_NAME="Alpine Linux"\n',
        })

        assert agent._read_os_pretty_name() == "Alpine Linux"

    def test_neither_present_returns_unknown(self, fake_files):
        fake_files({"/host/etc/os-release": None, "/etc/os-release": None})

        assert agent._read_os_pretty_name() == "unknown"


class TestReadDeviceModel:
    def test_prefers_device_tree(self, fake_files):
        fake_files({"/proc/device-tree/model": "Raspberry Pi 5 Model B Rev 1.0\x00"})

        assert agent._read_device_model() == "Raspberry Pi 5 Model B Rev 1.0"

    @staticmethod
    def _patch_dmi(monkeypatch, content_by_path):
        # Path.read_text() doesn't reliably route through a patched
        # builtins.open (pathlib can hold its own internal reference,
        # confirmed not intercepted on this Python/platform) - patch the
        # Path method directly instead, dispatching on the instance's own
        # path string so unrelated Path.read_text() calls elsewhere are
        # unaffected.
        real_read_text = agent.Path.read_text

        def fake_read_text(self, *args, **kwargs):
            # str(WindowsPath(...)) renders with backslashes on Windows -
            # as_posix() gives a consistent forward-slash form regardless
            # of platform, matching how the dict keys are written here.
            key = self.as_posix()
            if key in content_by_path:
                content = content_by_path[key]
                if content is None:
                    raise OSError(f"simulated missing file: {key}")
                return content
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(agent.Path, "read_text", fake_read_text)

    def test_falls_back_to_dmi_on_x86(self, fake_files, monkeypatch):
        fake_files({"/proc/device-tree/model": None})
        self._patch_dmi(monkeypatch, {
            "/sys/class/dmi/id/sys_vendor": "Dell Inc.\n",
            "/sys/class/dmi/id/product_name": "OptiPlex 7090\n",
        })

        assert agent._read_device_model() == "Dell Inc. OptiPlex 7090"

    def test_neither_present_returns_unknown(self, fake_files, monkeypatch):
        fake_files({"/proc/device-tree/model": None})
        self._patch_dmi(monkeypatch, {
            "/sys/class/dmi/id/sys_vendor": None,
            "/sys/class/dmi/id/product_name": None,
        })

        assert agent._read_device_model() == "unknown"


class TestDiskStats:
    # os.statvfs is POSIX-only (doesn't exist on Windows at all) - raising=False
    # on every patch here so this file still collects/runs on a Windows dev
    # machine, not just Linux CI.

    @staticmethod
    def _fake_statvfs(f_frsize, f_blocks, f_bavail):
        return SimpleNamespace(f_frsize=f_frsize, f_blocks=f_blocks, f_bavail=f_bavail)

    def test_disk_total_gb(self, monkeypatch):
        # 4096-byte blocks, ~1M blocks -> ~4GB
        monkeypatch.setattr(
            agent.os, "statvfs",
            lambda path: TestDiskStats._fake_statvfs(4096, 1_048_576, 500_000),
            raising=False,
        )

        total_gb = agent._read_disk_total_gb()
        assert total_gb == pytest.approx(4.0, rel=0.01)

    def test_disk_usage_percent(self, monkeypatch):
        # blocks=1000, available=250 -> 75% used
        monkeypatch.setattr(
            agent.os, "statvfs",
            lambda path: TestDiskStats._fake_statvfs(4096, 1000, 250),
            raising=False,
        )

        assert agent._read_disk_usage_percent() == pytest.approx(75.0)

    def test_statvfs_failure_returns_none(self, monkeypatch):
        def raise_oserror(path):
            raise OSError("no such device")

        monkeypatch.setattr(agent.os, "statvfs", raise_oserror, raising=False)

        assert agent._read_disk_total_gb() is None
        assert agent._read_disk_usage_percent() is None
