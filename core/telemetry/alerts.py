"""Bounded persistent host alerts derived from allowlisted telemetry samples."""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


class HostAlertStore:
    """Persist deduplicated host resource alerts without retaining host labels.

    The store only understands a small fixed set of measured metrics. It keeps
    at most 500 events / 30 days, and never stores metric labels, paths, raw
    telemetry, or arbitrary source strings.
    """

    MAX_EVENTS = 500
    MAX_FILESYSTEMS_PER_SAMPLE = 32
    RETENTION = timedelta(days=30)
    RULES = {
        "host.cpu_percent": (90.0, "cpu.high", "CPU do host acima de 90%", "warning", "%"),
        "host.memory_used_percent": (90.0, "memory.high", "Memória do host acima de 90%", "warning", "%"),
        "host.temperature_celsius": (90.0, "temperature.high", "Temperatura do host acima de 90 °C", "critical", "°C"),
    }

    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        *,
        clock: Callable[[], datetime] | None = None,
        max_events: int = MAX_EVENTS,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.path = Path(path) if path is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_events = min(max_events, self.MAX_EVENTS)
        self._lock = threading.RLock()
        self._enabled = self.path is not None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._initialize()
            except (OSError, sqlite3.Error):
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _connect(self) -> sqlite3.Connection:
        assert self.path is not None
        conn = sqlite3.connect(self.path, timeout=0.5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=500")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS host_alerts (
                id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL,
                code TEXT NOT NULL,
                title TEXT NOT NULL,
                severity TEXT NOT NULL,
                unit TEXT NOT NULL,
                value REAL NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                acknowledged_at TEXT,
                cleared_at TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS host_alerts_recent ON host_alerts(last_seen DESC)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS host_alerts_active_key ON host_alerts(dedupe_key) WHERE cleared_at IS NULL")
            self._prune(conn)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("alert clock must return an aware datetime")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _sample_fields(sample: Any) -> tuple[str, Any, str, dict[str, Any]]:
        if isinstance(sample, dict):
            return (str(sample.get("name", "")), sample.get("value"), str(sample.get("availability", "")),
                    sample.get("labels") if isinstance(sample.get("labels"), dict) else {})
        return (str(getattr(sample, "name", "")), getattr(sample, "value", None),
                str(getattr(sample, "availability", "")),
                getattr(sample, "labels", {}) if isinstance(getattr(sample, "labels", {}), dict) else {})

    def _candidates(self, samples: Iterable[Any]) -> tuple[dict[str, tuple[str, float, str, str, str]], set[str]]:
        current: dict[str, tuple[str, float, str, str, str]] = {}
        evaluated: set[str] = set()
        observed = list(samples)
        memory_total = memory_available = None
        filesystems: dict[str, dict[str, float]] = {}
        for sample in observed:
            name, value, availability, labels = self._sample_fields(sample)
            if availability == "available" and isinstance(value, (int, float)) and not isinstance(value, bool):
                if name == "host.memory_total_bytes":
                    memory_total = float(value)
                elif name == "host.memory_available_bytes":
                    memory_available = float(value)
                elif name in {"host.filesystem_total_bytes", "host.filesystem_available_bytes"} and labels:
                    # Keep mount paths out of storage and response; only a
                    # one-way digest distinguishes multiple filesystem alerts.
                    import json
                    fingerprint = hashlib.sha256(json.dumps(labels, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
                    filesystems.setdefault(fingerprint, {})[name] = float(value)
        if memory_total is not None and memory_total > 0 and memory_available is not None and math.isfinite(memory_total) and math.isfinite(memory_available):
            used_percent = max(0.0, min(100.0, 100.0 * (memory_total - memory_available) / memory_total))
            evaluated.add("memory.high")
            if used_percent >= 90:
                current["memory.high"] = ("memory.high", used_percent, "Memória do host acima de 90%", "warning", "%")
        for fingerprint, values in list(filesystems.items())[:self.MAX_FILESYSTEMS_PER_SAMPLE]:
            total = values.get("host.filesystem_total_bytes")
            available = values.get("host.filesystem_available_bytes")
            key = f"filesystem.low:{fingerprint}"
            if total is None or available is None or total <= 0 or not math.isfinite(total) or not math.isfinite(available):
                continue
            evaluated.add(key)
            free_percent = max(0.0, min(100.0, available * 100.0 / total))
            if free_percent <= 10:
                current[key] = ("filesystem.low", free_percent, "Espaço livre do disco abaixo de 10%", "warning", "%")
        for sample in observed:
            name, value, availability, labels = self._sample_fields(sample)
            rule = self.RULES.get(name)
            if rule is None or availability != "available" or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = float(value)
            if not math.isfinite(number):
                continue
            limit, code, title, severity, unit = rule
            if name == "host.cpu_percent" and not 0 <= number <= 100:
                continue
            if name == "host.temperature_celsius" and not -100 <= number <= 200:
                continue
            if name == "host.temperature_celsius":
                # Sensors are keyed by an opaque digest; sysfs/hwmon labels are
                # not copied into persistent state or returned to clients.
                sensor = str(labels.get("sensor", "default"))[:96]
                dedupe = f"{code}:{hashlib.sha256(sensor.encode()).hexdigest()[:16]}"
            else:
                dedupe = code
            evaluated.add(dedupe)
            if number >= limit:
                current[dedupe] = (code, number, title, severity, unit)
        return current, evaluated

    def observe(self, samples: Iterable[Any]) -> None:
        """Update active alerts and clear any whose monitored condition resolved."""
        if not self.enabled:
            return
        candidates, evaluated = self._candidates(samples)
        now = self._now().isoformat()
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("SELECT dedupe_key FROM host_alerts WHERE cleared_at IS NULL").fetchall()
                active = {row[0] for row in rows}
                for dedupe, (code, value, title, severity, unit) in candidates.items():
                    if dedupe in active:
                        conn.execute("UPDATE host_alerts SET last_seen=?, value=? WHERE dedupe_key=? AND cleared_at IS NULL",
                                     (now, value, dedupe))
                    else:
                        conn.execute("""INSERT INTO host_alerts
                            (id,dedupe_key,code,title,severity,unit,value,first_seen,last_seen)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                            (os.urandom(16).hex(), dedupe, code, title, severity, unit, value, now, now))
                for dedupe in (active - candidates.keys()) & evaluated:
                    conn.execute("UPDATE host_alerts SET cleared_at=? WHERE dedupe_key=? AND cleared_at IS NULL", (now, dedupe))
                self._prune(conn)
        except (OSError, sqlite3.Error):
            return

    def list(self, *, include_cleared: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not self.enabled:
            return []
        try:
            with self._lock, self._connect() as conn:
                clause = "" if include_cleared else "WHERE cleared_at IS NULL"
                rows = conn.execute(f"SELECT id,code,title,severity,unit,value,first_seen,last_seen,acknowledged_at,cleared_at FROM host_alerts {clause} ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        except (OSError, sqlite3.Error):
            return []
        return [dict(row) for row in rows]

    def acknowledge(self, alert_id: str) -> bool:
        if not self.enabled or not isinstance(alert_id, str) or len(alert_id) != 32:
            return False
        try:
            with self._lock, self._connect() as conn:
                cur = conn.execute("UPDATE host_alerts SET acknowledged_at=? WHERE id=? AND cleared_at IS NULL AND acknowledged_at IS NULL",
                                   (self._now().isoformat(), alert_id))
                return cur.rowcount == 1
        except (OSError, sqlite3.Error):
            return False

    def _prune(self, conn: sqlite3.Connection) -> None:
        cutoff = (self._now() - self.RETENTION).isoformat()
        conn.execute("DELETE FROM host_alerts WHERE last_seen<?", (cutoff,))
        conn.execute("DELETE FROM host_alerts WHERE id IN (SELECT id FROM host_alerts ORDER BY last_seen DESC LIMIT -1 OFFSET ?)",
                     (self.max_events,))

    def close(self) -> None:
        if not self.enabled:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (OSError, sqlite3.Error):
            pass
