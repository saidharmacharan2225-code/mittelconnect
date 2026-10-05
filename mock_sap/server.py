"""Mock SAP S/4HANA Gateway for the local sandbox (standard library only).

Implements just enough of SAP's behaviour to exercise MittelConnect end to end:
  POST /sap/bc/sec/oauth2/token          OAuth2 client credentials
  GET  <service>/  (x-csrf-token: Fetch) CSRF token handshake
  POST <service>/$batch                  OData V2 multipart batch, per-changeset results

Control endpoints for drills (not part of SAP):
  GET  /_mock/stats                      counts of received / rejected records
  POST /_mock/outage?down=true|false     simulate SAP returning 503
  GET  /_mock/healthz                    liveness probe

Records whose Material starts with "INVALID" are rejected with HTTP 400,
mimicking an SAP business error. Accepted records are appended to a JSONL file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LISTEN_HOST = os.environ.get("MOCK_SAP_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MOCK_SAP_PORT", "8080"))
CLIENT_ID = os.environ.get("MOCK_SAP_CLIENT_ID", "MITTELCONNECT")
CLIENT_SECRET = os.environ.get("MOCK_SAP_CLIENT_SECRET", "sandbox-secret")
TOKEN_TTL = int(os.environ.get("MOCK_SAP_TOKEN_TTL", "300"))
OUTPUT_FILE = os.environ.get("MOCK_SAP_OUTPUT", "/data/received.jsonl")

logging.basicConfig(level=logging.INFO, format="%(asctime)s mock-sap %(levelname)s %(message)s")
log = logging.getLogger("mock-sap")


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tokens: dict[str, float] = {}
        self.csrf = secrets.token_hex(16)
        self.down = False
        self.received = 0
        self.rejected = 0
        self.batches = 0


STATE = State()


def boundary_of(content_type: str) -> str | None:
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    return match.group(1) if match else None


def split_parts(body: str, boundary: str) -> list[str]:
    parts = []
    for chunk in body.split(f"--{boundary}")[1:]:
        if chunk.startswith("--"):
            break
        parts.append(chunk.strip("\r\n"))
    return parts


def head_body(text: str) -> tuple[str, str]:
    for sep in ("\r\n\r\n", "\n\n"):
        if sep in text:
            head, body = text.split(sep, 1)
            return head, body
    return text, ""


def http_part(status: int, reason: str, payload: dict) -> list[str]:
    return [
        "Content-Type: application/http",
        "Content-Transfer-Encoding: binary",
        "",
        f"HTTP/1.1 {status} {reason}",
        "Content-Type: application/json",
        "",
        json.dumps(payload, ensure_ascii=False),
    ]


class Handler(BaseHTTPRequestHandler):
    server_version = "MockSAPGateway/1.0"

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, headers: dict | None = None) -> None:
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        self._send(status, json.dumps(payload).encode("utf-8"), merged)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:]
        with STATE.lock:
            expiry = STATE.tokens.get(token)
        return expiry is not None and expiry > time.time()

    # ----------------------------------------------------------------- GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/_mock/healthz":
            self._json(200, {"status": "ok"})
            return
        if path == "/_mock/stats":
            with STATE.lock:
                self._json(200, {"received": STATE.received, "rejected": STATE.rejected,
                                 "batches": STATE.batches, "down": STATE.down})
            return
        if STATE.down:
            self._json(503, {"error": {"code": "/IWFND/CM_BEC/026", "message": {"value": "Service unavailable"}}})
            return
        if not self._authorized():
            self._send(401, b"Unauthorized", {"WWW-Authenticate": "Bearer"})
            return
        headers = {}
        if self.headers.get("x-csrf-token", "").lower() == "fetch":
            headers["x-csrf-token"] = STATE.csrf
        self._json(200, {"d": {"EntitySets": []}}, headers)

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_body()

        if parsed.path == "/_mock/outage":
            down = parse_qs(parsed.query).get("down", ["true"])[0].lower() == "true"
            with STATE.lock:
                STATE.down = down
            log.warning("Simulated outage %s", "ON" if down else "OFF")
            self._json(200, {"down": down})
            return

        if STATE.down:
            self._json(503, {"error": {"code": "/IWFND/CM_BEC/026", "message": {"value": "Service unavailable"}}})
            return

        if parsed.path.endswith("/oauth2/token"):
            self._token(body)
            return

        if parsed.path.endswith("/$batch"):
            if not self._authorized():
                self._send(401, b"Unauthorized", {"WWW-Authenticate": "Bearer"})
                return
            if self.headers.get("x-csrf-token") != STATE.csrf:
                self._send(403, b"CSRF token validation failed", {"x-csrf-token": "Required"})
                return
            self._batch(body)
            return

        self._json(404, {"error": {"message": {"value": f"No route for {parsed.path}"}}})

    def _token(self, body: bytes) -> None:
        import base64

        form = parse_qs(body.decode("utf-8"))
        auth = self.headers.get("Authorization", "")
        client_id = client_secret = ""
        if auth.startswith("Basic "):
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            client_id, _, client_secret = decoded.partition(":")
        if form.get("grant_type", [""])[0] != "client_credentials":
            self._json(400, {"error": "unsupported_grant_type"})
            return
        if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
            self._json(401, {"error": "invalid_client"})
            return
        token = secrets.token_urlsafe(32)
        with STATE.lock:
            now = time.time()
            STATE.tokens = {t: e for t, e in STATE.tokens.items() if e > now}
            STATE.tokens[token] = now + TOKEN_TTL
        self._json(200, {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_TTL})

    def _batch(self, raw: bytes) -> None:
        boundary = boundary_of(self.headers.get("Content-Type", ""))
        if not boundary:
            self._json(400, {"error": {"message": {"value": "Missing batch boundary"}}})
            return
        text = raw.decode("utf-8")
        response_boundary = f"batchresponse_{secrets.token_hex(8)}"
        out: list[str] = []
        accepted: list[dict] = []
        rejected = 0

        for part in split_parts(text, boundary):
            part_head, part_body = head_body(part)
            changeset = boundary_of(part_head)
            if not changeset:
                continue
            records = []
            for request in split_parts(part_body, changeset):
                _, http_text = head_body(request)
                _, json_body = head_body(http_text)
                records.append(json.loads(json_body.strip()))

            invalid = [r for r in records if str(r.get("Material", "")).upper().startswith("INVALID")]
            out.append(f"--{response_boundary}")
            if invalid:
                rejected += len(records)
                out.extend(http_part(400, "Bad Request", {"error": {
                    "code": "M3/305",
                    "message": {"lang": "de", "value": f"Material {invalid[0].get('Material')} ist nicht vorhanden"},
                }}))
                continue
            inner = f"changesetresponse_{secrets.token_hex(8)}"
            out.append(f"Content-Type: multipart/mixed; boundary={inner}")
            out.append("")
            for record in records:
                out.append(f"--{inner}")
                out.extend(http_part(201, "Created", {"d": record}))
            out.append(f"--{inner}--")
            accepted.extend(records)
        out.append(f"--{response_boundary}--")

        if accepted:
            os.makedirs(os.path.dirname(OUTPUT_FILE) or ".", exist_ok=True)
            with open(OUTPUT_FILE, "a", encoding="utf-8") as handle:
                for record in accepted:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        with STATE.lock:
            STATE.received += len(accepted)
            STATE.rejected += rejected
            STATE.batches += 1
        log.info("$batch: %d accepted, %d rejected", len(accepted), rejected)
        self._send(202, "\r\n".join(out).encode("utf-8"),
                   {"Content-Type": f"multipart/mixed; boundary={response_boundary}"})


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("Mock SAP gateway listening on %s:%d", LISTEN_HOST, LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
