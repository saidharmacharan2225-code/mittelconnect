# MittelConnect

Local-first middleware that streams data from legacy MS SQL Server and Oracle
databases into SAP S/4HANA / ECC OData services. Everything runs on-premise or
in an EU cloud; data only leaves towards hosts on an explicit allowlist.

## Layout

| Path | Purpose |
|---|---|
| `config.yaml` | Sources, SAP endpoint, jobs and field mappings. Secrets as `${ENV}` or `ENC[...]`. |
| `core/crypto.py` | Fernet secret encryption (with key rotation) and HMAC pseudonymisation. |
| `core/settings.py` | Loads YAML, substitutes env vars, decrypts `ENC[...]`, validates. |
| `core/resilience.py` | Exponential backoff with jitter and the circuit breaker. |
| `core/db_adapters.py` | Chunked, read-only extraction for MSSQL (pyodbc), Oracle (oracledb), SQLite. |
| `core/sap_client.py` | Async OData V2 `$batch` client: OAuth2 refresh, CSRF, per-record results, egress guard. |
| `core/pipeline.py` | Extract, transform, deliver; encrypted SQLite outbox, dead letters, watermarks. |
| `main.py` | Daemon with JSON logging, SIGTERM/SIGINT handling and admin commands. |
| `monitor/` | `mcmon`, the Go sidecar: Prometheus `/metrics`, `/healthz`, `/readyz` from the cache (read-only). |
| `deploy/docker-compose.prod.yml` | Production stack: middleware + mcmon, one customer site per host. |
| `deploy/hetzner/` | Terraform for Hetzner Cloud (EU only): hardened host, data volume, WireGuard to the plant. |
| `deploy/monitoring/` | Prometheus scrape config and alert rules. |
| `scripts/deploy.sh`, `scripts/site_init.sh` | SSH deployment and on-host key/credential set-up. |
| `.github/workflows/ci.yml` | CI: Python and Go tests, shellcheck, Terraform validate, image build and Trivy scan. |
| `docs/RUNBOOK.md`, `docs/OUTAGE_DRILL.md` | Step-by-step operations and the outage drill. |
| `docs/DEPLOYMENT.md` | Production deployment (edge server or Hetzner), monitoring and scaling. |
| `tests/test_offline.py` | Offline tests with a SQLite source and a mocked SAP gateway (outage included). |

## Quick start (no live DB or SAP needed)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
(cd monitor && go test ./...)                # Go monitor, needs Go 1.26
```

## Configure for a real plant

```bash
python main.py generate-key --out ./secrets/master.key       # back this up offline
export MC_MASTER_KEY_FILE=./secrets/master.key
python main.py encrypt-secret                                # paste DB password -> ENC[...]
export MC_MSSQL_PASSWORD='ENC[...]'
export MC_SAP_CLIENT_SECRET='ENC[...]'
export MC_PSEUDONYMIZATION_KEY='ENC[...]'
python main.py check-config                                  # prints a redacted config
python main.py run --once                                    # one cycle
python main.py run                                           # daemon
python main.py status                                        # outbox / dead letters / watermarks
```

The MSSQL adapter needs the system ODBC stack (`unixodbc` and Microsoft
`msodbcsql18`); Module 2's Dockerfile installs it. Oracle uses thin mode and
needs no Instant Client.

## Local sandbox (Docker)

```bash
./scripts/bootstrap_sandbox.sh        # builds the image, creates secrets/master.key and .env
docker compose up -d --build          # SQL Server, seed, factory simulator, mock SAP, middleware
docker compose logs -f middleware
curl -s http://127.0.0.1:8080/_mock/stats                      # records SAP received
curl -s -X POST "http://127.0.0.1:8080/_mock/outage?down=true"  # simulate SAP outage
docker compose exec middleware python /app/main.py status       # outbox / watermarks
docker compose down -v                # stop and wipe all sandbox data
```

The seed creates 500 stock rows in `PRODUKTION.dbo.Lagerbestand` (5 of them
named `INVALID-*`, which the mock SAP rejects so dead letters can be seen) and
a read-only `mc_reader` login. The simulator adds one row every 15 seconds.
The middleware container runs as uid 10001 with a read-only root filesystem,
no Linux capabilities and the master key as a Docker secret. Its two networks
keep the database and SAP sides apart. The master key file is world-readable
inside the 0700 `secrets/` directory so the container user can read it; the
middleware logs a warning about that at start-up, which is expected in the
sandbox. Production should use a 0400 file owned by uid 10001.

## Production

```bash
cd deploy/hetzner && terraform apply                     # or prepare an edge VM at the plant
scripts/deploy.sh deploy@<host> --init <site-name>       # key + credentials, generated on the host
scripts/deploy.sh deploy@<host>                          # validate config, start, wait for healthy
scripts/deploy.sh deploy@<host> --status
```

Full walkthrough, WireGuard set-up, monitoring and scaling: `docs/DEPLOYMENT.md`.

## Delivery semantics

At-least-once. A watermark only advances after every row of a chunk is
accepted by SAP, parked in the encrypted outbox, or stored as a dead letter.
Rows sharing the last timestamp of an interrupted chunk are re-read rather
than skipped. SAP business errors (4xx per record) go to dead letters, which
are purged after `cache.dead_letter_retention_days`.

## Roadmap

1. **Module 1: core codebase** (done).
2. **Module 2: Docker** (done). `Dockerfile`, `docker-compose.yml`,
   `mock_sap/`, `sandbox/`, `scripts/bootstrap_sandbox.sh`.
3. **Module 3: GDPR whitepaper** (done, published as a Claude Doc). TOMs per Art. 32, data-flow chart, and a
   DPA (Auftragsverarbeitungsvertrag) template.
4. **Module 4: sales kit** (done, published as a Claude Doc). Technical discovery questionnaire and the
   €5,000 setup + €3,500/month proposal.
5. **Module 5: runbooks** (done). `docs/RUNBOOK.md` (install, credentials,
   sandbox test, operations) and `docs/OUTAGE_DRILL.md` with the automated
   `scripts/outage_drill.sh`.
6. **Production** (done). Go monitoring sidecar, Hetzner Terraform,
   production compose, deploy scripts, alert rules, CI pipeline and
   `docs/DEPLOYMENT.md`.
