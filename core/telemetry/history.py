"""Bounded SQLite rollups for host and VM telemetry.

Only numeric, finite measurements contribute to aggregates. Missing measurements
are counted separately, so a gap is never represented as an invented zero.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .collector import MetricSample


class TelemetryHistoryStore:
    """Store 10-second/1-hour and configurable 60-second numeric rollups.

    ``path=None`` disables persistence, which is used for ephemeral Live media.
    The reader returns UTC bucket timestamps and nullable ``average`` values.
    """

    ROLLUPS = {10: timedelta(hours=1), 60: timedelta(hours=24)}
    RETENTION_OPTIONS_HOURS = (1, 6, 12, 24)
    MAX_SERIES = 256
    MAX_ROWS = 200_000
    MAX_LABELS = 8
    MAX_TEXT = 96

    def __init__(
        self,
        path: str | os.PathLike[str] | None,
        *,
        clock: Callable[[], datetime] | None = None,
        max_bytes: int = 128 * 1024 * 1024,
        max_rows: int = MAX_ROWS,
    ) -> None:
        if max_bytes < 1024 or max_rows < 1:
            raise ValueError("history quotas must be positive")
        self.path = Path(path) if path is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_bytes = max_bytes
        self.max_rows = max_rows
        self._lock = threading.RLock()
        self._disabled_reason: str | None = "disabled" if self.path is None else None
        self._retention_hours = 24
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._init_db()
            except (OSError, sqlite3.Error) as exc:
                self._disabled_reason = str(exc)

    @property
    def enabled(self) -> bool:
        return self.path is not None and self._disabled_reason is None

    @property
    def retention_hours(self) -> int:
        return self._retention_hours

    def set_retention_hours(self, hours: int) -> int:
        """Persist a bounded retention choice and immediately prune older rollups."""
        if isinstance(hours, bool) or not isinstance(hours, int) or hours not in self.RETENTION_OPTIONS_HOURS:
            raise ValueError("retention must be one of 1, 6, 12, or 24 hours")
        if not self.enabled:
            raise RuntimeError("persistent telemetry history is disabled")
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("history clock must return an aware datetime")
        cutoff = int(now.astimezone(timezone.utc).timestamp()) - hours * 60 * 60
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO telemetry_settings(key,value) VALUES ('retention_hours',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(hours),),
                )
                conn.execute("DELETE FROM rollups WHERE resolution=60 AND bucket<?", (cutoff,))
                conn.commit()
                self._enforce_limits(conn)
                conn.commit()
                self._retention_hours = hours
            return hours
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("could not persist telemetry retention") from exc

    def _rollup_retentions(self) -> dict[int, timedelta]:
        return {10: timedelta(hours=1), 60: timedelta(hours=self._retention_hours)}

    def _connect(self) -> sqlite3.Connection:
        assert self.path is not None
        conn = sqlite3.connect(self.path, timeout=0.1)
        conn.execute("PRAGMA busy_timeout=100")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS rollups (
                resolution INTEGER NOT NULL,
                bucket INTEGER NOT NULL,
                series TEXT NOT NULL,
                name TEXT NOT NULL,
                labels TEXT NOT NULL,
                unit TEXT NOT NULL,
                source TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                unavailable_count INTEGER NOT NULL,
                value_sum REAL,
                value_min REAL,
                value_max REAL,
                PRIMARY KEY (resolution, bucket, series)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS rollups_lookup ON rollups(name, resolution, bucket)")
            conn.execute("CREATE TABLE IF NOT EXISTS telemetry_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO telemetry_settings(key, value) VALUES ('retention_hours', '24')")
            stored = conn.execute("SELECT value FROM telemetry_settings WHERE key='retention_hours'").fetchone()
            try:
                configured = int(stored[0]) if stored else 24
            except (TypeError, ValueError):
                configured = 24
            self._retention_hours = configured if configured in self.RETENTION_OPTIONS_HOURS else 24
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _numeric(sample: MetricSample) -> float | None:
        if sample.availability != "available" or isinstance(sample.value, bool):
            return None
        if not isinstance(sample.value, (int, float)):
            return None
        value = float(sample.value)
        return value if math.isfinite(value) else None

    @classmethod
    def _safe_sample(cls, sample: MetricSample) -> tuple[str, str, str, str] | None:
        name, unit, source = sample.name.strip(), sample.unit.strip(), sample.source.strip()
        if not name or len(name) > cls.MAX_TEXT or len(unit) > cls.MAX_TEXT or len(source) > cls.MAX_TEXT:
            return None
        if len(sample.labels) > cls.MAX_LABELS:
            return None
        labels: dict[str, str] = {}
        for key, value in sorted(sample.labels.items()):
            if not isinstance(key, str) or not isinstance(value, str):
                return None
            if not key or len(key) > cls.MAX_TEXT or len(value) > cls.MAX_TEXT:
                return None
            labels[key] = value
        labels_json = json.dumps(labels, sort_keys=True, separators=(",", ":"))
        return name, unit, source, labels_json

    def record(self, samples: Iterable[MetricSample]) -> None:
        """Persist one freshly collected batch; failures are intentionally fail-open."""
        if not self.enabled:
            return
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("history clock must return an aware datetime")
        now_epoch = int(now.astimezone(timezone.utc).timestamp())
        prepared = []
        for sample in samples:
            safe = self._safe_sample(sample)
            if safe is None:
                continue
            name, unit, source, labels_json = safe
            series = json.dumps([name, labels_json], separators=(",", ":"))
            value = self._numeric(sample)
            stamp = int(sample.timestamp.astimezone(timezone.utc).timestamp())
            prepared.append((name, unit, source, labels_json, series, value, stamp))
        if not prepared:
            return
        try:
            with self._lock, self._connect() as conn:
                known = {row[0] for row in conn.execute("SELECT DISTINCT series FROM rollups")}
                candidates = {item[4] for item in prepared}
                if len(known | candidates) > self.MAX_SERIES:
                    allowed = set(sorted(known))
                    allowed.update(sorted(candidates)[:max(0, self.MAX_SERIES - len(known))])
                else:
                    allowed = known | candidates
                for resolution, retention in self._rollup_retentions().items():
                    cutoff = now_epoch - int(retention.total_seconds())
                    batch = [item for item in prepared if item[4] in allowed and item[6] >= cutoff]
                    for name, unit, source, labels, series, value, stamp in batch:
                        bucket = stamp - stamp % resolution
                        count = 1 if value is not None else 0
                        total = value if value is not None else None
                        conn.execute("""INSERT INTO rollups
                            (resolution,bucket,series,name,labels,unit,source,sample_count,
                             unavailable_count,value_sum,value_min,value_max)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(resolution,bucket,series) DO UPDATE SET
                              source=excluded.source,
                              sample_count=rollups.sample_count+excluded.sample_count,
                              unavailable_count=rollups.unavailable_count+excluded.unavailable_count,
                              value_sum=CASE WHEN excluded.value_sum IS NULL THEN rollups.value_sum
                                WHEN rollups.value_sum IS NULL THEN excluded.value_sum ELSE rollups.value_sum+excluded.value_sum END,
                              value_min=CASE WHEN excluded.value_min IS NULL THEN rollups.value_min
                                WHEN rollups.value_min IS NULL THEN excluded.value_min ELSE MIN(rollups.value_min,excluded.value_min) END,
                              value_max=CASE WHEN excluded.value_max IS NULL THEN rollups.value_max
                                WHEN rollups.value_max IS NULL THEN excluded.value_max ELSE MAX(rollups.value_max,excluded.value_max) END""",
                            (resolution, bucket, series, name, labels, unit, source, count,
                             0 if value is not None else 1, total, value, value))
                    conn.execute("DELETE FROM rollups WHERE resolution=? AND bucket<?", (resolution, cutoff))
                conn.commit()
                self._enforce_limits(conn)
                conn.commit()
        except (OSError, sqlite3.Error):
            # Persistence is auxiliary; callers still receive live samples.
            return

    def _enforce_limits(self, conn: sqlite3.Connection) -> None:
        current_rows = conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0]
        excess = max(0, current_rows - self.max_rows)
        if excess:
            victims = conn.execute(
                "SELECT rowid FROM rollups ORDER BY bucket ASC, resolution DESC LIMIT ?", (excess,)
            ).fetchall()
            conn.executemany("DELETE FROM rollups WHERE rowid=?", victims)
            conn.commit()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        if not self.path:
            return
        db_size = self.path.stat().st_size if self.path.exists() else 0
        wal_path = Path(str(self.path) + "-wal")
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
        # Leave room for SQLite's WAL header and metadata emitted by readers.
        target_bytes = max(1024, self.max_bytes - min(4096, self.max_bytes // 8))
        if db_size + wal_size <= target_bytes:
            return
        while db_size + wal_size > target_bytes:
            row_count = conn.execute("SELECT COUNT(*) FROM rollups").fetchone()[0]
            if not row_count:
                break
            remove_count = min(1000, max(1, row_count // 8))
            conn.execute("DELETE FROM rollups WHERE rowid IN (SELECT rowid FROM rollups ORDER BY bucket ASC LIMIT ?)",
                         (remove_count,))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db_size = self.path.stat().st_size if self.path.exists() else 0
            wal_size = wal_path.stat().st_size if wal_path.exists() else 0

    def query(
        self,
        name: str,
        *,
        labels: dict[str, str] | None = None,
        resolution: int = 10,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Read bucket aggregates in ascending UTC order (10s or 60s)."""
        if resolution not in self.ROLLUPS or not 1 <= limit <= 5000:
            raise ValueError("resolution must be 10 or 60 seconds and limit 1..5000")
        if not self.enabled:
            return []
        if labels is not None:
            if not isinstance(labels, dict) or len(labels) > self.MAX_LABELS or any(
                not isinstance(k, str) or not isinstance(v, str) or not k or
                len(k) > self.MAX_TEXT or len(v) > self.MAX_TEXT for k, v in labels.items()
            ):
                raise ValueError("invalid telemetry labels")
        labels_json = json.dumps(labels or {}, sort_keys=True, separators=(",", ":"))
        clauses = ["name=?", "resolution=?", "labels=?"]
        args: list[Any] = [name, resolution, labels_json]
        for field, value in (("bucket>=", since), ("bucket<=", until)):
            if value is not None:
                if value.tzinfo is None:
                    raise ValueError("query timestamps must be timezone-aware")
                clauses.append(field + "?")
                args.append(int(value.astimezone(timezone.utc).timestamp()))
        args.append(limit)
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("SELECT bucket,unit,source,sample_count,unavailable_count,value_sum,value_min,value_max "
                    "FROM rollups WHERE " + " AND ".join(clauses) + " ORDER BY bucket ASC LIMIT ?", args).fetchall()
        except (OSError, sqlite3.Error):
            return []
        result = []
        for bucket, unit, source, count, unavailable, total, minimum, maximum in rows:
            result.append({
                "timestamp": datetime.fromtimestamp(bucket, timezone.utc).isoformat(),
                "resolution_seconds": resolution,
                "average": total / count if count and total is not None else None,
                "minimum": minimum,
                "maximum": maximum,
                "sample_count": count,
                "unavailable_count": unavailable,
                "availability": "available" if count and not unavailable else "partial" if count else "unavailable",
                "unit": unit,
                "source": source,
                "labels": labels or {},
            })
        return result

    def close(self) -> None:
        if self.path is None:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (OSError, sqlite3.Error):
            pass
