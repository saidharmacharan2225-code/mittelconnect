# MittelConnect: production deployment and scaling

This guide takes a customer site from nothing to a monitored production
installation. The sandbox (`docker-compose.yml`) is for testing only; production
uses `deploy/docker-compose.prod.yml`. Tick off `docs/PRODUCTION_CHECKLIST.md`
before a site goes live; it covers sealing the master key
(`scripts/seal_master_key.sh`) and central logs (`deploy/docker-compose.logging.yml`).

## 1. Choose the topology

| | A. Edge server at the plant | B. Hetzner Cloud (EU) host |
|---|---|---|
| Where it runs | Small Linux server or VM inside the customer's network | One dedicated cloud server per site in Nuremberg, Falkenstein or Helsinki |
| Database access | Local network, no tunnel | WireGuard site-to-site tunnel into the plant |
| Customer IT effort | Provide a VM (2 vCPU, 4 GB RAM, 40 GB disk, Ubuntu 24.04) | Configure one WireGuard peer on their router or firewall |
| Our effort | Remote access via the customer's VPN | Fully under our control: `terraform apply` + `scripts/deploy.sh` |
| Choose when | IT forbids inbound tunnels or data must not leave the site | Customer has no virtualisation or wants a managed service |

Both topologies use the same compose file, the same `scripts/site_init.sh`
and the same `scripts/deploy.sh`. Only Section 2 differs.

## 2a. Prepare an edge server (topology A)

On the customer VM, as an admin user:

```bash
# Docker Engine and the Compose plugin: RUNBOOK.md section 1
sudo apt-get install -y rsync openssl
sudo usermod -aG docker "$USER"                 # log out and in afterwards
sudo install -d -m 0755 -o "$USER" -g "$USER" /srv/mittelconnect/app
```

Then continue with Section 3, using `<admin-user>@<vm-address>` as the SSH target.
The metrics endpoint stays on `127.0.0.1:9464` unless you set `MCMON_BIND`
in the site `.env` to an address your monitoring server can reach.

## 2b. Provision a Hetzner host (topology B)

Requirements on your workstation: Terraform 1.6 or newer, an SSH key, and a
Hetzner Cloud project with an API token (read/write).

```bash
cd deploy/hetzner
cp terraform.tfvars.example terraform.tfvars      # gitignored
$EDITOR terraform.tfvars                          # admin key, admin_cidrs, sites
export TF_VAR_hcloud_token='...'                  # never written to a file
terraform init
terraform plan -out site.plan
terraform apply site.plan
terraform output sites
```

Each entry in `sites` creates one server, one data volume (outbox, dead
letters, watermarks) and one WireGuard tunnel. What the host gets on first boot:

- `deploy` user with your SSH key; root login and passwords disabled; fail2ban.
- Hetzner firewall: SSH only from `admin_cidrs`, WireGuard UDP 51820, ICMP. Nothing else.
- Docker from Docker's signed repository, with log rotation, `live-restore`
  and `no-new-privileges` as daemon defaults.
- Unattended security upgrades with an automatic reboot at 03:30 if needed.
  Containers restart on their own (`restart: unless-stopped`).
- Data volume mounted at `/srv/mittelconnect` (`nodev,nosuid,noexec`).
- Delete and rebuild protection, plus daily Hetzner backups (`enable_backups`).

### WireGuard to the plant

The host generates its own WireGuard key on first boot; the private key
never leaves the server. Fetch the public key:

```bash
ssh deploy@<host> cat /etc/wireguard/server.pub
```

Give the customer's IT this peer definition (FRITZ!Box 7.50+, OPNsense,
pfSense, MikroTik, Sophos and Linux all accept it):

```ini
[Interface]
# Plant side
PrivateKey = <their private key; the matching public key is plant_wg_public_key>
Address    = 10.99.0.2/32

[Peer]
# MittelConnect host
PublicKey           = <content of /etc/wireguard/server.pub>
Endpoint            = <public IPv4 of the host>:51820
AllowedIPs          = 10.99.0.1/32
PersistentKeepalive = 25
```

On their firewall, allow only `10.99.0.1` to reach the database ports
(1433 for SQL Server, 1521 for Oracle) and, for on-premise SAP, the gateway
HTTPS port. Check from the host:

```bash
ssh deploy@<host> 'sudo wg show; nc -vz -w 5 <db-ip> 1433'
```

## 3. First deployment of a site

```bash
scripts/deploy.sh deploy@<host> --init <site-name>
```

This copies the code (never `.env`, `secrets/` or `data/`), builds both
images on the host, then runs `scripts/site_init.sh` interactively there:

1. Generates the Fernet master key on the host
   (`/srv/mittelconnect/site/secrets/master.key`, 0400, uid 10001).
2. Prompts, without echo, for the database and SAP credentials and stores
   them only as `ENC[...]` values in `/srv/mittelconnect/site/.env`.
3. Generates a separate pseudonymisation key.
4. Copies `config.yaml` to `/srv/mittelconnect/site/config.yaml`.

Then:

1. **Back up the master key offline** (password manager or sealed USB stick).
   Without it the credentials and any parked outbox records cannot be decrypted.
   ```bash
   ssh deploy@<host> sudo cat /srv/mittelconnect/site/secrets/master.key
   ```
