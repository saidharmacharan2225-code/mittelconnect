"""Asynchronous SAP S/4HANA OData V2 client with batch upload.

Features:
* OAuth2 client-credentials tokens refreshed before expiry (or Basic auth for
  classic communication users), with a forced refresh on HTTP 401.
* SAP CSRF token handshake (``x-csrf-token: Fetch``) with automatic re-fetch
  when SAP answers 403 "CSRF token validation failed".
* OData ``$batch`` (multipart/mixed) upload: either one atomic changeset per
  batch or one changeset per record so a single bad record cannot block the
  others. Responses are parsed per record.
* Exponential backoff with jitter for transport errors, 429 and 5xx, honouring
  ``Retry-After``; a circuit breaker fails fast while SAP is down so the
  pipeline can park data in the local outbox immediately.
* Egress allowlist (data sovereignty) and TLS 1.2+ with optional internal CA
  and mutual-TLS client certificates. System proxies are ignored unless
  explicitly enabled so data cannot leave through an unexpected route.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from core.resilience import CircuitBreaker, CircuitOpenError, backoff_delay

logger = logging.getLogger(__name__)

CRLF = "\r\n"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class SAPError(Exception):
    """Base class for SAP client errors."""


class SAPUnavailableError(SAPError):
    """SAP could not be reached or kept failing; data must be cached locally."""


class SAPAuthError(SAPError):
    """Authentication failed (wrong credentials or revoked client)."""


class SAPRequestError(SAPError):
    """SAP rejected the whole request (e.g. unknown service path)."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class EgressBlockedError(SAPError):
    """The target host is not on the data-sovereignty allowlist."""


@dataclass
class RecordResult:
    index: int
    status: int
    message: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status in RETRYABLE_STATUS

    @property
    def rejected(self) -> bool:
        return not self.ok and not self.retryable


