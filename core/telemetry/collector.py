"""Linux telemetry collection with explicit provenance and bounded history.

This module is deliberately independent of HTTP/API code. A single collector
instance owns sampling state, so API consumers should read its snapshot rather
than instantiate one collector per browser connection.
"""

from __future__ import annotations

import csv
import os
import logging
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetricSample:
    """One measured value, or an explicit unavailable value (``value=None``)."""

    name: str
    value: float | int | str | None
    timestamp: datetime
    unit: str
    source: str
    quality: str = "measured"
    availability: str = "available"
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("sample timestamp must be timezone-aware")
        if self.availability not in {"available", "unavailable"}:
            raise ValueError("availability must be available or unavailable")
        if self.availability == "unavailable" and self.value is not None:
            raise ValueError("unavailable samples must not carry a fabricated value")


@dataclass(frozen=True)
class VMProcessIdentity:
    """A VM identity tied to an already identified host process/cgroup."""

    vm_id: str
    pid: int
    vcpu_count: int
    cgroup_path: str | None = None

    def __post_init__(self) -> None:
        if not self.vm_id.strip() or self.pid <= 0 or self.vcpu_count <= 0:
            raise ValueError("VM id, positive PID, and positive vCPU count are required")


class TelemetryCollector:
    """Thread-safe collector with 300-sample per-series rings and shared cycles."""

    def __init__(self, *, history_limit: int = 300, sample_interval: float = 1.0,
                 sensor_interval: float = 5.0, gpu_interval: float = 10.0,
                 proc_root: str | os.PathLike[str] = "/proc",
                 sys_root: str | os.PathLike[str] = "/sys",
                 sample_sink: Any | None = None) -> None:
        if history_limit < 1 or sample_interval <= 0 or sensor_interval <= 0 or gpu_interval <= 0:
            raise ValueError("history and sampling intervals must be positive")
        self.history_limit = history_limit
        self.sample_interval = sample_interval
        self.sensor_interval = sensor_interval
        self.gpu_interval = gpu_interval
        self.proc_root = Path(proc_root)
        self.sys_root = Path(sys_root)
        self.sample_sink = sample_sink
        self._lock = threading.RLock()
        self._history: dict[tuple[str, tuple[tuple[str, str], ...]], deque[MetricSample]] = {}
        self._last_host: tuple[float, tuple[MetricSample, ...]] | None = None
        self._last_sensor_samples: tuple[MetricSample, ...] = ()
        self._last_gpu_samples: tuple[MetricSample, ...] = ()
        self._last_sensor_at = 0.0
        self._last_gpu_at = 0.0
        self._last_cpu: dict[str, tuple[int, int]] = {}
        self._last_net: dict[str, tuple[int, int]] = {}
        self._last_disk: dict[str, tuple[int, int]] = {}
        self._last_vm_cpu: dict[str, tuple[int, float]] = {}
        self._last_qmp_io: dict[str, tuple[float, int, int]] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _publish(self, samples: Iterable[MetricSample]) -> tuple[MetricSample, ...]:
        result = tuple(samples)
        for sample in result:
            key = (sample.name, tuple(sorted(sample.labels.items())))
            ring = self._history.setdefault(key, deque(maxlen=self.history_limit))
            ring.append(sample)
        if result and self.sample_sink is not None:
            try:
                self.sample_sink(result)
            except Exception:
                # Optional historical persistence must never break live metrics.
                logger.warning("Unable to persist telemetry batch", exc_info=True)
        return result

    def history(self, name: str | None = None) -> dict[str, tuple[MetricSample, ...]]:
        """Return a bounded copy of history, keyed by metric name and labels."""
        with self._lock:
            return {
                f"{key[0]}[{','.join(k + '=' + v for k, v in key[1])}]": tuple(values)
                for key, values in self._history.items() if name is None or key[0] == name
            }

    def sample_host(self, *, force: bool = False) -> tuple[MetricSample, ...]:
        """Sample host once per configured interval, returning shared cached data."""
        with self._lock:
            now_mono = time.monotonic()
            if not force and self._last_host and now_mono - self._last_host[0] < self.sample_interval:
                return self._last_host[1]
            stamp = self._now()
            out: list[MetricSample] = []
            self._collect_cpu(out, stamp)
            self._collect_memory(out, stamp)
            self._collect_load(out, stamp)
            self._collect_uptime(out, stamp)
            self._collect_network(out, stamp, now_mono)
            self._collect_disk_io(out, stamp, now_mono)
            self._collect_filesystems(out, stamp)
            fresh_sensors: tuple[MetricSample, ...] = ()
            fresh_gpu: tuple[MetricSample, ...] = ()
            if now_mono - self._last_sensor_at >= self.sensor_interval:
                sensors: list[MetricSample] = []
                self._collect_temperatures(sensors, stamp)
                fresh_sensors = tuple(sensors)
                self._last_sensor_samples = fresh_sensors
                self._last_sensor_at = now_mono
            if now_mono - self._last_gpu_at >= self.gpu_interval:
                gpu: list[MetricSample] = []
                self._collect_gpu_metrics(gpu)
                fresh_gpu = tuple(gpu)
                self._last_gpu_samples = fresh_gpu
                self._last_gpu_at = now_mono
            self._publish((*out, *fresh_sensors, *fresh_gpu))
            # Keep slower sources in each live snapshot until their next real
            # reading. They remain timestamped at collection time, and are
            # published to history only once when freshly collected.
            samples = tuple(out) + self._last_sensor_samples + self._last_gpu_samples
            self._last_host = (now_mono, samples)
            return samples

    def sample_vm(self, identity: VMProcessIdentity) -> tuple[MetricSample, ...]:
        """Measure only external process/cgroup facts for a caller-identified VM.

        No guest memory or network usage is inferred. Missing/permission-denied
        sources are represented as unavailable samples, never zero.
        """
        with self._lock:
            stamp = self._now()
            labels = {"vm_id": identity.vm_id}
            out: list[MetricSample] = []
            stat = self._read_proc_stat(identity.pid)
            if stat is None:
                self._unavailable(out, "vm.process.cpu_percent", "%", "procfs:pid", labels)
                self._unavailable(out, "vm.process.rss_bytes", "bytes", "procfs:pid", labels)
                self._unavailable(out, "vm.process.uptime_seconds", "s", "procfs:pid", labels)
                self._unavailable(out, "vm.process.io_read_bytes", "bytes", "procfs:pid/io", labels)
                self._unavailable(out, "vm.process.io_write_bytes", "bytes", "procfs:pid/io", labels)
            else:
                ticks, start_ticks, rss = stat
                uptime = self._read_uptime()
                elapsed = max(0.0, uptime - start_ticks / self._clock_ticks())
                old = self._last_vm_cpu.get(identity.vm_id)
                cpu_pct = None
                if old and time.monotonic() > old[1]:
                    delta = max(0, ticks - old[0]) / self._clock_ticks()
                    cpu_pct = 100.0 * delta / ((time.monotonic() - old[1]) * identity.vcpu_count)
                self._last_vm_cpu[identity.vm_id] = (ticks, time.monotonic())
                self._value(out, "vm.process.cpu_percent", cpu_pct, "%", "procfs:pid/stat",
                            labels, quality="measured_normalized_by_configured_vcpus")
                self._value(out, "vm.process.rss_bytes", rss, "bytes", "procfs:pid/statm", labels)
                self._value(out, "vm.process.uptime_seconds", elapsed, "s", "procfs:pid/stat", labels)
                io = self._read_pid_io(identity.pid)
                for key, name in (("read_bytes", "vm.process.io_read_bytes"),
                                  ("write_bytes", "vm.process.io_write_bytes")):
                    self._value(out, name, io.get(key), "bytes", "procfs:pid/io", labels)
            if identity.cgroup_path:
                self._collect_cgroup(identity, out, labels)
            else:
                for name, unit in (("vm.cgroup.cpu_usage_seconds", "s"),
                                   ("vm.cgroup.memory_current_bytes", "bytes"),
                                   ("vm.cgroup.memory_limit_bytes", "bytes"),
                                   ("vm.cgroup.io_read_bytes", "bytes"),
                                   ("vm.cgroup.io_write_bytes", "bytes")):
                    self._unavailable(out, name, unit, "cgroup:not_configured", labels)
            self._unavailable(out, "vm.guest.memory_used_bytes", "bytes", "guest_agent:absent", labels)
            self._unavailable(out, "vm.network.rx_bytes", "bytes", "vm_network:unattributed", labels)
            self._unavailable(out, "vm.network.tx_bytes", "bytes", "vm_network:unattributed", labels)
            return self._publish(out)

    def sample_qmp(self, vm_id: str, qmp: Any) -> tuple[MetricSample, ...]:
        """Read attributable QMP facts for one VM using its shared broker.

        Memory is reported as QEMU-assigned memory, never as guest-internal
        memory use. Block counters are converted to rates using per-VM deltas.
        QMP failures remain explicit unavailable samples.
        """
        if not vm_id.strip():
            raise ValueError("VM id is required")
        labels = {"vm_id": vm_id}
        out: list[MetricSample] = []
        try:
            memory = qmp.command("query-memory-size-summary")
            if not isinstance(memory, dict):
                memory = {}
            base = memory.get("base-memory")
            plugged = memory.get("plugged-memory", 0)
            if isinstance(base, int) and base >= 0 and isinstance(plugged, int) and plugged >= 0:
                self._value(out, "vm.qmp.memory_assigned_bytes", base + plugged, "bytes",
                            "qmp:query-memory-size-summary", labels,
                            "qemu_assigned_not_guest_usage")
            else:
                self._unavailable(out, "vm.qmp.memory_assigned_bytes", "bytes",
                                  "qmp:query-memory-size-summary:invalid", labels)
        except Exception:
            self._unavailable(out, "vm.qmp.memory_assigned_bytes", "bytes",
                              "qmp:query-memory-size-summary:failed", labels)

        try:
            blocks = qmp.command("query-blockstats")
            if not isinstance(blocks, list):
                raise ValueError("query-blockstats response is not a list")
            read_total = 0
            write_total = 0
            valid = False
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                stats = block.get("stats")
                if not isinstance(stats, dict):
                    continue
                read_value = stats.get("rd_bytes")
                write_value = stats.get("wr_bytes")
                if isinstance(read_value, int) and read_value >= 0:
                    read_total += read_value
                    valid = True
                if isinstance(write_value, int) and write_value >= 0:
                    write_total += write_value
                    valid = True
            now = time.monotonic()
            previous = self._last_qmp_io.get(vm_id)
            self._last_qmp_io[vm_id] = (now, read_total, write_total)
            if valid and previous and now > previous[0]:
                elapsed = now - previous[0]
                read_rate = max(0.0, (read_total - previous[1]) / elapsed)
                write_rate = max(0.0, (write_total - previous[2]) / elapsed)
                self._value(out, "vm.qmp.disk_read_bytes_per_sec", read_rate, "bytes/s",
                            "qmp:query-blockstats", labels)
                self._value(out, "vm.qmp.disk_write_bytes_per_sec", write_rate, "bytes/s",
                            "qmp:query-blockstats", labels)
            else:
                self._unavailable(out, "vm.qmp.disk_read_bytes_per_sec", "bytes/s",
                                  "qmp:query-blockstats:warming_up" if valid else "qmp:query-blockstats:unavailable", labels)
                self._unavailable(out, "vm.qmp.disk_write_bytes_per_sec", "bytes/s",
                                  "qmp:query-blockstats:warming_up" if valid else "qmp:query-blockstats:unavailable", labels)
        except Exception:
            self._unavailable(out, "vm.qmp.disk_read_bytes_per_sec", "bytes/s",
                              "qmp:query-blockstats:failed", labels)
            self._unavailable(out, "vm.qmp.disk_write_bytes_per_sec", "bytes/s",
                              "qmp:query-blockstats:failed", labels)
        return self._publish(out)

    def snapshot(self, identities: Iterable[VMProcessIdentity] = ()) -> dict[str, tuple[MetricSample, ...]]:
        """Collect one shared host cycle and each supplied, explicit VM identity."""
        result = {"host": self.sample_host()}
        for identity in identities:
            result[f"vm:{identity.vm_id}"] = self.sample_vm(identity)
        return result

    def _value(self, out: list[MetricSample], name: str, value: float | int | str | None,
               unit: str, source: str, labels: dict[str, str] | None = None,
               quality: str = "measured") -> None:
        avail = "available" if value is not None else "unavailable"
        out.append(MetricSample(name, value, self._now(), unit, source, quality, avail, labels or {}))

    def _unavailable(self, out: list[MetricSample], name: str, unit: str, source: str,
                     labels: dict[str, str] | None = None) -> None:
        self._value(out, name, None, unit, source, labels, "unavailable")

    def _read_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="ascii", errors="replace")
        except (OSError, ValueError):
            return None

    def _collect_cpu(self, out: list[MetricSample], stamp: datetime) -> None:
        text = self._read_text(self.proc_root / "stat")
        if text is None:
            self._unavailable(out, "host.cpu_percent", "%", "procfs:/stat")
            return
        cores: list[tuple[str, list[int]]] = []
        for line in text.splitlines():
            parts = line.split()
            if not parts or not (parts[0] == "cpu" or (parts[0].startswith("cpu") and parts[0][3:].isdigit())):
                continue
            try:
                cores.append((parts[0], [int(x) for x in parts[1:]]))
            except ValueError:
                continue
        for name, counters in cores:
            if len(counters) < 4:
                continue
            total = sum(counters)
            idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
            old = self._last_cpu.get(name)
            value = None
            if old:
                dt, di = total - old[0], idle - old[1]
                if dt > 0:
                    value = max(0.0, min(100.0, 100.0 * (dt - di) / dt))
            self._last_cpu[name] = (total, idle)
            metric = "host.cpu_percent" if name == "cpu" else "host.cpu_core_percent"
            self._value(out, metric, value, "%", "procfs:/stat", {"core": "total" if name == "cpu" else name[3:]},
                        "measured_interval_delta" if value is not None else "warming_up")

    def _collect_memory(self, out: list[MetricSample], stamp: datetime) -> None:
        values: dict[str, int] = {}
        raw = self._read_text(self.proc_root / "meminfo")
        if raw:
            for line in raw.splitlines():
                parts = line.replace(":", "").split()
                if len(parts) >= 2:
                    try: values[parts[0]] = int(parts[1]) * 1024
                    except ValueError: pass
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        used = total - available if total is not None and available is not None else None
        for name, val in (("host.memory_total_bytes", total), ("host.memory_available_bytes", available),
                          ("host.memory_used_bytes", used), ("host.memory_cached_bytes", values.get("Cached")),
                          ("host.swap_total_bytes", values.get("SwapTotal")),
                          ("host.swap_free_bytes", values.get("SwapFree"))):
            self._value(out, name, val, "bytes", "procfs:/meminfo")

    def _collect_load(self, out: list[MetricSample], stamp: datetime) -> None:
        raw = self._read_text(self.proc_root / "loadavg")
        vals = raw.split()[:3] if raw else []
        for idx, name in enumerate(("host.load_1m", "host.load_5m", "host.load_15m")):
            try: val = float(vals[idx])
            except (IndexError, ValueError): val = None
            self._value(out, name, val, "processes", "procfs:/loadavg")

    def _read_uptime(self) -> float | None:
        try: return float((self.proc_root / "uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError): return None

    def _collect_uptime(self, out: list[MetricSample], stamp: datetime) -> None:
        self._value(out, "host.uptime_seconds", self._read_uptime(), "s", "procfs:/uptime")

    @staticmethod
    def _clock_ticks() -> int:
        try: return int(os.sysconf("SC_CLK_TCK"))
        except (ValueError, OSError, AttributeError): return 100

    def _collect_network(self, out: list[MetricSample], stamp: datetime, now: float) -> None:
        raw = self._read_text(self.proc_root / "net/dev")
        if raw is None:
            self._unavailable(out, "host.network_rx_bytes_per_second", "bytes/s", "procfs:/net/dev")
            return
        for line in raw.splitlines()[2:]:
            if ":" not in line: continue
            iface, counters = line.split(":", 1)
            nums = counters.split()
            try: rx, tx = int(nums[0]), int(nums[8])
            except (IndexError, ValueError): continue
            old = self._last_net.get(iface.strip())
            labels = {"interface": iface.strip()}
            for metric, current, old_index in (("host.network_rx_bytes_per_second", rx, 0),
                                                ("host.network_tx_bytes_per_second", tx, 1)):
                rate = None if old is None or now <= old[2] else max(0, current - old[old_index]) / (now - old[2])
                self._value(out, metric, rate, "bytes/s", "procfs:/net/dev", labels,
                            "measured_interval_delta" if rate is not None else "warming_up")
            self._last_net[iface.strip()] = (rx, tx, now)

    def _collect_disk_io(self, out: list[MetricSample], stamp: datetime, now: float) -> None:
        raw = self._read_text(self.proc_root / "diskstats")
        if raw is None: return
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 14: continue
            device = parts[2]
            try: read_bytes, write_bytes = int(parts[5]) * 512, int(parts[9]) * 512
            except ValueError: continue
            old = self._last_disk.get(device)
            for metric, curr, idx in (("host.disk_read_bytes_per_second", read_bytes, 0),
                                      ("host.disk_write_bytes_per_second", write_bytes, 1)):
                rate = None if old is None or now <= old[2] else max(0, curr - old[idx]) / (now - old[2])
                self._value(out, metric, rate, "bytes/s", "procfs:/diskstats", {"device": device},
                            "measured_interval_delta" if rate is not None else "warming_up")
            self._last_disk[device] = (read_bytes, write_bytes, now)

    def _collect_filesystems(self, out: list[MetricSample], stamp: datetime) -> None:
        try: mounts = (self.proc_root / "mounts").read_text(errors="replace").splitlines()
        except OSError: mounts = []
        seen: set[str] = set()
        for line in mounts:
            fields = line.split()
            if len(fields) < 3: continue
            mount = fields[1].replace("\\040", " ")
            if mount in seen or fields[2] in {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2"}: continue
            seen.add(mount)
            try:
                st = os.statvfs(mount); total = st.f_blocks * st.f_frsize; free = st.f_bavail * st.f_frsize
            except OSError: continue
            labels = {"mount": mount}
            self._value(out, "host.filesystem_total_bytes", total, "bytes", "statvfs", labels)
            self._value(out, "host.filesystem_available_bytes", free, "bytes", "statvfs", labels)

    def _collect_temperatures(self, out: list[MetricSample], stamp: datetime) -> None:
        root = self.sys_root / "class/hwmon"
        found = False
        try: sensors = tuple(sorted(root.glob("hwmon*/temp*_input")))
        except OSError: sensors = ()
        try:
            chips = {
                name.strip() for path in root.glob("hwmon*")
                if (name := self._read_text(path / "name")) and name.strip()
            }
        except OSError:
            chips = set()
        # Dell's dell_smm and dell_ddv drivers expose the same temperature
        # channels on supported laptops. Prefer dell_ddv when both are loaded:
        # it supplies proper CPU/GPU/Ambient labels while dell_smm mirrors the
        # readings under a second set of tempN entries.
        duplicate_dell_smm = "dell_ddv" in chips
        for path in sensors:
            try: milli = int(path.read_text().strip())
            except (OSError, ValueError): continue
            label = path.name.removesuffix("_input")
            chip = (self._read_text(path.parent / "name") or "").strip() or path.parent.name
            if chip == "dell_smm" and duplicate_dell_smm:
                continue
            human_label = (self._read_text(path.with_name(f"{label}_label")) or "").strip()
            labels = {"sensor": label, "chip": chip, "hwmon": path.parent.name}
            if human_label:
                labels["sensor_label"] = human_label
            bdf = self._pci_bdf_for_path(path.parent / "device")
            if bdf:
                labels["pci_bdf"] = bdf
            self._value(out, "host.temperature_celsius", milli / 1000.0, "°C", "sysfs:hwmon", labels)
            if bdf and self._is_pci_display_device(bdf):
                self._value(out, "host.gpu.temperature_celsius", milli / 1000.0, "°C",
                            "sysfs:hwmon", {"pci_bdf": bdf, "chip": chip, "sensor": label,
                                            "hwmon": path.parent.name})
            found = True
        if not found:
            self._unavailable(out, "host.temperature_celsius", "°C", "sysfs:hwmon")

    @staticmethod
    def _pci_bdf_for_path(path: Path) -> str | None:
        """Resolve sysfs device links to a canonical physical PCI BDF."""
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        for part in reversed(resolved.parts):
            if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", part):
                return part.lower()
        return None

    def _is_pci_display_device(self, bdf: str) -> bool:
        raw = self._read_text(self.sys_root / "bus/pci/devices" / bdf / "class")
        return bool(raw and raw.lower().startswith("0x03"))

    def _collect_gpu_metrics(self, out: list[MetricSample]) -> None:
        """Read shared, reduced-frequency GPU counters by physical PCI BDF.

        DRM sysfs remains the portable provider. NVIDIA's proprietary driver
        does not expose the same counters there, so use one bounded
        ``nvidia-smi`` query per shared collection cycle when available. Neither
        provider is invoked once per browser client.
        """
        nvidia_bdfs = self._collect_nvidia_gpu_metrics(out)
        root = self.sys_root / "class/drm"
        try:
            cards = tuple(root.glob("card[0-9]*"))
        except OSError:
            cards = ()
        seen: set[str] = set()
        for card in cards:
            device = card / "device"
            bdf = self._pci_bdf_for_path(device)
            if not bdf or bdf in seen or bdf in nvidia_bdfs or not self._is_pci_display_device(bdf):
                continue
            seen.add(bdf)
            labels = {"pci_bdf": bdf}
            for filename, metric, unit, divisor in (
                ("gpu_busy_percent", "host.gpu.busy_percent", "%", 1.0),
                ("mem_info_vram_used", "host.gpu.vram_used_bytes", "bytes", 1.0),
                ("mem_info_vram_total", "host.gpu.vram_total_bytes", "bytes", 1.0),
            ):
                raw = self._read_text(device / filename)
                try:
                    value = float(raw) / divisor if raw is not None else None
                    if value is not None and (value < 0 or (metric.endswith("busy_percent") and value > 100)):
                        value = None
                except ValueError:
                    value = None
                self._value(out, metric, value, unit, f"sysfs:drm/{filename}", labels,
                            "measured" if value is not None else "unavailable")

    @staticmethod
    def _nvidia_bdf(raw: str) -> str | None:
        match = re.fullmatch(
            r"\s*([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])\s*",
            raw,
        )
        if not match:
            return None
        domain, bus, device, function = (int(value, 16) for value in match.groups())
        if domain > 0xFFFF or bus > 0xFF or device > 0x1F:
            return None
        return f"{domain:04x}:{bus:02x}:{device:02x}.{function}"

    @staticmethod
    def _nvidia_number(raw: str, *, minimum: float = 0.0, maximum: float | None = None) -> float | None:
        value = raw.strip()
        if not value or value.lower() in {"n/a", "na", "[n/a]", "not supported", "unsupported"}:
            return None
        value = re.sub(r"\s*(?:%|w|c|mib|mb)\s*$", "", value, flags=re.IGNORECASE)
        try:
            number = float(value)
        except ValueError:
            return None
        if not (number >= minimum and (maximum is None or number <= maximum)):
            return None
        return number

    def _collect_nvidia_gpu_metrics(self, out: list[MetricSample]) -> set[str]:
        """Query NVIDIA telemetry once, with a strict timeout and bounded output."""
        binary = shutil.which("nvidia-smi")
        if not binary:
            return set()
        query = (
            "pci.bus_id,temperature.gpu,utilization.gpu,memory.used,memory.total,"
            "power.draw,fan.speed"
        )
        try:
            result = subprocess.run(
                [binary, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        if result.returncode != 0 or len(result.stdout) > 64 * 1024:
            return set()

        seen: set[str] = set()
        try:
            rows = csv.reader(result.stdout.splitlines())
            for index, row in enumerate(rows):
                if index >= 64 or len(row) != 7:
                    continue
                bdf = self._nvidia_bdf(row[0])
                if not bdf or bdf in seen:
                    continue
                seen.add(bdf)
                labels = {"pci_bdf": bdf}
                temperature = self._nvidia_number(row[1], maximum=200)
                busy = self._nvidia_number(row[2], maximum=100)
                memory_used = self._nvidia_number(row[3])
                memory_total = self._nvidia_number(row[4])
                power = self._nvidia_number(row[5])
                fan = self._nvidia_number(row[6], maximum=100)
                self._value(out, "host.gpu.temperature_celsius", temperature, "°C", "nvidia-smi", labels)
                self._value(out, "host.gpu.busy_percent", busy, "%", "nvidia-smi", labels)
                self._value(out, "host.gpu.vram_used_bytes",
                            round(memory_used * 1024 * 1024) if memory_used is not None else None,
                            "bytes", "nvidia-smi:memory.used_mib", labels)
                self._value(out, "host.gpu.vram_total_bytes",
                            round(memory_total * 1024 * 1024) if memory_total is not None else None,
                            "bytes", "nvidia-smi:memory.total_mib", labels)
                self._value(out, "host.gpu.power_watts", power, "W", "nvidia-smi", labels)
                self._value(out, "host.gpu.fan_percent", fan, "%", "nvidia-smi", labels)
        except csv.Error:
            return set()
        return seen

    def _read_proc_stat(self, pid: int) -> tuple[int, int, int] | None:
        raw = self._read_text(self.proc_root / str(pid) / "stat")
        if raw is None: return None
        try:
            # comm may contain spaces and parentheses; fields after its final ')' start at field 3.
            fields = raw[raw.rfind(")") + 2:].split()
            ticks = int(fields[11]) + int(fields[12])
            start = int(fields[19])
            resident_pages = int(fields[21])
            return ticks, start, resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, IndexError, OSError): return None

    def _read_pid_io(self, pid: int) -> dict[str, int]:
        raw = self._read_text(self.proc_root / str(pid) / "io")
        result: dict[str, int] = {}
        for line in raw.splitlines() if raw else ():
            key, _, value = line.partition(":")
            if key in {"read_bytes", "write_bytes"}:
                try: result[key] = int(value)
                except ValueError: pass
        return result

    def _collect_cgroup(self, identity: VMProcessIdentity, out: list[MetricSample], labels: dict[str, str]) -> None:
        # cgroup path is supplied by trusted VM supervisor code; reject traversal.
        relative = Path(identity.cgroup_path or "")
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("cgroup path must be relative and cannot traverse")
        root = self.sys_root / "fs/cgroup" / relative
        cpu_raw = self._read_text(root / "cpu.stat")
        cpu_usec = None
        if cpu_raw:
            try: cpu_usec = int(dict(line.split() for line in cpu_raw.splitlines()).get("usage_usec", ""))
            except (ValueError, TypeError): pass
        self._value(out, "vm.cgroup.cpu_usage_seconds", cpu_usec / 1_000_000 if cpu_usec is not None else None,
                    "s", "cgroupv2:cpu.stat", labels)
        for fname, metric in (("memory.current", "vm.cgroup.memory_current_bytes"),
                              ("memory.max", "vm.cgroup.memory_limit_bytes")):
            raw = self._read_text(root / fname)
            try: value = int(raw.strip()) if raw and raw.strip() != "max" else None
            except ValueError: value = None
            self._value(out, metric, value, "bytes", f"cgroupv2:{fname}", labels)
        io_raw = self._read_text(root / "io.stat")
        totals = {"rbytes": 0, "wbytes": 0}
        if io_raw:
            for line in io_raw.splitlines():
                for part in line.split()[1:]:
                    key, _, val = part.partition("=")
                    if key in totals:
                        try: totals[key] += int(val)
                        except ValueError: pass
        for key, metric in (("rbytes", "vm.cgroup.io_read_bytes"), ("wbytes", "vm.cgroup.io_write_bytes")):
            self._value(out, metric, totals[key] if io_raw is not None else None, "bytes", "cgroupv2:io.stat", labels)
