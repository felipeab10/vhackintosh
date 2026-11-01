from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.telemetry import MetricSample, TelemetryCollector, TelemetryHistoryStore
from core.telemetry.alerts import HostAlertStore


class TelemetryHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
        self.store = TelemetryHistoryStore(Path(self.temp.name) / "history.sqlite", clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def sample(self, seconds: int, value, *, availability="available", labels=None):
        return MetricSample(
            "host.cpu_percent", value,
            self.now - timedelta(seconds=seconds), "%", "procfs:stat",
            availability=availability, labels=labels or {},
        )

    def test_aggregates_numeric_samples_at_both_resolutions_and_keeps_gaps_explicit(self):
        self.store.record([
            self.sample(1, 10),
            self.sample(2, 20),
            self.sample(3, None, availability="unavailable"),
        ])

        ten = self.store.query("host.cpu_percent", resolution=10)
        sixty = self.store.query("host.cpu_percent", resolution=60)
        self.assertEqual(len(ten), 1)
        self.assertEqual(len(sixty), 1)
        for point in (ten[0], sixty[0]):
            self.assertEqual(point["average"], 15)
            self.assertEqual(point["minimum"], 10)
            self.assertEqual(point["maximum"], 20)
            self.assertEqual(point["sample_count"], 2)
            self.assertEqual(point["unavailable_count"], 1)
            self.assertEqual(point["availability"], "partial")

    def test_all_unavailable_bucket_has_null_numeric_fields_not_zero(self):
        self.store.record([self.sample(1, None, availability="unavailable")])
        point = self.store.query("host.cpu_percent")[0]
        self.assertIsNone(point["average"])
        self.assertIsNone(point["minimum"])
        self.assertIsNone(point["maximum"])
        self.assertEqual(point["availability"], "unavailable")

    def test_rollups_expire_at_one_hour_and_twenty_four_hours(self):
        self.store.record([self.sample(0, 10)])
        self.now += timedelta(hours=1, seconds=1)
        self.store.record([MetricSample("host.cpu_percent", 20, self.now, "%", "procfs:stat")])
        self.assertEqual(len(self.store.query("host.cpu_percent", resolution=10)), 1)
        self.assertEqual(len(self.store.query("host.cpu_percent", resolution=60)), 2)
        self.now += timedelta(hours=23)
        self.store.record([MetricSample("host.cpu_percent", 30, self.now, "%", "procfs:stat")])
        self.assertEqual(len(self.store.query("host.cpu_percent", resolution=60)), 2)

    def test_rollups_survive_store_recreation_after_service_restart(self):
        path = Path(self.temp.name) / "restart.sqlite"
        writer = TelemetryHistoryStore(path, clock=lambda: self.now)
        writer.record([self.sample(0, 42)])
        writer.close()
        restarted = TelemetryHistoryStore(path, clock=lambda: self.now)
        history = restarted.query("host.cpu_percent", resolution=10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["average"], 42)
        restarted.close()

    def test_retention_setting_prunes_old_rollups_and_survives_restart(self):
        self.store.record([self.sample(10 * 60 * 60, 20)])
        self.assertEqual(len(self.store.query("host.cpu_percent", resolution=60)), 1)
        self.assertEqual(self.store.set_retention_hours(6), 6)
        self.assertEqual(self.store.query("host.cpu_percent", resolution=60), [])
        self.store.close()

        restarted = TelemetryHistoryStore(Path(self.temp.name) / "history.sqlite", clock=lambda: self.now)
        self.assertEqual(restarted.retention_hours, 6)
        with self.assertRaises(ValueError):
            restarted.set_retention_hours(7)
        with self.assertRaises(ValueError):
            restarted.set_retention_hours(6.0)
        restarted.close()

    def test_row_quota_prunes_oldest_rollups_and_caps_series(self):
        bounded = TelemetryHistoryStore(Path(self.temp.name) / "bounded.sqlite", clock=lambda: self.now, max_rows=2)
        bounded.record([
            MetricSample("host.a", 1, self.now - timedelta(seconds=120), "u", "test"),
            MetricSample("host.b", 2, self.now - timedelta(seconds=60), "u", "test"),
            MetricSample("host.c", 3, self.now, "u", "test"),
        ])
        with bounded._connect() as conn:
            self.assertLessEqual(conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0], 2)
            self.assertLessEqual(conn.execute("SELECT COUNT(DISTINCT series) FROM rollups").fetchone()[0], 2)

    def test_disk_byte_quota_prunes_oldest_rows(self):
        path = Path(self.temp.name) / "disk-bounded.sqlite"
        bounded = TelemetryHistoryStore(path, clock=lambda: self.now, max_bytes=32 * 1024)
        bounded.record([
            MetricSample(f"host.metric_{index}", index, self.now, "u", "test")
            for index in range(100)
        ])
        wal_path = Path(str(path) + "-wal")
        occupied = path.stat().st_size + (wal_path.stat().st_size if wal_path.exists() else 0)
        self.assertLessEqual(occupied, 32 * 1024)
        with bounded._connect() as conn:
            self.assertLess(conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0], 200)

    def test_write_failure_is_fail_open_and_live_collection_continues(self):
        path = Path(self.temp.name) / "broken.sqlite"
        store = TelemetryHistoryStore(path, clock=lambda: self.now)
        path.unlink()
        path.mkdir()
        store.record([self.sample(0, 50)])
        collector = TelemetryCollector(sample_sink=lambda _samples: store.record([self.sample(0, 50)]))
        samples = collector.sample_host(force=True)
        self.assertTrue(samples)

    def test_live_mode_store_does_not_create_database(self):
        disabled_path = Path(self.temp.name) / "no-history.sqlite"
        disabled = TelemetryHistoryStore(None, clock=lambda: self.now)
        disabled.record([self.sample(0, 10)])
        self.assertFalse(disabled_path.exists())
        self.assertFalse(disabled.enabled)


class HostAlertStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
        self.path = Path(self.temp.name) / "alerts.sqlite"
        self.store = HostAlertStore(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def sample(self, name, value, *, labels=None, availability="available"):
        return MetricSample(name, value, self.now, "%", "private-source-path",
                            availability=availability, labels=labels or {})

    def test_alerts_deduplicate_acknowledge_clear_and_survive_restart(self):
        self.store.observe([self.sample("host.cpu_percent", 94, labels={"secret": "/home/user"})])
        first = self.store.list()[0]
        self.assertEqual(first["code"], "cpu.high")
        self.assertNotIn("secret", str(first))
        self.assertNotIn("private-source-path", str(first))
        self.assertTrue(self.store.acknowledge(first["id"]))
        self.assertFalse(self.store.acknowledge(first["id"]))
        self.now += timedelta(seconds=20)
        self.store.observe([self.sample("host.cpu_percent", 96)])
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.list()[0]["id"], first["id"])
        self.assertEqual(self.store.list()[0]["value"], 96)
        self.assertIsNotNone(self.store.list()[0]["acknowledged_at"])
        self.store.observe([self.sample("host.cpu_percent", None, availability="unavailable")])
        self.assertEqual(len(self.store.list()), 1, "missing telemetry must not falsely clear an alert")
        self.store.close()

        restarted = HostAlertStore(self.path, clock=lambda: self.now)
        self.assertEqual(restarted.list()[0]["id"], first["id"])
        restarted.observe([self.sample("host.cpu_percent", 20)])
        self.assertEqual(restarted.list(), [])
        historical = restarted.list(include_cleared=True)
        self.assertIsNotNone(historical[0]["cleared_at"])

    def test_alerts_apply_allowlist_nonfinite_rejection_and_bounded_retention(self):
        bounded = HostAlertStore(Path(self.temp.name) / "bounded.sqlite", clock=lambda: self.now, max_events=1)
        bounded.observe([
            self.sample("host.cpu_percent", 91),
            self.sample("host.temperature_celsius", 95, labels={"sensor": "/sys/private/hwmon1"}),
            self.sample("host.gpu.busy_percent", 99),
            self.sample("host.cpu_percent", float("nan")),
        ])
        self.assertLessEqual(len(bounded.list(include_cleared=True)), 1)
        self.assertEqual(len(bounded.list()), 1)
        self.now += timedelta(days=31)
        bounded.observe([])
        self.assertEqual(bounded.list(include_cleared=True), [])

    def test_low_disk_space_alert_does_not_persist_mount_path(self):
        labels = {"mountpoint": "/home/private", "device": "/dev/mapper/secret"}
        self.store.observe([
            self.sample("host.filesystem_total_bytes", 1000, labels=labels),
            self.sample("host.filesystem_available_bytes", 50, labels=labels),
        ])
        alert = self.store.list()[0]
        self.assertEqual(alert["code"], "filesystem.low")
        self.assertEqual(alert["value"], 5)
        self.assertNotIn("/home/private", str(alert))
        self.assertNotIn("/dev/mapper/secret", str(alert))

    def test_cached_host_snapshots_are_not_persisted_again(self):
        calls = []
        collector = TelemetryCollector(sample_sink=lambda batch: calls.append(batch))
        collector.sample_host(force=True)
        collector.sample_host()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
