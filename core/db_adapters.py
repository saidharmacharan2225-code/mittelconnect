"""Read-only, chunked extraction from legacy industrial databases.

Every adapter streams results with ``fetchmany`` so a 20-million-row table
never sits in memory at once: peak memory is bounded by ``chunk_size``.
Adapters reconnect with exponential backoff on transient connection errors
and normalise legacy data (Windows-1252 bytes, padded CHAR columns,
Decimal/LOB types) into plain Python values.
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from core.resilience import backoff_delay, retry_sync

logger = logging.getLogger(__name__)

NAMED_PARAM = re.compile(r"(?<![:\w]):(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?)?$")

Row = dict[str, Any]
Chunk = list[Row]


class AdapterError(Exception):
    """Base error for database adapters."""


class ConnectionFailedError(AdapterError):
    """The database could not be reached after all retries."""


class QueryFailedError(AdapterError):
    """The query was rejected or failed during execution."""


def coerce_bind_value(value: Any) -> Any:
    """Bind ISO-8601 watermark strings as datetimes so drivers compare natively."""
    if isinstance(value, str) and ISO_DATETIME.match(value.strip()):
        try:
            return dt.datetime.fromisoformat(value.strip())
        except ValueError:
            return value
    return value


def to_qmark(query: str, params: dict[str, Any]) -> tuple[str, list[Any]]:
    """Convert ``:name`` binds to ``?`` placeholders (pyodbc) preserving order.

    String literals are left untouched so a literal like ``'12:30'`` inside
    the SQL is not mistaken for a bind parameter.
    """
    ordered: list[Any] = []
    output: list[str] = []
    for index, segment in enumerate(re.split(r"('(?:[^']|'')*')", query)):
        if index % 2 == 1:
            output.append(segment)
            continue

        def replace(match: re.Match) -> str:
            name = match.group("name")
            if name not in params:
                raise QueryFailedError(f"Query references unbound parameter :{name}")
            ordered.append(params[name])
            return "?"

        output.append(NAMED_PARAM.sub(replace, segment))
    return "".join(output), ordered


class BaseAdapter(abc.ABC):
    """Common connection lifecycle, retry and normalisation logic."""

    db_type = "base"

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.legacy_encoding = config.get("legacy_encoding", "cp1252")
        self.connect_retries = int(config.get("connect_retries", 5))
        self.connect_base_delay = float(config.get("connect_base_delay_seconds", 2))
        self.connect_max_delay = float(config.get("connect_max_delay_seconds", 60))
        self.query_timeout = int(config.get("query_timeout_seconds", 300))
        self._conn: Any = None

    # ----------------------------------------------------------- lifecycle
    @abc.abstractmethod
    def _open_connection(self) -> Any:
        """Open and return a DB-API connection."""

    @abc.abstractmethod
    def _transient_errors(self) -> tuple[type[BaseException], ...]:
        """Exception types that justify a reconnect and retry."""

    def _prepare(self, query: str, params: dict[str, Any]) -> tuple[str, Any]:
        """Return the query and parameters in the driver's bind style."""
        return query, {k: coerce_bind_value(v) for k, v in params.items()}

    def _configure_cursor(self, cursor: Any, chunk_size: int) -> None:
        """Driver-specific cursor tuning (array size, timeouts)."""

    def connect(self) -> Any:
        if self._conn is not None:
            return self._conn

        def attempt() -> Any:
            return self._open_connection()

        try:
            self._conn = retry_sync(
                attempt,
                attempts=self.connect_retries,
                base_delay=self.connect_base_delay,
                max_delay=self.connect_max_delay,
                retry_on=self._transient_errors(),
                description=f"Connect to {self.db_type} source '{self.name}'",
            )
        except self._transient_errors() as exc:
            raise ConnectionFailedError(
                f"Cannot connect to '{self.name}' after {self.connect_retries} attempts: {exc}"
            ) from exc
        logger.info("Connected to %s source '%s'", self.db_type, self.name)
        return self._conn

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception as exc:  # closing a broken connection must never crash shutdown
            logger.debug("Ignoring error while closing '%s': %s", self.name, exc)
        finally:
            self._conn = None
            logger.info("Closed connection to '%s'", self.name)

    def __enter__(self) -> "BaseAdapter":
        self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def ping(self) -> bool:
        try:
            for _ in self.fetch_chunks(self.ping_query(), {}, chunk_size=1):
                pass
            return True
        except AdapterError as exc:
            logger.warning("Ping of '%s' failed: %s", self.name, exc)
            return False

    def ping_query(self) -> str:
        return "SELECT 1"

    # ----------------------------------------------------------- extraction
    def _normalize(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            try:
                return raw.decode(self.legacy_encoding).rstrip()
            except UnicodeDecodeError:
                return raw.decode("latin-1").rstrip()
        if isinstance(value, str):
            return value.rstrip()  # CHAR(n) columns are space-padded
        if hasattr(value, "read") and callable(value.read):  # Oracle LOB
            return self._normalize(value.read())
        return value

    @contextmanager
    def _cursor(self, chunk_size: int) -> Iterator[Any]:
        conn = self.connect()
        cursor = conn.cursor()
        try:
            self._configure_cursor(cursor, chunk_size)
            yield cursor
        finally:
            try:
                cursor.close()
            except Exception as exc:
                logger.debug("Ignoring cursor close error on '%s': %s", self.name, exc)

    def fetch_chunks(
        self, query: str, params: dict[str, Any], chunk_size: int = 1000
    ) -> Iterator[Chunk]:
        """Yield lists of row dicts, at most ``chunk_size`` rows each.

        A transient failure before the first chunk is retried after a
        reconnect. A failure mid-stream is raised: the caller only advances
        its watermark for chunks it has fully processed, so the next cycle
        resumes exactly where this one stopped.
        """
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        prepared_query, prepared_params = self._prepare(query, params)
        transient = self._transient_errors()
        attempts = max(1, self.connect_retries)

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            yielded_any = False
            total = 0
            try:
                with self._cursor(chunk_size) as cursor:
                    cursor.execute(prepared_query, prepared_params)
                    if cursor.description is None:
                        raise QueryFailedError(f"Query on '{self.name}' returned no result set")
                    columns = [col[0] for col in cursor.description]
                    while True:
                        rows = cursor.fetchmany(chunk_size)
                        if not rows:
                            break
                        chunk = [
                            {col: self._normalize(val) for col, val in zip(columns, row)}
                            for row in rows
                        ]
                        total += len(chunk)
                        yielded_any = True
                        yield chunk
                logger.debug(
                    "Extracted %d rows from '%s' in %.2fs",
                    total, self.name, time.monotonic() - started,
                )
                return
            except transient as exc:
                self.close()
                if yielded_any or attempt >= attempts:
                    raise QueryFailedError(
                        f"Extraction from '{self.name}' failed after {total} rows: {exc}"
                    ) from exc
                delay = backoff_delay(attempt, self.connect_base_delay, self.connect_max_delay)
                logger.warning(
                    "Query on '%s' failed (attempt %d/%d): %s; reconnecting in %.1fs",
                    self.name, attempt, attempts, exc, delay,
                )
                time.sleep(delay)
            except AdapterError:
                raise
            except Exception as exc:
                raise QueryFailedError(f"Query on '{self.name}' failed: {exc}") from exc


class MSSQLAdapter(BaseAdapter):
    """Microsoft SQL Server via pyodbc and the Microsoft ODBC driver."""

    db_type = "mssql"

    def _connection_string(self) -> str:
        cfg = self.config
        parts = {
            "DRIVER": "{" + cfg.get("driver", "ODBC Driver 18 for SQL Server") + "}",
            "SERVER": f"tcp:{cfg['host']},{int(cfg.get('port', 1433))}",
            "DATABASE": cfg["database"],
            "UID": cfg["username"],
            "PWD": cfg.get("password", ""),
            "Encrypt": "yes" if cfg.get("encrypt", True) else "no",
            "TrustServerCertificate": "yes" if cfg.get("trust_server_certificate", False) else "no",
            "Connection Timeout": str(int(cfg.get("login_timeout_seconds", 15))),
            "ApplicationIntent": "ReadOnly",
            "APP": "MittelConnect",
        }

        def quote(value: str) -> str:
            if any(ch in value for ch in ";{}="):
                return "{" + value.replace("}", "}}") + "}"
            return value

        return ";".join(
            f"{key}={value if key == 'DRIVER' else quote(str(value))}"
            for key, value in parts.items()
        )

    def _open_connection(self) -> Any:
        import pyodbc

        conn = pyodbc.connect(
            self._connection_string(),
            autocommit=True,
            readonly=True,
            timeout=int(self.config.get("login_timeout_seconds", 15)),
        )
        conn.timeout = self.query_timeout
        conn.setdecoding(pyodbc.SQL_CHAR, encoding=self.legacy_encoding)
        conn.setdecoding(pyodbc.SQL_WCHAR, encoding="utf-16le")
        return conn

    def _transient_errors(self) -> tuple[type[BaseException], ...]:
        import pyodbc

        return (pyodbc.OperationalError, pyodbc.InterfaceError)

    def _prepare(self, query: str, params: dict[str, Any]) -> tuple[str, Any]:
        coerced = {k: coerce_bind_value(v) for k, v in params.items()}
        return to_qmark(query, coerced)

    def _configure_cursor(self, cursor: Any, chunk_size: int) -> None:
        cursor.arraysize = chunk_size


class OracleAdapter(BaseAdapter):
    """Oracle Database via python-oracledb (thin mode, no Instant Client needed)."""

    db_type = "oracle"

    def _dsn(self) -> str:
        import oracledb

        if self.config.get("dsn"):
            return self.config["dsn"]
        return oracledb.makedsn(
            self.config["host"],
            int(self.config.get("port", 1521)),
            service_name=self.config["service_name"],
        )

    def _open_connection(self) -> Any:
        import oracledb

        params = oracledb.ConnectParams(
            user=self.config["username"],
            password=self.config.get("password", ""),
            tcp_connect_timeout=float(self.config.get("login_timeout_seconds", 15)),
            expire_time=2,
        )
        if self.config.get("protocol", "tcp") == "tcps":
            params.set(protocol="tcps")
        # Return NUMBER columns as Decimal so quantities keep exact precision.
        oracledb.defaults.fetch_decimals = True
        conn = oracledb.connect(dsn=self._dsn(), params=params)
        conn.call_timeout = self.query_timeout * 1000
        return conn

    def _transient_errors(self) -> tuple[type[BaseException], ...]:
        import oracledb

        return (oracledb.OperationalError, oracledb.InterfaceError)

    def _configure_cursor(self, cursor: Any, chunk_size: int) -> None:
        cursor.arraysize = chunk_size
        cursor.prefetchrows = chunk_size + 1

    def ping_query(self) -> str:
        return "SELECT 1 FROM DUAL"


class SQLiteAdapter(BaseAdapter):
    """SQLite source for sandbox testing and CSV-staging databases."""

    db_type = "sqlite"

    def _open_connection(self) -> Any:
        path = self.config["path"]
        uri = f"file:{path}?mode=ro" if self.config.get("read_only", True) else path
        conn = sqlite3.connect(
            uri,
            uri=self.config.get("read_only", True),
            timeout=float(self.config.get("login_timeout_seconds", 15)),
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        return conn

    def _transient_errors(self) -> tuple[type[BaseException], ...]:
        return (sqlite3.OperationalError,)

    def _prepare(self, query: str, params: dict[str, Any]) -> tuple[str, Any]:
        # SQLite compares ISO-8601 text natively; keep watermarks as strings.
        return query, dict(params)

    def _configure_cursor(self, cursor: Any, chunk_size: int) -> None:
        cursor.arraysize = chunk_size


ADAPTERS: dict[str, type[BaseAdapter]] = {
    "mssql": MSSQLAdapter,
    "oracle": OracleAdapter,
    "sqlite": SQLiteAdapter,
}


def create_adapter(name: str, config: dict) -> BaseAdapter:
    db_type = config.get("type")
    adapter_cls: Optional[type[BaseAdapter]] = ADAPTERS.get(db_type or "")
    if adapter_cls is None:
        raise AdapterError(f"Unsupported database type '{db_type}' for source '{name}'")
    return adapter_cls(name, config)
