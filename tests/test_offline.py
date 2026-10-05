"""Offline end-to-end tests: SQLite as the legacy source, a mocked SAP gateway.

Run from the project root:  python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx
import yaml

from core.crypto import CryptoError, Pseudonymizer, SecretBox, generate_key
from core.db_adapters import SQLiteAdapter, to_qmark
from core.pipeline import Pipeline
from core.resilience import CircuitBreaker, CircuitOpenError, CircuitState
from core.sap_client import SAPClient, parse_batch_response
from core.settings import load_settings


class FakeSAP:
    """Minimal SAP Gateway: OAuth token, CSRF handshake and OData V2 $batch."""

    def __init__(self) -> None:
        self.down = False
        self.received: list[dict] = []
        self.token_requests = 0
        self.expire_next_token = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("simulated network outage", request=request)
        if request.url.path.endswith("/oauth2/token"):
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_requests}", "expires_in": 3600})
        auth = request.headers.get("authorization", "")
        if self.expire_next_token and auth == "Bearer tok1":
            self.expire_next_token = False
            return httpx.Response(401, text="token expired")
        if request.method == "GET" and request.headers.get("x-csrf-token") == "Fetch":
            return httpx.Response(200, headers={"x-csrf-token": "csrf123"}, json={"d": {}})
        if request.method == "POST" and request.url.path.endswith("/$batch"):
            if request.headers.get("x-csrf-token") != "csrf123":
                return httpx.Response(403, headers={"x-csrf-token": "Required"})
            return self._batch(request)
        return httpx.Response(404)

    def _batch(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        payloads = [json.loads(line) for line in body.split("\r\n") if line.startswith("{")]
        changesets = len(re.findall(r"Content-Type: multipart/mixed; boundary=changeset_", body))
        assert changesets == len(payloads), "one changeset per record expected"
        boundary = "batchresp_1"
        lines: list[str] = []
        for idx, record in enumerate(payloads):
            lines.append(f"--{boundary}")
            if record.get("Material") == "BAD":
                lines += [
                    "Content-Type: application/http",
                    "Content-Transfer-Encoding: binary",
                    "",
                    "HTTP/1.1 400 Bad Request",
                    "Content-Type: application/json",
                    "",
                    json.dumps({"error": {"code": "M3/305", "message": {"value": "Material BAD does not exist"}}}),
                ]
            else:
                self.received.append(record)
                lines += [
                    f"Content-Type: multipart/mixed; boundary=csresp_{idx}",
                    "",
                    f"--csresp_{idx}",
                    "Content-Type: application/http",
                    "Content-Transfer-Encoding: binary",
                    "",
                    "HTTP/1.1 201 Created",
                    "Content-Type: application/json",
                    "",
                    json.dumps({"d": record}),
                    f"--csresp_{idx}--",
                ]
        lines.append(f"--{boundary}--")
        return httpx.Response(
            202,
            headers={"content-type": f"multipart/mixed; boundary={boundary}"},
            content="\r\n".join(lines).encode("utf-8"),
        )


def build_source_db(path: Path, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE Lagerbestand (ArtikelNr TEXT, Werk TEXT, Bestand TEXT, "
        "Mengeneinheit TEXT, LetzterBearbeiter TEXT, LastChanged TEXT)"
    )
    data = []
    for i in range(rows):
        material = "BAD" if i == 7 else f"mat-{i:05d}"
        data.append((material, "1000", f"{i},5", "STK", "Hans Müller", f"2024-01-01T08:{i // 60:02d}:{i % 60:02d}"))
    conn.executemany("INSERT INTO Lagerbestand VALUES (?, ?, ?, ?, ?, ?)", data)
    conn.commit()
    conn.close()


def write_config(tmp: Path, source_db: Path) -> Path:
    config = {
        "service": {"poll_interval_seconds": 1, "heartbeat_file": str(tmp / "heartbeat")},
        "security": {
            "master_key_env": "MITTELCONNECT_MASTER_KEY",
            "master_key_file": "",
            "pseudonymization_key": "${TEST_PSEUDO_KEY}",
            "encrypt_outbox": True,
            "allowed_host_suffixes": [".sap.test"],
        },
        "cache": {
            "sqlite_path": str(tmp / "cache.db"),
            "replay_base_delay_seconds": 0,
            "replay_max_delay_seconds": 0,
            "max_replay_attempts": 5,
        },
        "databases": {"legacy": {"type": "sqlite", "path": str(source_db), "connect_retries": 1}},
        "sap": {
            "base_url": "https://s4.sap.test",
            "sap_client": "100",
            "auth_mode": "oauth2",
            "oauth2": {
                "token_url": "https://s4.sap.test/sap/bc/sec/oauth2/token",
                "client_id": "MC",
                "client_secret": "${TEST_SAP_SECRET}",
            },
            "batch_size": 10,
            "atomic_batches": False,
            "retry": {"max_attempts": 2, "base_delay_seconds": 0, "max_delay_seconds": 0},
            "circuit_breaker": {"failure_threshold": 2, "recovery_timeout_seconds": 0},
        },
        "jobs": [
            {
                "name": "stock",
                "source": "legacy",
                "chunk_size": 8,
                "watermark_column": "LastChanged",
                "initial_watermark": "2000-01-01T00:00:00",
                "query": "SELECT * FROM Lagerbestand WHERE LastChanged > :watermark ORDER BY LastChanged",
                "sap": {"service_path": "/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV", "entity_set": "A_MatlStkInAcctMod"},
                "mapping": {
                    "Material": {"source": "ArtikelNr", "type": "string", "upper": True, "required": True},
                    "Plant": {"source": "Werk", "type": "string", "max_length": 4},
                    "Quantity": {"source": "Bestand", "type": "decimal", "scale": 3},
                    "Unit": {"source": "Mengeneinheit", "type": "string", "value_map": {"STK": "ST"}},
                    "Editor": {"source": "LetzterBearbeiter", "type": "string", "pseudonymize": True},
                    "ChangedOn": {"source": "LastChanged", "type": "datetime"},
                    "Origin": {"type": "constant", "value": "LEGACY"},
                },
            }
        ],
    }
    path = tmp / "config.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return path


async def no_sleep(_seconds: float) -> None:
    return None


class CryptoTests(unittest.TestCase):
    def test_roundtrip_and_tamper(self) -> None:
        box = SecretBox([generate_key().encode()])
        wrapped = box.wrap("Geheim!123")
        self.assertTrue(wrapped.startswith("ENC["))
        self.assertEqual(box.unwrap(wrapped), "Geheim!123")
        self.assertEqual(box.unwrap("plain"), "plain")
        tampered = wrapped[:-3] + ("A" if wrapped[-3] != "A" else "B") + wrapped[-2:]
        with self.assertRaises(CryptoError):
            box.unwrap(tampered)

    def test_rotation(self) -> None:
        old_key, new_key = generate_key().encode(), generate_key().encode()
        old_value = SecretBox([old_key]).wrap("pw")
        both = SecretBox([new_key, old_key])
        rotated = both.rotate(old_value)
        self.assertEqual(SecretBox([new_key]).unwrap(rotated), "pw")

    def test_pseudonymizer(self) -> None:
        p = Pseudonymizer("x" * 32)
        self.assertEqual(p.pseudonymize("Hans Müller"), p.pseudonymize("hans müller "))
        self.assertNotEqual(p.pseudonymize("Hans Müller"), p.pseudonymize("Eva Schmidt"))
        self.assertNotIn("Hans", p.pseudonymize("Hans Müller"))


class AdapterTests(unittest.TestCase):
    def test_qmark_conversion_ignores_literals(self) -> None:
        sql, params = to_qmark("SELECT ':x' AS t FROM a WHERE b > :watermark AND c = :watermark", {"watermark": 5})
        self.assertEqual(sql, "SELECT ':x' AS t FROM a WHERE b > ? AND c = ?")
        self.assertEqual(params, [5, 5])

    def test_sqlite_chunking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "src.db"
            build_source_db(db, 25)
            adapter = SQLiteAdapter("src", {"path": str(db)})
            sizes = [len(c) for c in adapter.fetch_chunks("SELECT * FROM Lagerbestand WHERE LastChanged > :watermark", {"watermark": ""}, 10)]
            adapter.close()
            self.assertEqual(sizes, [10, 10, 5])


class BreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_opens_and_recovers(self) -> None:
        now = [0.0]
        breaker = CircuitBreaker("t", failure_threshold=2, recovery_timeout=10, clock=lambda: now[0])
        await breaker.record_failure()
        await breaker.record_failure()
        self.assertEqual(breaker.state, CircuitState.OPEN)
        with self.assertRaises(CircuitOpenError):
            await breaker.before_call()
        now[0] = 11
        await breaker.before_call()  # half-open trial permitted
        with self.assertRaises(CircuitOpenError):
            await breaker.before_call()  # only one trial at a time
        await breaker.record_success()
        self.assertEqual(breaker.state, CircuitState.CLOSED)


class BatchParserTests(unittest.TestCase):
    def test_parse_mixed(self) -> None:
        body = (
            "--b\r\nContent-Type: multipart/mixed; boundary=c\r\n\r\n--c\r\nContent-Type: application/http\r\n\r\n"
            "HTTP/1.1 201 Created\r\n\r\n{}\r\n--c--\r\n--b\r\nContent-Type: application/http\r\n\r\n"
            "HTTP/1.1 400 Bad Request\r\n\r\n{\"error\":{}}\r\n--b--"
        )
        groups = parse_batch_response("multipart/mixed; boundary=b", body)
        self.assertEqual([g[0].status for g in groups], [201, 400])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.key = generate_key()
        os.environ["MITTELCONNECT_MASTER_KEY"] = self.key
        os.environ["TEST_PSEUDO_KEY"] = "pseudo-key-for-tests-0123456789"
        os.environ["TEST_SAP_SECRET"] = SecretBox([self.key.encode()]).wrap("s3cret")
        self.source = self.tmp / "legacy.db"
        build_source_db(self.source, 30)
        self.settings = load_settings(write_config(self.tmp, self.source))
        self.fake = FakeSAP()
        sap = SAPClient(
            self.settings.sap,
            self.settings.security["allowed_host_suffixes"],
            transport=httpx.MockTransport(self.fake.handler),
            sleep=no_sleep,
        )
        self.pipeline = Pipeline(self.settings, sap_client=sap)

    async def asyncTearDown(self) -> None:
        await self.pipeline.close()
        self._tmp.cleanup()
        for name in ("MITTELCONNECT_MASTER_KEY", "TEST_PSEUDO_KEY", "TEST_SAP_SECRET"):
            os.environ.pop(name, None)

    async def test_secret_was_decrypted(self) -> None:
        self.assertEqual(self.settings.sap["oauth2"]["client_secret"], "s3cret")

    async def test_happy_path(self) -> None:
        report = await self.pipeline.run_cycle()
        job = report.jobs[0]
        self.assertEqual(job.extracted, 30)
        self.assertEqual(job.delivered, 29)
        self.assertEqual(job.rejected, 1)
        self.assertEqual(report.dead_letters, 1)
        self.assertEqual(report.outbox_records, 0)
        first = self.fake.received[0]
        self.assertEqual(first["Material"], "MAT-00000")
        self.assertEqual(first["Unit"], "ST")
        self.assertEqual(first["Quantity"], "0.500")
        self.assertTrue(first["Editor"].startswith("PSN"))
        self.assertRegex(first["ChangedOn"], r"^/Date\(\d+\)/$")
        self.assertEqual(self.pipeline.store.get_watermark("stock"), "2024-01-01T08:00:29")
        again = await self.pipeline.run_cycle()
        self.assertEqual(again.jobs[0].extracted, 0)

    async def test_outage_caches_then_replays(self) -> None:
        self.fake.down = True
        report = await self.pipeline.run_cycle()
        job = report.jobs[0]
        self.assertEqual(job.extracted, 30)
        self.assertEqual(job.delivered, 0)
        self.assertEqual(job.cached, 30)
        self.assertEqual(report.outbox_records, 30)
        raw = sqlite3.connect(self.tmp / "cache.db").execute("SELECT payload FROM outbox").fetchone()[0]
        self.assertNotIn(b"MAT-", raw)  # outbox is encrypted at rest

        self.fake.down = False
        recovered = await self.pipeline.run_cycle()
        self.assertEqual(recovered.replayed_ok, 29)
        self.assertEqual(recovered.replay_rejected, 1)
        self.assertEqual(recovered.outbox_records, 0)
        self.assertEqual(len(self.fake.received), 29)

    async def test_long_outage_never_dead_letters(self) -> None:
        self.fake.down = True
        for _ in range(8):  # max_replay_attempts is 5 in the test config
            report = await self.pipeline.run_cycle()
        self.assertEqual(report.outbox_records, 30)
        self.assertEqual(report.dead_letters, 0)

        self.fake.down = False
        recovered = await self.pipeline.run_cycle()
        self.assertEqual(recovered.replayed_ok, 29)
        self.assertEqual(recovered.outbox_records, 0)

    async def test_transform_error_dead_letter_is_pseudonymised(self) -> None:
        conn = sqlite3.connect(self.source)
        conn.execute(
            "INSERT INTO Lagerbestand VALUES ('', '1000', '1', 'STK', 'Erika Mustermann', '2024-01-01T09:00:00')"
        )
        conn.commit()
        conn.close()
        report = await self.pipeline.run_cycle()
        self.assertEqual(report.jobs[0].transform_errors, 1)
        rows = sqlite3.connect(self.tmp / "cache.db").execute("SELECT payload, encrypted FROM dead_letters").fetchall()
        payloads = [self.pipeline.store._open(payload, encrypted) for payload, encrypted in rows]
        flat = json.dumps(payloads, ensure_ascii=False)
        self.assertNotIn("Erika Mustermann", flat)
        self.assertIn("PSN", flat)

    async def test_token_refresh_on_401(self) -> None:
        self.fake.expire_next_token = True
        report = await self.pipeline.run_cycle()
        self.assertEqual(report.jobs[0].delivered, 29)
        self.assertGreaterEqual(self.fake.token_requests, 2)

    async def test_egress_guard(self) -> None:
        from core.sap_client import EgressBlockedError

        cfg = dict(self.settings.sap, base_url="https://evil.example.com")
        with self.assertRaises(EgressBlockedError):
            SAPClient(cfg, [".sap.test"], transport=httpx.MockTransport(self.fake.handler))


if __name__ == "__main__":
    unittest.main()