@dataclass
class BatchResult:
    results: list[RecordResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[RecordResult]:
        return [r for r in self.results if r.ok]

    @property
    def rejected(self) -> list[RecordResult]:
        return [r for r in self.results if r.rejected]

    @property
    def retryable(self) -> list[RecordResult]:
        return [r for r in self.results if not r.ok and r.retryable]


@dataclass
class HttpPart:
    status: int
    headers: dict[str, str]
    body: str


def check_egress(url: str, allowed_suffixes: list[str], allow_http: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise EgressBlockedError(f"Unsupported URL scheme in {url!r}")
    if parsed.scheme == "http" and not allow_http:
        raise EgressBlockedError(f"Plain HTTP is disabled; refusing {url!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise EgressBlockedError(f"URL has no host: {url!r}")
    for suffix in allowed_suffixes:
        suffix = suffix.lower()
        if host == suffix.lstrip(".") or (suffix.startswith(".") and host.endswith(suffix)):
            return
    raise EgressBlockedError(
        f"Host '{host}' is not in security.allowed_host_suffixes; data egress blocked"
    )


def build_ssl_context(tls: dict) -> ssl.SSLContext | bool:
    ca_bundle = tls.get("ca_bundle") or None
    context = ssl.create_default_context(cafile=ca_bundle)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if tls.get("client_cert"):
        context.load_cert_chain(tls["client_cert"], tls.get("client_key") or None)
    if not tls.get("verify", True):
        logger.warning("TLS certificate verification is DISABLED for SAP; sandbox use only")
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _parse_headers(block: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in block.split("\n"):
        line = line.rstrip("\r")
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return headers


def _split_head_body(text: str) -> tuple[str, str]:
    for separator in ("\r\n\r\n", "\n\n"):
        if separator in text:
            head, body = text.split(separator, 1)
            return head, body
    return text, ""


def _boundary_from(content_type: str) -> Optional[str]:
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            return piece.split("=", 1)[1].strip().strip('"')
    return None


def _split_multipart(body: str, boundary: str) -> list[str]:
    delimiter = f"--{boundary}"
    parts: list[str] = []
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith("--"):
            break
        parts.append(chunk.strip("\r\n"))
    return parts


def _parse_http_part(text: str) -> HttpPart:
    head, body = _split_head_body(text.lstrip("\r\n"))
    lines = head.replace("\r\n", "\n").split("\n")
    status = 0
    if lines and lines[0].upper().startswith("HTTP/"):
        tokens = lines[0].split(" ", 2)
        if len(tokens) >= 2 and tokens[1].isdigit():
            status = int(tokens[1])
    return HttpPart(status=status, headers=_parse_headers("\n".join(lines[1:])), body=body.strip())


def parse_batch_response(content_type: str, body: str) -> list[list[HttpPart]]:
    """Return one list of HTTP responses per top-level batch part.

    A successful changeset yields a list with one response per request; a
    failed changeset is answered by SAP with a single error response.
    """
    boundary = _boundary_from(content_type)
    if not boundary:
        raise SAPError(f"Batch response has no multipart boundary: {content_type!r}")
    groups: list[list[HttpPart]] = []
    for part in _split_multipart(body, boundary):
        part_head, part_body = _split_head_body(part)
        part_headers = _parse_headers(part_head)
        part_type = part_headers.get("content-type", "")
        if part_type.lower().startswith("multipart/mixed"):
            inner_boundary = _boundary_from(part_type)
            if not inner_boundary:
                raise SAPError("Changeset response without boundary")
            inner = []
            for inner_part in _split_multipart(part_body, inner_boundary):
                _, inner_body = _split_head_body(inner_part)
                inner.append(_parse_http_part(inner_body))
            groups.append(inner)
        else:
            groups.append([_parse_http_part(part_body)])
    return groups


def extract_error_message(body: str) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body[:500]
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    message = error.get("message", {})
    if isinstance(message, dict):
        text = message.get("value", "")
    else:
        text = str(message)
    code = error.get("code", "")
    details = error.get("innererror", {}).get("errordetails", []) if isinstance(error.get("innererror"), dict) else []
    extra = "; ".join(d.get("message", "") for d in details if isinstance(d, dict) and d.get("message"))
    combined = f"{code}: {text}" if code else text
    if extra and extra not in combined:
        combined = f"{combined} ({extra})"
    return combined[:1000] or body[:500]


class TokenProvider:
    """Caches an OAuth2 access token and refreshes it before expiry."""

    def __init__(self, client: httpx.AsyncClient, oauth: dict):
        self._client = client
        self._token_url = oauth["token_url"]
        self._client_id = oauth["client_id"]
        self._client_secret = oauth.get("client_secret", "")
        self._scope = oauth.get("scope", "")
        self._skew = float(oauth.get("refresh_skew_seconds", 60))
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    async def get_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at - self._skew:
                return self._token
            await self._refresh()
            assert self._token is not None
            return self._token

    async def _refresh(self) -> None:
        data = {"grant_type": "client_credentials"}
        if self._scope:
            data["scope"] = self._scope
        try:
            response = await self._client.post(
                self._token_url,
                data=data,
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.TransportError as exc:
            raise SAPUnavailableError(f"Token endpoint unreachable: {exc}") from exc
        if response.status_code in (400, 401, 403):
            raise SAPAuthError(
                f"Token request rejected ({response.status_code}): {response.text[:300]}"
            )
        if response.status_code >= 500 or response.status_code == 429:
            raise SAPUnavailableError(f"Token endpoint returned {response.status_code}")
        if response.status_code != 200:
            raise SAPAuthError(f"Unexpected token response {response.status_code}")
        try:
            payload = response.json()
            self._token = payload["access_token"]
            lifetime = float(payload.get("expires_in", 3600))
        except (ValueError, KeyError) as exc:
            raise SAPAuthError("Token response is not valid OAuth2 JSON") from exc
        self._expires_at = time.monotonic() + lifetime
        logger.info("Obtained SAP OAuth2 token valid for %.0fs", lifetime)


class SAPClient:
    """Batch uploader for SAP S/4HANA / ECC (Gateway) OData V2 services."""

    def __init__(
        self,
        config: dict,
        allowed_host_suffixes: list[str],
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep=asyncio.sleep,
    ):
        self.config = config
        self.base_url = config["base_url"].rstrip("/")
        self.sap_client = str(config.get("sap_client", "") or "")
        self.auth_mode = config.get("auth_mode", "oauth2")
        self.batch_size = int(config.get("batch_size", 100))
        self.atomic = bool(config.get("atomic_batches", False))
        retry = config.get("retry", {})
        self.max_attempts = max(1, int(retry.get("max_attempts", 5)))
        self.base_delay = float(retry.get("base_delay_seconds", 1))
        self.max_delay = float(retry.get("max_delay_seconds", 30))
        breaker_cfg = config.get("circuit_breaker", {})
        self.breaker = CircuitBreaker(
            "sap",
            failure_threshold=int(breaker_cfg.get("failure_threshold", 5)),
            recovery_timeout=float(breaker_cfg.get("recovery_timeout_seconds", 120)),
        )
        self._sleep = sleep
        allow_http = bool(config.get("allow_insecure_http", False))

        check_egress(self.base_url, allowed_host_suffixes, allow_http)
        if self.auth_mode == "oauth2":
            check_egress(config["oauth2"]["token_url"], allowed_host_suffixes, allow_http)

        timeouts = config.get("timeouts", {})
        timeout = httpx.Timeout(
            connect=float(timeouts.get("connect_seconds", 10)),
            read=float(timeouts.get("read_seconds", 120)),
            write=float(timeouts.get("write_seconds", 60)),
            pool=float(timeouts.get("pool_seconds", 10)),
        )
        max_conn = int(config.get("max_connections", 10))
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "limits": httpx.Limits(max_connections=max_conn, max_keepalive_connections=max_conn),
            "trust_env": bool(config.get("use_system_proxy", False)),
            "follow_redirects": False,
            "headers": {"User-Agent": "MittelConnect/1.0"},
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        else:
            client_kwargs["verify"] = build_ssl_context(config.get("tls", {}))
        self._client = httpx.AsyncClient(**client_kwargs)

        self._tokens: Optional[TokenProvider] = None
        if self.auth_mode == "oauth2":
            self._tokens = TokenProvider(self._client, config["oauth2"])
        self._csrf: dict[str, str] = {}
        self._csrf_lock = asyncio.Lock()

    async def __aenter__(self) -> "SAPClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ auth
    async def _auth(self) -> tuple[dict[str, str], Optional[tuple[str, str]]]:
        if self.auth_mode == "oauth2":
            assert self._tokens is not None
            token = await self._tokens.get_token()
            return {"Authorization": f"Bearer {token}"}, None
        if self.auth_mode == "basic":
            basic = self.config.get("basic", {})
            return {}, (basic.get("username", ""), basic.get("password", ""))
        return {}, None

    def _params(self) -> dict[str, str]:
        return {"sap-client": self.sap_client} if self.sap_client else {}

    async def _fetch_csrf(self, service_path: str) -> str:
        async with self._csrf_lock:
            if service_path in self._csrf:
                return self._csrf[service_path]
            headers, auth = await self._auth()
            headers.update({"x-csrf-token": "Fetch", "Accept": "application/json"})
            response = await self._client.get(
                f"{self.base_url}{service_path}/",
                headers=headers,
                auth=auth,
                params=self._params(),
            )
            if response.status_code == 401:
                if self._tokens:
                    self._tokens.invalidate()
                raise SAPAuthError("SAP rejected credentials while fetching CSRF token")
            if response.status_code in RETRYABLE_STATUS:
                raise SAPUnavailableError(f"CSRF fetch returned {response.status_code}")
            if response.status_code >= 400:
                raise SAPRequestError(
                    f"CSRF fetch for {service_path} returned {response.status_code}",
                    response.status_code,
                )
            token = response.headers.get("x-csrf-token", "")
            if not token or token.lower() == "required":
                raise SAPRequestError("SAP did not issue a CSRF token", response.status_code)
            self._csrf[service_path] = token
            return token

    async def health_check(self, service_path: str) -> bool:
        try:
            await self.breaker.before_call()
            self._csrf.pop(service_path, None)
            await self._fetch_csrf(service_path)
            await self.breaker.record_success()
            return True
        except CircuitOpenError:
            return False
        except (SAPError, httpx.TransportError) as exc:
            await self.breaker.record_failure()
            logger.warning("SAP health check failed: %s", exc)
            return False

    # ----------------------------------------------------------------- batch
    def build_batch(self, entity_set: str, records: list[dict]) -> tuple[str, str]:
        batch_boundary = f"batch_{uuid.uuid4().hex}"
        lines: list[str] = []

        def request_lines(content_id: int, record: dict) -> list[str]:
            payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            return [
                "Content-Type: application/http",
                "Content-Transfer-Encoding: binary",
                f"Content-ID: {content_id}",
                "",
                f"POST {entity_set} HTTP/1.1",
                "Content-Type: application/json",
                "Accept: application/json",
                f"Content-Length: {len(payload.encode('utf-8'))}",
                "",
                payload,
            ]

        groups = [records] if self.atomic else [[record] for record in records]
        content_id = 0
        for group in groups:
            changeset = f"changeset_{uuid.uuid4().hex}"
            lines.append(f"--{batch_boundary}")
            lines.append(f"Content-Type: multipart/mixed; boundary={changeset}")
            lines.append("")
            for record in group:
                content_id += 1
                lines.append(f"--{changeset}")
                lines.extend(request_lines(content_id, record))
            lines.append(f"--{changeset}--")
            lines.append("")
        lines.append(f"--{batch_boundary}--")
        lines.append("")
        return batch_boundary, CRLF.join(lines)

    def _map_results(self, groups: list[list[HttpPart]], count: int) -> BatchResult:
        results: list[RecordResult] = []
        if self.atomic:
            parts = groups[0] if groups else []
            failures = [p for p in parts if not 200 <= p.status < 300]
            if not parts:
                return BatchResult([RecordResult(i, 0, "Empty changeset response") for i in range(count)])
            if failures:
                error = failures[0]
                message = extract_error_message(error.body)
                return BatchResult([RecordResult(i, error.status, message) for i in range(count)])
            for index in range(count):
                part = parts[index] if index < len(parts) else parts[-1]
                results.append(RecordResult(index, part.status))
            return BatchResult(results)

        for index in range(count):
            if index >= len(groups) or not groups[index]:
                results.append(RecordResult(index, 0, "No response for changeset"))
                continue
            part = groups[index][0]
            message = "" if 200 <= part.status < 300 else extract_error_message(part.body)
            results.append(RecordResult(index, part.status, message))
        return BatchResult(results)

    async def _post_once(self, service_path: str, entity_set: str, records: list[dict]) -> httpx.Response:
        csrf = await self._fetch_csrf(service_path)
        headers, auth = await self._auth()
        boundary, body = self.build_batch(entity_set, records)
        headers.update(
            {
                "Content-Type": f"multipart/mixed; boundary={boundary}",
                "Accept": "multipart/mixed",
                "x-csrf-token": csrf,
            }
        )
        return await self._client.post(
            f"{self.base_url}{service_path}/$batch",
            content=body.encode("utf-8"),
            headers=headers,
            auth=auth,
            params=self._params(),
        )

    async def post_batch(self, service_path: str, entity_set: str, records: list[dict]) -> BatchResult:
        """Upload ``records`` (at most ``batch_size``) as one OData $batch.

        Raises SAPUnavailableError / CircuitOpenError when SAP cannot take the
        data now (caller caches it), SAPAuthError on credential problems and
        SAPRequestError when SAP rejects the request as a whole.
        """
        if not records:
            return BatchResult()
        if len(records) > self.batch_size:
            raise ValueError(f"Batch of {len(records)} exceeds sap.batch_size {self.batch_size}")

        auth_refreshed = False
        csrf_refreshed = False
        last_error = "unknown error"
        attempt = 0
        while attempt < self.max_attempts:
            attempt += 1
            await self.breaker.before_call()
            try:
                response = await self._post_once(service_path, entity_set, records)
            except (httpx.TransportError, SAPUnavailableError) as exc:
                await self.breaker.record_failure()
                last_error = f"{type(exc).__name__}: {exc}"
                await self._backoff(attempt, last_error, None)
                continue
            except SAPAuthError:
                await self.breaker.record_success()
                if not auth_refreshed and self._tokens is not None:
                    auth_refreshed = True
                    self._tokens.invalidate()
                    attempt -= 1
                    continue
                raise
            except SAPRequestError:
                await self.breaker.record_success()
                raise
            except Exception:
                await self.breaker.record_failure()
                raise

            status = response.status_code
            if status not in RETRYABLE_STATUS:
                # SAP answered: the upstream is alive even if this request is refused.
                await self.breaker.record_success()
            if status == 401 and not auth_refreshed:
                auth_refreshed = True
                if self._tokens is not None:
                    self._tokens.invalidate()
                self._csrf.pop(service_path, None)
                attempt -= 1
                continue
            if status == 403 and response.headers.get("x-csrf-token", "").lower() == "required" and not csrf_refreshed:
                csrf_refreshed = True
                self._csrf.pop(service_path, None)
                attempt -= 1
                continue
            if status in RETRYABLE_STATUS:
                await self.breaker.record_failure()
                last_error = f"HTTP {status}: {response.text[:200]}"
                await self._backoff(attempt, last_error, response.headers.get("retry-after"))
                continue

            if status == 401:
                raise SAPAuthError("SAP rejected credentials after token refresh")
            if status >= 400:
                raise SAPRequestError(
                    f"SAP rejected $batch ({status}): {extract_error_message(response.text)}",
                    status,
                )
            groups = parse_batch_response(response.headers.get("content-type", ""), response.text)
            result = self._map_results(groups, len(records))
            logger.info(
                "SAP $batch %s/%s: %d ok, %d rejected, %d retryable",
                service_path, entity_set,
                len(result.succeeded), len(result.rejected), len(result.retryable),
            )
            return result

        raise SAPUnavailableError(
            f"SAP $batch failed after {self.max_attempts} attempts: {last_error}"
        )

    async def _backoff(self, attempt: int, reason: str, retry_after: Optional[str]) -> None:
        if attempt >= self.max_attempts:
            return
        delay = backoff_delay(attempt, self.base_delay, self.max_delay)
        if retry_after and retry_after.strip().isdigit():
            delay = min(self.max_delay, max(delay, float(retry_after)))
        logger.warning(
            "SAP request failed (attempt %d/%d): %s; retrying in %.1fs",
            attempt, self.max_attempts, reason, delay,
        )
        await self._sleep(delay)
