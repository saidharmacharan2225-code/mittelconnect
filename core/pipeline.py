"""Master orchestration: extract -> transform -> deliver, with a local outbox.

Delivery guarantee: at-least-once. A job's watermark only advances once every
row of a chunk is accounted for: accepted by SAP, parked in the encrypted
SQLite outbox, or recorded as a dead letter. A crash at any point therefore
re-reads, never skips, data. The outbox is replayed (oldest first, bounded,
with per-entry exponential backoff) at the start of every cycle.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from core.crypto import Pseudonymizer, SecretBox
from core.db_adapters import AdapterError, BaseAdapter, create_adapter
from core.resilience import CircuitOpenError, backoff_delay
from core.sap_client import (
    BatchResult,
    SAPAuthError,
    SAPClient,
    SAPError,
    SAPRequestError,
    SAPUnavailableError,
)
from core.settings import Settings

logger = logging.getLogger(__name__)

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
GERMAN_DATE_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y%m%d")
TRUE_VALUES = {"1", "true", "t", "y", "yes", "j", "ja", "x"}
FALSE_VALUES = {"0", "false", "f", "n", "no", "nein", ""}


# =============================================================================
# Local persistent store
# =============================================================================
class LocalStore:
    """SQLite store for watermarks, the outbox and dead letters.

    WAL journaling with synchronous=FULL survives power loss on the shop floor
    without corrupting the database.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS watermarks (
        job         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS outbox (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job             TEXT NOT NULL,
        service_path    TEXT NOT NULL,
        entity_set      TEXT NOT NULL,
        record_count    INTEGER NOT NULL,
        payload         BLOB NOT NULL,
        encrypted       INTEGER NOT NULL,
        attempts        INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        next_attempt_at REAL NOT NULL,
        last_error      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox (next_attempt_at, id);
    CREATE TABLE IF NOT EXISTS dead_letters (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job         TEXT NOT NULL,
        reason      TEXT NOT NULL,
        status      INTEGER,
        payload     BLOB NOT NULL,
        encrypted   INTEGER NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_dead_letters_created ON dead_letters (created_at);
    """

    def __init__(self, path: str, secret_box: Optional[SecretBox], encrypt: bool = True):
        if encrypt and secret_box is None:
            raise ValueError("Outbox encryption requested but no master key is loaded")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._box = secret_box if encrypt else None
        self._conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(self.SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError as exc:
            logger.debug("Could not restrict cache permissions: %s", exc)

    def close(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self._conn.close()

    @staticmethod
    def _now_iso() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def _seal(self, records: list[dict]) -> tuple[bytes, int]:
        raw = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self._box is not None:
            return self._box.encrypt_bytes(raw), 1
        return raw, 0

    def _open(self, payload: bytes, encrypted: int) -> list[dict]:
        if encrypted:
            if self._box is None:
                raise ValueError("Encrypted cache entry found but no master key is loaded")
            payload = self._box.decrypt_bytes(payload)
        return json.loads(payload.decode("utf-8"))

    # -------------------------------------------------------------- watermarks
    def get_watermark(self, job: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM watermarks WHERE job = ?", (job,)).fetchone()
        return row["value"] if row else None

    def set_watermark(self, job: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO watermarks (job, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(job) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (job, value, self._now_iso()),
        )

    # ------------------------------------------------------------------ outbox
    def enqueue(self, job: str, service_path: str, entity_set: str, records: list[dict], error: str) -> int:
        payload, encrypted = self._seal(records)
        cursor = self._conn.execute(
            "INSERT INTO outbox (job, service_path, entity_set, record_count, payload, encrypted, "
            "attempts, created_at, next_attempt_at, last_error) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (job, service_path, entity_set, len(records), payload, encrypted,
             self._now_iso(), time.time(), error[:1000]),
        )
        return int(cursor.lastrowid)

    def due_outbox(self, limit: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM outbox WHERE next_attempt_at <= ? ORDER BY id LIMIT ?",
            (time.time(), limit),
        ).fetchall()

    def outbox_records(self, entry: sqlite3.Row) -> list[dict]:
        return self._open(entry["payload"], entry["encrypted"])

    def reschedule(self, entry_id: int, attempts: int, delay: float, error: str,
                   records: Optional[list[dict]] = None) -> None:
        if records is None:
            self._conn.execute(
                "UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
                (attempts, time.time() + delay, error[:1000], entry_id),
            )
            return
        payload, encrypted = self._seal(records)
        self._conn.execute(
            "UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ?, payload = ?, "
            "encrypted = ?, record_count = ? WHERE id = ?",
            (attempts, time.time() + delay, error[:1000], payload, encrypted, len(records), entry_id),
        )

    def delete_outbox(self, entry_id: int) -> None:
        self._conn.execute("DELETE FROM outbox WHERE id = ?", (entry_id,))

    def outbox_record_count(self) -> int:
        row = self._conn.execute("SELECT COALESCE(SUM(record_count), 0) AS n FROM outbox").fetchone()
        return int(row["n"])

    def outbox_entry_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    # ------------------------------------------------------------ dead letters
    def dead_letter(self, job: str, records: list[dict], reason: str, status: Optional[int] = None) -> None:
        if not records:
            return
        payload, encrypted = self._seal(records)
        self._conn.execute(
            "INSERT INTO dead_letters (job, reason, status, payload, encrypted, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job, reason[:2000], status, payload, encrypted, self._now_iso()),
        )

    def dead_letter_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])

    def purge_dead_letters(self, retention_days: int) -> int:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)).isoformat(timespec="seconds")
        cursor = self._conn.execute("DELETE FROM dead_letters WHERE created_at < ?", (cutoff,))
        return cursor.rowcount


# =============================================================================
# Transformation
# =============================================================================
class TransformError(Exception):
    def __init__(self, field_name: str, reason: str):
        super().__init__(f"{field_name}: {reason}")
        self.field_name = field_name
        self.reason = reason


class Transformer:
    """Applies a job's field mapping to one source row."""

    def __init__(self, job_name: str, mapping: dict, pseudonymizer: Optional[Pseudonymizer], timezone: str):
        self.job_name = job_name
        self.mapping = mapping
        self.pseudonymizer = pseudonymizer
        try:
            self.zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown source_timezone '{timezone}' for job {job_name}") from exc
        if pseudonymizer is None and any(rule.get("pseudonymize") for rule in mapping.values()):
            raise ValueError(
                f"Job {job_name} pseudonymises fields but security.pseudonymization_key is not set"
            )

    def redact(self, row: dict) -> dict:
        """Source row safe to keep in dead letters: every column that feeds a
        pseudonymised field is replaced by its pseudonym, so personal data never
        rests in clear text locally, not even for rows that failed to transform."""
        personal = {
            str(rule["source"]).lower()
            for rule in self.mapping.values()
            if rule.get("pseudonymize") and "source" in rule
        }
        if not personal:
            return dict(row)
        assert self.pseudonymizer is not None
        return {
            key: self.pseudonymizer.pseudonymize(value) if str(key).lower() in personal else value
            for key, value in row.items()
        }

    def transform(self, row: dict) -> dict:
        lowered = {str(key).lower(): value for key, value in row.items()}
        record: dict[str, Any] = {}
        for target, rule in self.mapping.items():
            ftype = rule.get("type", "string")
            if ftype == "constant":
                record[target] = rule["value"]
                continue
            source = str(rule["source"]).lower()
            if source not in lowered:
                raise TransformError(target, f"source column '{rule['source']}' missing from result set")
            value = lowered[source]
            if isinstance(value, str):
                value = value.strip()
                if value == "":
                    value = None
            if value is None and "default" in rule:
                value = rule["default"]
            value_map = rule.get("value_map")
            if value_map and value is not None:
                key = str(value).strip()
                value = value_map.get(key, value_map.get(key.upper(), value))
            if value is None:
                if rule.get("required"):
                    raise TransformError(target, "required value is empty")
                continue  # omit NULLs so SAP applies its own field defaults
            converted = self._convert(target, value, ftype, rule)
            if rule.get("pseudonymize"):
                assert self.pseudonymizer is not None
                converted = self.pseudonymizer.pseudonymize(converted)
            if rule.get("required") and converted in (None, ""):
                raise TransformError(target, "required value is empty after conversion")
            record[target] = converted
        return record

    def _convert(self, target: str, value: Any, ftype: str, rule: dict) -> Any:
        try:
            if ftype == "string":
                text = str(value).strip()
                if rule.get("upper"):
                    text = text.upper()
                max_length = rule.get("max_length")
                if max_length and len(text) > int(max_length):
                    logger.debug("Truncating %s.%s to %s characters", self.job_name, target, max_length)
                    text = text[: int(max_length)]
                return text
            if ftype == "int":
                number = Decimal(str(value).replace(",", "."))
                if number != number.to_integral_value():
                    raise TransformError(target, f"'{value}' is not an integer")
                return int(number)
            if ftype == "decimal":
                number = Decimal(str(value).strip().replace(",", ".")) if not isinstance(value, Decimal) else value
                scale = int(rule.get("scale", 3))
                quantized = number.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
                return format(quantized, "f")  # OData V2 transports Edm.Decimal as string
            if ftype == "bool":
                if isinstance(value, bool):
                    return value
                text = str(value).strip().lower()
                if text in TRUE_VALUES:
                    return True
                if text in FALSE_VALUES:
                    return False
                raise TransformError(target, f"'{value}' is not a boolean")
            if ftype == "date":
                moment = self._to_datetime(value)
                midnight = dt.datetime(moment.year, moment.month, moment.day, tzinfo=dt.timezone.utc)
                return self._odata_v2_date(midnight)
            if ftype == "datetime":
                return self._odata_v2_date(self._to_datetime(value))
        except TransformError:
            raise
        except (InvalidOperation, ValueError, TypeError, OverflowError) as exc:
            raise TransformError(target, f"cannot convert '{value}' to {ftype}: {exc}") from exc
        raise TransformError(target, f"unsupported type {ftype}")

    def _to_datetime(self, value: Any) -> dt.datetime:
        if isinstance(value, dt.datetime):
            moment = value
        elif isinstance(value, dt.date):
            moment = dt.datetime(value.year, value.month, value.day)
        else:
            text = str(value).strip()
            moment = None
            try:
                moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                for fmt in GERMAN_DATE_FORMATS:
                    try:
                        moment = dt.datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
            if moment is None:
                raise ValueError(f"unrecognised date format '{text}'")
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.zone)
        return moment.astimezone(dt.timezone.utc)

    @staticmethod
    def _odata_v2_date(moment: dt.datetime) -> str:
        millis = int((moment - EPOCH).total_seconds() * 1000)
        return f"/Date({millis})/"


# =============================================================================
# Reporting
# =============================================================================
@dataclass
class JobReport:
    job: str
    extracted: int = 0
    delivered: int = 0
    rejected: int = 0
    cached: int = 0
    transform_errors: int = 0
    skipped: str = ""
    error: str = ""
    watermark: Optional[str] = None


@dataclass
class CycleReport:
    started_at: float = field(default_factory=time.monotonic)
    replayed_ok: int = 0
    replay_rejected: int = 0
    replay_pending: int = 0
    jobs: list[JobReport] = field(default_factory=list)
    outbox_records: int = 0
    dead_letters: int = 0

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started_at

    def as_log_fields(self) -> dict:
        return {
            "duration_s": round(self.duration, 2),
            "replayed_ok": self.replayed_ok,
            "replay_rejected": self.replay_rejected,
            "replay_pending": self.replay_pending,
            "outbox_records": self.outbox_records,
            "dead_letters": self.dead_letters,
            "jobs": [job.__dict__ for job in self.jobs],
        }


def _watermark_text(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day).isoformat()
    return str(value)


def _batched(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


# =============================================================================
# Pipeline
# =============================================================================
SAP_DOWN_ERRORS = (SAPUnavailableError, CircuitOpenError, httpx.TransportError)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        sap_client: Optional[SAPClient] = None,
        adapters: Optional[dict[str, BaseAdapter]] = None,
        store: Optional[LocalStore] = None,
    ):
        self.settings = settings
        cache = settings.cache
        security = settings.security
        self.store = store or LocalStore(
            cache.get("sqlite_path", "./data/mittelconnect_cache.db"),
            settings.secret_box,
            encrypt=bool(security.get("encrypt_outbox", True)),
        )
        self.sap = sap_client or SAPClient(settings.sap, list(security.get("allowed_host_suffixes", [])))
        self.adapters: dict[str, BaseAdapter] = adapters or {
            name: create_adapter(name, cfg) for name, cfg in settings.databases.items()
        }
        pseudo_key = security.get("pseudonymization_key") or ""
        self.pseudonymizer = Pseudonymizer(pseudo_key) if pseudo_key else None
        self.transformers = {
            job["name"]: Transformer(
                job["name"], job["mapping"], self.pseudonymizer, job.get("source_timezone", "Europe/Berlin")
            )
            for job in settings.jobs
        }
        self.max_replay_attempts = int(cache.get("max_replay_attempts", 50))
        self.replay_limit = int(cache.get("replay_batch_limit", 200))
        self.replay_base = float(cache.get("replay_base_delay_seconds", 30))
        self.replay_max = float(cache.get("replay_max_delay_seconds", 3600))
        self.retention_days = int(cache.get("dead_letter_retention_days", 30))
        self.extract_while_down = bool(cache.get("extract_while_sap_down", True))
        self.max_outbox_records = int(cache.get("max_outbox_records", 500000))

    async def close(self) -> None:
        for adapter in self.adapters.values():
            await asyncio.to_thread(adapter.close)
        await self.sap.close()
        self.store.close()

    # ------------------------------------------------------------------ cycle
    async def run_cycle(self, stop_event: Optional[asyncio.Event] = None) -> CycleReport:
        stop_event = stop_event or asyncio.Event()
        report = CycleReport()
        purged = self.store.purge_dead_letters(self.retention_days)
        if purged:
            logger.info("Purged %d dead letters older than %d days", purged, self.retention_days)

        await self.replay_outbox(report, stop_event)

        for job in self.settings.jobs:
            if stop_event.is_set():
                logger.info("Shutdown requested; skipping remaining jobs")
                break
            job_report = JobReport(job=job["name"])
            report.jobs.append(job_report)
            try:
                await self.run_job(job, job_report, stop_event)
            except AdapterError as exc:
                job_report.error = str(exc)
                logger.error("Job %s: source error: %s", job["name"], exc)
            except Exception as exc:
                job_report.error = f"{type(exc).__name__}: {exc}"
                logger.exception("Job %s failed unexpectedly", job["name"])

        report.outbox_records = self.store.outbox_record_count()
        report.dead_letters = self.store.dead_letter_count()
        logger.info("Cycle finished", extra={"cycle": report.as_log_fields()})
        return report

    # ----------------------------------------------------------------- replay
    async def replay_outbox(self, report: CycleReport, stop_event: asyncio.Event) -> None:
        entries = self.store.due_outbox(self.replay_limit)
        if not entries:
            return
        if not self.sap.breaker.is_call_permitted():
            report.replay_pending = self.store.outbox_record_count()
            logger.info("SAP circuit open; %d records wait in the outbox", report.replay_pending)
            return
        logger.info("Replaying %d outbox entries", len(entries))
        for entry in entries:
            if stop_event.is_set() or not self.sap.breaker.is_call_permitted():
                break
            entry_id = entry["id"]
            attempts = int(entry["attempts"]) + 1
            try:
                records = self.store.outbox_records(entry)
            except Exception as exc:
                logger.error("Outbox entry %d unreadable (%s); moving to dead letters", entry_id, exc)
                self.store.dead_letter(entry["job"], [{"outbox_id": entry_id}], f"unreadable: {exc}")
                self.store.delete_outbox(entry_id)
                continue
            try:
                result = await self.sap.post_batch(entry["service_path"], entry["entity_set"], records)
            except SAP_DOWN_ERRORS as exc:
                # An unreachable SAP says nothing about the records themselves, so
                # outage time never counts towards max_replay_attempts: buffered
                # records wait for as long as the outage lasts and are never
                # moved to dead letters just because SAP was down.
                delay = backoff_delay(attempts, self.replay_base, self.replay_max)
                self.store.reschedule(entry_id, attempts - 1, delay, str(exc))
                logger.warning("SAP still unavailable; outbox replay paused: %s", exc)
                break
            except (SAPAuthError, SAPRequestError, SAPError) as exc:
                self._reschedule_or_bury(entry, attempts, str(exc), records)
                continue

            rejected = [records[r.index] for r in result.rejected]
            if rejected:
                reasons = "; ".join(sorted({r.message for r in result.rejected}))[:2000]
                self.store.dead_letter(entry["job"], rejected, f"SAP rejected on replay: {reasons}",
                                       result.rejected[0].status)
                report.replay_rejected += len(rejected)
            report.replayed_ok += len(result.succeeded)
            pending = [records[r.index] for r in result.retryable]
            if pending:
                self._reschedule_or_bury(entry, attempts, "partial retryable failure", pending)
            else:
                self.store.delete_outbox(entry_id)
        report.replay_pending = self.store.outbox_record_count()

    def _reschedule_or_bury(self, entry: sqlite3.Row, attempts: int, error: str, records: list[dict]) -> None:
        if attempts >= self.max_replay_attempts:
            logger.error(
                "Outbox entry %d exceeded %d attempts; moving %d records to dead letters",
                entry["id"], self.max_replay_attempts, len(records),
            )
            self.store.dead_letter(entry["job"], records, f"max replay attempts exceeded: {error}")
            self.store.delete_outbox(entry["id"])
            return
        delay = backoff_delay(attempts, self.replay_base, self.replay_max)
        self.store.reschedule(entry["id"], attempts, delay, error, records)

    # -------------------------------------------------------------------- job
    async def run_job(self, job: dict, report: JobReport, stop_event: asyncio.Event) -> None:
        name = job["name"]
        adapter = self.adapters[job["source"]]
        transformer = self.transformers[name]
        watermark = self.store.get_watermark(name) or str(job.get("initial_watermark", "1900-01-01T00:00:00"))
        report.watermark = watermark

        if not self.extract_while_down and not self.sap.breaker.is_call_permitted():
            report.skipped = "SAP circuit open; source data stays in place"
            logger.info("Job %s skipped: %s", name, report.skipped)
            return

        chunk_size = int(job.get("chunk_size", 1000))
        wm_column = str(job["watermark_column"]).lower()
        generator = adapter.fetch_chunks(job["query"], {"watermark": watermark}, chunk_size)
        exhausted = False
        last_value: Any = None
        try:
            while not stop_event.is_set():
                if self.store.outbox_record_count() >= self.max_outbox_records:
                    report.skipped = f"outbox full ({self.max_outbox_records} records); backpressure"
                    logger.warning("Job %s paused: %s", name, report.skipped)
                    break
                chunk = await asyncio.to_thread(next, generator, None)
                if chunk is None:
                    exhausted = True
                    break
                report.extracted += len(chunk)

                records: list[dict] = []
                for row in chunk:
                    try:
                        records.append(transformer.transform(row))
                    except TransformError as exc:
                        report.transform_errors += 1
                        self.store.dead_letter(name, [_jsonable(transformer.redact(row))], f"transform: {exc}")
                await self._deliver(job, records, report)

                values = [{str(k).lower(): v for k, v in row.items()}.get(wm_column) for row in chunk]
                if any(value is None for value in values):
                    raise AdapterError(f"Watermark column '{job['watermark_column']}' missing or NULL")
                last_value = values[-1]
                # Rows sharing the last timestamp may continue in the next chunk.
                # Commit only the highest value strictly below it, so stopping
                # here re-reads (never skips) those ties on the next cycle.
                safe_value = next((v for v in reversed(values) if v != last_value), None)
                if safe_value is not None:
                    self._commit_watermark(name, safe_value, report)
            if exhausted and last_value is not None:
                self._commit_watermark(name, last_value, report)
        finally:
            await asyncio.to_thread(generator.close)
        logger.info(
            "Job %s: extracted=%d delivered=%d cached=%d rejected=%d transform_errors=%d watermark=%s",
            name, report.extracted, report.delivered, report.cached,
            report.rejected, report.transform_errors, report.watermark,
        )

    def _commit_watermark(self, job_name: str, value: Any, report: JobReport) -> None:
        text = _watermark_text(value)
        self.store.set_watermark(job_name, text)
        report.watermark = text

    async def _deliver(self, job: dict, records: list[dict], report: JobReport) -> None:
        name = job["name"]
        service_path = job["sap"]["service_path"]
        entity_set = job["sap"]["entity_set"]
        for batch in _batched(records, self.sap.batch_size):
            if not self.sap.breaker.is_call_permitted():
                self.store.enqueue(name, service_path, entity_set, batch, "SAP circuit open")
                report.cached += len(batch)
                continue
            try:
                result: BatchResult = await self.sap.post_batch(service_path, entity_set, batch)
            except SAP_DOWN_ERRORS as exc:
                logger.warning("Job %s: SAP unavailable, caching %d records: %s", name, len(batch), exc)
                self.store.enqueue(name, service_path, entity_set, batch, str(exc))
                report.cached += len(batch)
                continue
            except (SAPAuthError, SAPRequestError, SAPError) as exc:
                logger.error("Job %s: SAP refused batch, caching %d records: %s", name, len(batch), exc)
                self.store.enqueue(name, service_path, entity_set, batch, str(exc))
                report.cached += len(batch)
                continue

            report.delivered += len(result.succeeded)
            if result.rejected:
                rejected = [batch[r.index] for r in result.rejected]
                reasons = "; ".join(sorted({r.message for r in result.rejected}))[:2000]
                self.store.dead_letter(name, rejected, f"SAP rejected: {reasons}", result.rejected[0].status)
                report.rejected += len(rejected)
                logger.warning("Job %s: SAP rejected %d records: %s", name, len(rejected), reasons)
            if result.retryable:
                pending = [batch[r.index] for r in result.retryable]
                self.store.enqueue(name, service_path, entity_set, pending, "retryable record failure")
                report.cached += len(pending)


def _jsonable(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if isinstance(value, (dt.datetime, dt.date)):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, (bytes, bytearray)):
            out[key] = value.hex()
        else:
            out[key] = value
    return out
