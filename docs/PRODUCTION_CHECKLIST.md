# Production deployment checklist

Use this per customer site, after `docs/DEPLOYMENT.md` sections 1 to 3. Every
box is a yes/no check; a site goes live only when all are ticked.

## What runs where

| Component | Sandbox | Production |
|---|---|---|
| Legacy database | `mssql` container with seed data | **Customer's** SQL Server / Oracle, reached over the plant LAN or WireGuard |
| SAP | `mock-sap` container | **Customer's** S/4HANA gateway over HTTPS |
| Middleware | built locally, `.env` with plaintext test passwords | `deploy/docker-compose.prod.yml`, ENC[...] ciphertexts only |
| Monitor | `docker-compose.monitor.yml` overlay | always on, metrics on the private address |
| Logs | local `docker compose logs` | local json-file plus Loki via `deploy/docker-compose.logging.yml` |

`mssql`, `mssql-init`, `simulator` and `mock-sap` must never run on a customer host.

## 1. Before the site visit

- [ ] Customer signed the AVV (Art. 28 GDPR data processing agreement).
- [ ] SAP team confirmed the target OData service and entity set, and that it accepts creates. Note: in standard S/4HANA, `API_MATERIAL_STOCK_SRV` is a read API; stock postings normally go through `API_MATERIAL_DOCUMENT_SRV`. Confirm with the customer before go-live (inferred from SAP's API catalogue, not tested against a real system).
- [ ] SAP technical user with only the authorisations for that service; OAuth client or basic auth credentials received over a secure channel.
- [ ] Read-only database login (`db_datareader` on the listed tables only).
- [ ] Network path agreed: edge VM (topology A) or WireGuard peer (topology B).

## 2. Host

- [ ] Ubuntu 24.04, Docker Engine 25 or newer (`docker version`). Compose health-check `start_interval` needs Engine 25+.
- [ ] `/etc/docker/daemon.json` has `live-restore`, `no-new-privileges`, `icc: false` (cloud-init sets these on Hetzner hosts).
- [ ] Unattended security upgrades enabled; reboot window agreed with the customer.
- [ ] SSH only from admin addresses, key-only, root login disabled.

## 3. Secrets

- [ ] `scripts/site_init.sh` run on the host: master key generated there, never copied over the network.
- [ ] Site `.env` contains only `ENC[...]` values and settings: `grep -vE '^(#|$|MC_[A-Z_]+_ENC=(ENC\[.*)?$|MC_(SITE_NAME|SITE_DIR|SOURCE_TZ|LOG_LEVEL|HEALTH_MAX_AGE_SECONDS|CPUS|MEMORY|HOST_MASTER_KEY_FILE|LOKI_URL|LOKI_USER)=|MCMON_(BIND|PORT)=)' /srv/mittelconnect/site/.env` prints nothing.
- [ ] Master key backed up **offline** (password manager entry or sealed USB stick), labelled with the site name and date.
- [ ] Key sealed: `sudo scripts/seal_master_key.sh --offline-backup-done`, then `scripts/deploy.sh <target>`.
  - [ ] `ls /srv/mittelconnect/site/secrets` shows `master.key.cred` and no `master.key`.
  - [ ] `systemctl is-enabled mittelconnect-master-key.service` prints `enabled`.
  - [ ] After a test reboot, `docker compose ps` shows the middleware healthy without manual steps.
- [ ] Loki push token stored as `/srv/mittelconnect/site/secrets/loki_password` (0400 root), push-only scope.

Rotating a database or SAP password later: `site_init.sh` needs the plaintext
key at `secrets/master.key`. On a sealed host, encrypt the new value with the
running container instead:
`printf '%s' "$NEW" | docker compose ... exec -T middleware python /app/main.py encrypt-secret --stdin`.

## 4. Deploy

- [ ] `docker compose -f deploy/docker-compose.prod.yml --env-file /srv/mittelconnect/site/.env config -q` passes.
- [ ] `scripts/deploy.sh <target>` completed; `docker compose ... ps` shows `middleware` and `monitor` as `(healthy)`.
- [ ] `docker compose ... exec middleware python /app/main.py status` shows the expected jobs and an empty outbox.
- [ ] First cycle in the logs: extracted > 0, delivered > 0, rejected explained.

## 5. Observability

- [ ] Prometheus scrapes `http://<private-address>:9464/metrics`; `mittelconnect_cache_up == 1`.
- [ ] Rules from `deploy/monitoring/alerts.yml` loaded; one test alert reached your phone or inbox.
- [ ] Logs visible in Loki: `{site="<site>", service="middleware"}` returns lines from the last 5 minutes.
- [ ] Loki alert for a silent site: `absent_over_time({site="<site>"}[15m])`.

## 6. Prove it on site

- [ ] Outage drill against the real SAP test system (not production SAP): block SAP egress for 10 minutes, confirm records are cached, restore, confirm the outbox drains and SAP shows each record once.
- [ ] Dead-letter review process agreed: who at the customer looks at rejected records, and how often.
- [ ] Customer IT has the runbook link (`docs/RUNBOOK.md`) and your on-call contact.

## 7. Hand-over record

- [ ] Site name, host address, WireGuard peer, image versions (`docker image ls`), date of the offline key backup and the drill result, filed in the customer folder.
