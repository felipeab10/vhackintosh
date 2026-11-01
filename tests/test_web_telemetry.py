"""Focused checks for the web manager's first telemetry collector layer."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.telemetry import TelemetryCollector, VMProcessIdentity


class TelemetryCollectorTests(unittest.TestCase):
    def test_gpu_sysfs_metrics_are_optional_physical_and_reduced_frequency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc, sys = root / "proc", root / "sys"
            (proc / "net").mkdir(parents=True)
            (proc / "stat").write_text("cpu 10 0 5 85 0 0 0 0 0 0\n")
            (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
            (proc / "loadavg").write_text("0 0 0 1/1 1\n")
            (proc / "uptime").write_text("10 0\n")
            (proc / "net/dev").write_text("header\nheader\n")
            (proc / "diskstats").write_text("")
            (proc / "mounts").write_text("")

            bdf = "0000:03:00.0"
            physical = sys / "devices/pci0000:00" / bdf
            physical.mkdir(parents=True)
            (physical / "class").write_text("0x030000")
            pci = sys / "bus/pci/devices"
            pci.mkdir(parents=True)
            (pci / bdf).symlink_to(physical)
            card = sys / "class/drm/card0"
            card.mkdir(parents=True)
            (card / "device").symlink_to(physical)
            (physical / "gpu_busy_percent").write_text("37\n")
            (physical / "mem_info_vram_used").write_text("4096\n")
            (physical / "mem_info_vram_total").write_text("8192\n")
            hwmon = sys / "class/hwmon/hwmon0"
            hwmon.mkdir(parents=True)
            (hwmon / "device").symlink_to(physical)
            (hwmon / "temp1_input").write_text("62000\n")

            collector = TelemetryCollector(proc_root=proc, sys_root=sys, gpu_interval=10)
            with patch("core.telemetry.collector.shutil.which", return_value=None):
                with patch("core.telemetry.collector.time.monotonic", side_effect=[100.0, 101.0]):
                    first = collector.sample_host(force=True)
                    second = collector.sample_host(force=True)
            by_name = {sample.name: sample for sample in first}
            busy = by_name["host.gpu.busy_percent"]
            self.assertEqual(busy.value, 37)
            self.assertEqual(busy.labels, {"pci_bdf": bdf})
            self.assertEqual(by_name["host.gpu.vram_used_bytes"].value, 4096)
            self.assertEqual(by_name["host.gpu.temperature_celsius"].value, 62)
            self.assertEqual(by_name["host.temperature_celsius"].labels["pci_bdf"], bdf)
            second_by_name = {sample.name: sample for sample in second}
            self.assertEqual(second_by_name["host.gpu.busy_percent"].value, 37)
            self.assertEqual(second_by_name["host.gpu.busy_percent"].timestamp, busy.timestamp)
            self.assertEqual(second_by_name["host.temperature_celsius"].value, 62)
            self.assertEqual(len(collector.history("host.gpu.busy_percent")), 1)

    def test_nvidia_smi_metrics_are_attributed_and_converted_to_shared_units(self):
        collector = TelemetryCollector()
        result = type("Completed", (), {
            "returncode": 0,
            "stdout": "00000000:01:00.0, 64, 13, 293, 4096, 5.27, [N/A]\\n",
        })()
        samples = []
        with patch("core.telemetry.collector.shutil.which", return_value="/usr/bin/nvidia-smi") as which:
            with patch("core.telemetry.collector.subprocess.run", return_value=result) as run:
                handled = collector._collect_nvidia_gpu_metrics(samples)

        self.assertEqual(handled, {"0000:01:00.0"})
        which.assert_called_once_with("nvidia-smi")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        by_name = {sample.name: sample for sample in samples}
        self.assertEqual(by_name["host.gpu.temperature_celsius"].value, 64)
        self.assertEqual(by_name["host.gpu.busy_percent"].value, 13)
        self.assertEqual(by_name["host.gpu.vram_used_bytes"].value, 293 * 1024 * 1024)
        self.assertEqual(by_name["host.gpu.vram_total_bytes"].value, 4096 * 1024 * 1024)
        self.assertEqual(by_name["host.gpu.power_watts"].value, 5.27)
        self.assertEqual(by_name["host.gpu.fan_percent"].availability, "unavailable")
        self.assertTrue(all(sample.labels == {"pci_bdf": "0000:01:00.0"} for sample in samples))

    def test_nvidia_smi_timeout_and_invalid_bus_id_fail_closed(self):
        collector = TelemetryCollector()
        samples = []
        with patch("core.telemetry.collector.shutil.which", return_value="/usr/bin/nvidia-smi"):
            with patch("core.telemetry.collector.subprocess.run", side_effect=TimeoutError):
                self.assertEqual(collector._collect_nvidia_gpu_metrics(samples), set())
        self.assertEqual(samples, [])

        result = type("Completed", (), {"returncode": 0, "stdout": "not-a-bdf, 64, 13, 293, 4096, 5.27, 32\\n"})()
        with patch("core.telemetry.collector.shutil.which", return_value="/usr/bin/nvidia-smi"):
            with patch("core.telemetry.collector.subprocess.run", return_value=result):
                self.assertEqual(collector._collect_nvidia_gpu_metrics(samples), set())
        self.assertEqual(samples, [])

    def test_temperature_labels_distinguish_same_sensor_name_on_different_chips(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sys = root / "sys"
            for hwmon, chip, value in (("hwmon0", "coretemp", 42000), ("hwmon1", "nvme", 51000), ("hwmon2", "coretemp", 47000)):
                directory = sys / "class/hwmon" / hwmon
                directory.mkdir(parents=True)
                (directory / "name").write_text(f"{chip}\n")
                (directory / "temp1_input").write_text(f"{value}\n")
            collector = TelemetryCollector(proc_root=root / "proc", sys_root=sys)
            samples = []
            collector._collect_temperatures(samples, datetime.now(timezone.utc))
            temperatures = [sample for sample in samples if sample.name == "host.temperature_celsius"]

        self.assertEqual(len(temperatures), 3)
        self.assertEqual({sample.labels["chip"] for sample in temperatures}, {"coretemp", "nvme"})
        self.assertEqual({sample.labels["hwmon"] for sample in temperatures}, {"hwmon0", "hwmon1", "hwmon2"})
        self.assertEqual(len({tuple(sorted(sample.labels.items())) for sample in temperatures}), 3)

    def test_dell_temperature_aliases_prefer_labeled_ddv_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sys" / "class" / "hwmon"
            for hwmon, chip, label, value in (
                ("hwmon4", "dell_smm", None, 75000),
                ("hwmon9", "dell_ddv", "CPU", 75000),
                ("hwmon5", "dell_smm", None, 48000),
                ("hwmon8", "dell_ddv", "Ambient", 48000),
            ):
                directory = root / hwmon
                directory.mkdir(parents=True)
                (directory / "name").write_text(f"{chip}\n")
                (directory / "temp1_input").write_text(f"{value}\n")
                if label:
                    (directory / "temp1_label").write_text(f"{label}\n")
            collector = TelemetryCollector(sys_root=root.parents[1])
            samples = []
            collector._collect_temperatures(samples, datetime.now(timezone.utc))
            temperatures = [sample for sample in samples if sample.name == "host.temperature_celsius"]

        self.assertEqual(len(temperatures), 2)
        self.assertEqual({sample.labels["chip"] for sample in temperatures}, {"dell_ddv"})
        self.assertEqual({sample.labels["sensor_label"] for sample in temperatures}, {"CPU", "Ambient"})

    def test_host_samples_have_utc_provenance_and_shared_interval(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp) / "proc"
            sys = Path(temp) / "sys"
            (proc / "net").mkdir(parents=True)
            (proc / "stat").write_text("cpu 10 0 5 85 0 0 0 0 0 0\ncpu0 10 0 5 85 0 0 0 0 0 0\n")
            (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\nCached: 100 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
            (proc / "loadavg").write_text("0.10 0.20 0.30 1/10 11\n")
            (proc / "uptime").write_text("123.5 100.0\n")
            (proc / "net/dev").write_text("header\nheader\nlo: 1 0 0 0 0 0 0 0 2 0 0 0 0 0 0 0\n")
            (proc / "diskstats").write_text("")
            (proc / "mounts").write_text("")
            collector = TelemetryCollector(proc_root=proc, sys_root=sys, history_limit=2)
            first = collector.sample_host()
            second = collector.sample_host()
            self.assertIs(first, second)
            self.assertTrue(first)
            self.assertTrue(all(s.timestamp.tzinfo == timezone.utc for s in first))
            memory = next(s for s in first if s.name == "host.memory_used_bytes")
            self.assertEqual(memory.value, 750 * 1024)
            cpu = next(s for s in first if s.name == "host.cpu_percent")
            self.assertIsNone(cpu.value)  # first interval is a baseline, not fabricated zero
            self.assertEqual(cpu.availability, "unavailable")

    def test_unknown_vm_has_explicitly_unavailable_external_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            collector = TelemetryCollector(proc_root=Path(temp) / "proc", sys_root=Path(temp) / "sys")
            samples = collector.sample_vm(VMProcessIdentity("vm-a", 999999, 4))
            by_name = {sample.name: sample for sample in samples}
            self.assertEqual(by_name["vm.process.rss_bytes"].availability, "unavailable")
            self.assertIsNone(by_name["vm.network.rx_bytes"].value)
            self.assertIsNone(by_name["vm.guest.memory_used_bytes"].value)
            self.assertEqual(by_name["vm.network.rx_bytes"].source, "vm_network:unattributed")

    def test_vm_process_cpu_is_normalized_and_cgroup_facts_are_attributed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc, sys = root / "proc", root / "sys"
            process = proc / "321"
            process.mkdir(parents=True)
            (proc / "uptime").write_text("500.0 0.0\n")
            (process / "io").write_text("read_bytes: 4096\nwrite_bytes: 1024\n")
            cgroup = sys / "fs/cgroup/vms/vm-1"
            cgroup.mkdir(parents=True)
            (cgroup / "cpu.stat").write_text("usage_usec 1250000\n")
            (cgroup / "memory.current").write_text("8192\n")
            (cgroup / "memory.max").write_text("16384\n")
            (cgroup / "io.stat").write_text("8:0 rbytes=2048 wbytes=512\n")

            def stat(ticks):
                fields = ["S"] + ["0"] * 21
                fields[11], fields[12], fields[19], fields[21] = str(ticks), "0", "1", "2"
                return f"321 (qemu macOS) " + " ".join(fields) + "\n"

            (process / "stat").write_text(stat(100))
            collector = TelemetryCollector(proc_root=proc, sys_root=sys)
            with patch("core.telemetry.collector.time.monotonic", side_effect=[1000.0]):
                collector.sample_vm(VMProcessIdentity("vm-1", 321, 4, "vms/vm-1"))
            (process / "stat").write_text(stat(200))
            with patch("core.telemetry.collector.time.monotonic", side_effect=[1001.0, 1001.0, 1001.0]):
                samples = collector.sample_vm(VMProcessIdentity("vm-1", 321, 4, "vms/vm-1"))
            by_name = {sample.name: sample for sample in samples}
            self.assertAlmostEqual(by_name["vm.process.cpu_percent"].value, 25.0)
            self.assertEqual(by_name["vm.process.cpu_percent"].quality, "measured_normalized_by_configured_vcpus")
            self.assertEqual(by_name["vm.cgroup.memory_current_bytes"].value, 8192)
            self.assertEqual(by_name["vm.cgroup.memory_limit_bytes"].value, 16384)
            self.assertEqual(by_name["vm.cgroup.io_read_bytes"].value, 2048)
            self.assertEqual(by_name["vm.process.io_read_bytes"].value, 4096)

    def test_history_is_bounded_and_traversing_cgroup_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            collector = TelemetryCollector(proc_root=Path(temp) / "proc", sys_root=Path(temp) / "sys", history_limit=2)
            ident = VMProcessIdentity("vm-a", 123, 2)
            for _ in range(4):
                collector.sample_vm(ident)
            history = collector.history("vm.network.rx_bytes")
            self.assertEqual(len(next(iter(history.values()))), 2)
            with self.assertRaises(ValueError):
                collector.sample_vm(VMProcessIdentity("vm-b", 123, 2, "../../etc"))

    def test_qmp_memory_is_assigned_memory_and_block_io_uses_deltas(self):
        class FakeQmp:
            def __init__(self):
                self.read = 100
                self.written = 50

            def command(self, name):
                if name == "query-memory-size-summary":
                    return {"base-memory": 4096, "plugged-memory": 1024}
                if name == "query-blockstats":
                    return [{"device": "disk0", "stats": {"rd_bytes": self.read, "wr_bytes": self.written}}]
                raise AssertionError(name)

        collector = TelemetryCollector()
        fake = FakeQmp()
        first = {sample.name: sample for sample in collector.sample_qmp("vm-a", fake)}
        self.assertEqual(first["vm.qmp.memory_assigned_bytes"].value, 5120)
        self.assertEqual(first["vm.qmp.memory_assigned_bytes"].quality,
                         "qemu_assigned_not_guest_usage")
        self.assertEqual(first["vm.qmp.disk_read_bytes_per_sec"].availability, "unavailable")
        fake.read += 100
        fake.written += 40
        second = {sample.name: sample for sample in collector.sample_qmp("vm-a", fake)}
        self.assertGreater(second["vm.qmp.disk_read_bytes_per_sec"].value, 0)
        self.assertGreater(second["vm.qmp.disk_write_bytes_per_sec"].value, 0)

    def test_qmp_failure_returns_unavailable_values(self):
        class BrokenQmp:
            def command(self, _name):
                raise TimeoutError("no QMP")

        samples = TelemetryCollector().sample_qmp("vm-a", BrokenQmp())
        self.assertTrue(samples)
        self.assertTrue(all(sample.availability == "unavailable" for sample in samples))


if __name__ == "__main__":
    unittest.main()