2. Edit the site configuration: database hosts, SAP `base_url` and
   `token_url`, `allowed_host_suffixes` (the egress allowlist), jobs and
   mappings. Remove the database entries and jobs the site does not use.
   ```bash
   ssh -t deploy@<host> sudoedit /srv/mittelconnect/site/config.yaml
   ```
3. If SAP uses an internal CA, copy the PEM bundle to
   `/srv/mittelconnect/site/certs/` and set `sap.tls.ca_bundle` to
   `/app/certs/<file>.pem`.
4. Deploy:
   ```bash
   scripts/deploy.sh deploy@<host>
   ```
   The script validates the configuration with `check-config` first and
   leaves any running version untouched when that fails. It then starts the
   stack and waits until the middleware reports healthy.

## 4. Updates and day-to-day

| Task | Command |
|---|---|
| Ship a new version | `scripts/deploy.sh deploy@<host>` |
| Health, outbox, dead letters, watermarks | `scripts/deploy.sh deploy@<host> --status` |
| Logs | `ssh deploy@<host> 'docker compose -f /srv/mittelconnect/app/deploy/docker-compose.prod.yml --env-file /srv/mittelconnect/site/.env logs -f middleware'` |
| Change a credential | `ssh -t deploy@<host>` then `docker run --rm -it --user 10001:10001 -v /srv/mittelconnect/site/secrets/master.key:/run/secrets/mittelconnect_master.key:ro mittelconnect:1.0.0 encrypt-secret`, paste the `ENC[...]` into `.env` with `sudoedit`, then deploy |
| Roll back | Check out the previous version locally and run `scripts/deploy.sh deploy@<host>`. The outbox and watermarks are on the data volume and survive. |

## 5. Monitoring

Every site runs `mcmon`, a small Go sidecar that reads the local cache
read-only and serves:

| Endpoint | Meaning |
|---|---|
| `/metrics` | Prometheus metrics: outbox records and age, due batches, replay attempts, dead letters and watermark progress per job, heartbeat age |
| `/healthz` | 200 while the daemon completed a cycle within `MC_HEALTH_MAX_AGE_SECONDS` |
| `/readyz` | 200 while the cache database can be read |

On Hetzner it listens on the host's private address (`10.42.1.<10+n>:9464`).
Put one Prometheus + Alertmanager server into the same private network
(`terraform output private_network_id`), use
`deploy/monitoring/prometheus.yml` as the starting configuration and load
`deploy/monitoring/alerts.yml`. Alerts with `severity: page` correspond to
the 4-hour outage response in the support contract.

## 6. Scaling

MittelConnect scales **out by site** and **up within a site**.

**One isolated instance per customer site.** Each site has its own host
(or edge VM), master key, credentials, outbox and network tunnel. A problem
or a breach at one customer cannot touch another, which is also what the
DPA (AVV) promises. Adding a customer is one more entry in `sites` plus
`terraform apply` and `scripts/deploy.sh --init`.

**Within one site, tune in this order:**

1. `sap.batch_size` (records per `$batch`, default 100) and
   `sap.max_connections` (default 10). Raise them only together with the
   customer's SAP Basis team; the gateway's work processes are usually the
   bottleneck, not MittelConnect.
2. `jobs[].chunk_size` (rows per extraction chunk) and
   `service.poll_interval_seconds` (cycle frequency).
3. Vertical size: change the site's `server_type` in Terraform (the volume
   and outbox stay), and raise `MC_CPUS` / `MC_MEMORY` in the site `.env`.
4. Split jobs across instances: run a second site entry with a disjoint set
   of jobs (each instance keeps its own watermarks; never enable the same job
   on two instances, or rows are delivered twice).

**Where the limit is.** The pipeline is I/O-bound, so throughput is set by
how fast SAP accepts batches, not by CPU. When
`mittelconnect_outbox_due_batches` stays above zero between cycles, SAP is
the bottleneck; when `mittelconnect_heartbeat_age_seconds` approaches the
poll interval plus the cycle time, extraction is, and step 2 or 3 applies.
Measure each new customer's rate during the sandbox outage drill with their
real record shapes before committing to volumes in the contract.

**During SAP outages** the outbox grows on the data volume; plan
`volume_size_gb` for the longest outage you promise to buffer
(`max_outbox_records`, default 500,000, applies backpressure before the
disk fills). The `MittelConnectOutboxNearLimit` alert fires at 80%.

## 7. Security checklist before go-live

- [ ] `admin_cidrs` contains only office or VPN addresses.
- [ ] Master key backed up offline, and the backup location recorded in the customer file.
- [ ] Database logins are read-only (`db_datareader` / `SELECT` grants only).
- [ ] `security.allowed_host_suffixes` lists only the customer's SAP hosts.
- [ ] `sap.tls.verify: true` and `allow_insecure_http: false`.
- [ ] Personal-data fields carry `pseudonymize: true` (see the GDPR whitepaper).
- [ ] Prometheus scrapes the site and a test alert reached the on-call phone.
- [ ] The outage drill (`docs/OUTAGE_DRILL.md`) passed against this site's configuration in the sandbox.
