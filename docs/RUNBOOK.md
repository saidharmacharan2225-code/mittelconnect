# MittelConnect Runbook: install, build, configure, run

Every command below is meant to be copied as is. Commands assume Ubuntu 22.04 or
24.04; notes cover macOS and Windows where they differ. Run them from the
project directory (the folder that contains `docker-compose.yml`) unless a step
says otherwise.

## 1. Install Docker

### Ubuntu 22.04 / 24.04 (Docker Engine from Docker's official repository)

```bash
# Remove distribution packages that conflict with Docker Engine
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  sudo apt-get remove -y "$pkg" 2>/dev/null
done

# Add Docker's signing key and repository
sudo apt-get update
sudo apt-get install -y ca-certificates curl openssl python3
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine and the Compose plugin
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Allow your user to run docker without sudo (log out and in, or use newgrp)
sudo usermod -aG docker "$USER"
newgrp docker

# Verify
docker run --rm hello-world
docker compose version
```

### macOS and Windows

Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and
start it. On Windows, run the commands below in a WSL 2 Ubuntu shell. On Apple
Silicon, enable *Settings > General > Use Rosetta for x86_64/amd64 emulation*,
because the SQL Server image is amd64-only.

Give Docker at least 4 GB of memory (SQL Server needs 2 GB on its own).

## 2. Get the code onto the machine

```bash
mkdir -p ~/mittelconnect && cd ~/mittelconnect
# Copy the project folder here (scp, USB stick, or git clone once a repository exists), then:
ls docker-compose.yml Dockerfile config.yaml main.py
chmod +x scripts/*.sh sandbox/mssql/*.sh
```

## 3. Create the master key and inject credentials safely

The bootstrap script builds the middleware image, creates the Fernet master key
and generates random passwords. It encrypts every secret the middleware reads,
so the `.env` file only holds `ENC[...]` values for it.

```bash
./scripts/bootstrap_sandbox.sh
```

What it leaves behind:

| File | Contents | Permissions |
| --- | --- | --- |
| `secrets/master.key` | Fernet master key, mounted into the container as a Docker secret | directory 0700, file 0444 |
| `.env` | SQL Server SA password and mock SAP secret (mock containers only), plus `ENC[...]` values for the middleware | 0600 |

Check that nothing secret is readable by others and that the key is backed up:

```bash
ls -ld secrets && ls -l secrets/master.key .env
cp secrets/master.key /path/to/offline/backup/mittelconnect-master.key   # e.g. an encrypted USB stick
```

Without the master key, the `ENC[...]` values and any records waiting in the
outbox cannot be decrypted. Keep one offline copy.

### Adding or changing a secret by hand

```bash
# Encrypt a value without it appearing in shell history or the process list
read -rsp 'Secret: ' SECRET; echo
printf '%s' "$SECRET" | docker run --rm -i --network none \
  -e MITTELCONNECT_MASTER_KEY="$(cat secrets/master.key)" \
  mittelconnect:1.0.0 encrypt-secret --stdin
unset SECRET
```

Paste the printed `ENC[...]` value into `.env` under the right variable
(`MC_MSSQL_PASSWORD_ENC`, `MC_SAP_CLIENT_SECRET_ENC` or
`MC_PSEUDONYMIZATION_KEY_ENC`), then restart the middleware:

```bash
docker compose up -d middleware
```

### Production differences

- Do not use `.env` on production hosts. Inject the `MC_*` variables from your
  secret store (HashiCorp Vault, a systemd `EnvironmentFile` owned by root with
  mode 0600, or your orchestrator's secrets) and still keep them as `ENC[...]`.
- Make the key file readable only by the container user:
  `sudo chown 10001:10001 /etc/mittelconnect/master.key && sudo chmod 0400 /etc/mittelconnect/master.key`.
- Use your real `config.yaml` with `allow_insecure_http: false`,
  `trust_server_certificate: false` and your SAP host on the allowlist.

## 4. Build and start the sandbox

```bash
docker compose up -d --build
docker compose ps
```

Expected after about one to two minutes:

```text
SERVICE      STATUS
mssql        Up (healthy)
mock-sap     Up (healthy)
simulator    Up
middleware   Up (healthy)
```

`mssql-init` runs once, seeds the database and exits with code 0, so it is not
listed as running. Check its result with `docker compose logs mssql-init`; the
last line must be `Seed complete`.

## 5. Run the sandbox test

### 5.1 Watch the first cycle

```bash
docker compose logs -f middleware
```

Within the first cycle you should see, among others:

```text
"msg": "Connected to mssql source 'erp_legacy_mssql'"
"msg": "Obtained SAP OAuth2 token valid for 300s"
"msg": "Job material_stock_sync: SAP rejected 1 records: M3/305: Material INVALID-97 ist nicht vorhanden"
"msg": "Job material_stock_sync: extracted=5xx delivered=4xx cached=0 rejected=5 ..."
```

Press `Ctrl+C` to stop following the log (the container keeps running).

### 5.2 Check what SAP received

```bash
curl -s http://127.0.0.1:8080/_mock/stats; echo
```

Pass: `rejected` is `5` (the seeded `INVALID-*` rows) and `received` equals
495 plus the number of rows the simulator has inserted so far. `received` keeps
growing by about 4 rows a minute.

### 5.3 Check the middleware's own state

```bash
docker compose exec -T middleware python /app/main.py status
```

Pass: `outbox_records` is `0`, `dead_letters` is `5`, and the watermark is a
timestamp from the last minute.

### 5.4 Confirm the GDPR measures

```bash
# Worker names arrive pseudonymised (PSN...), never in clear text
docker compose exec -T mock-sap sh -c 'tail -n 3 /data/received.jsonl'

# The local cache contains no readable record data
docker compose exec -T middleware python -c "import sqlite3; c = sqlite3.connect('/app/data/mittelconnect_cache.db'); print(c.execute('SELECT COUNT(*) FROM dead_letters').fetchone()); print(c.execute('SELECT substr(payload, 1, 40) FROM dead_letters LIMIT 1').fetchone())"

# The middleware runs as uid 10001 on a read-only root filesystem
docker compose exec -T middleware sh -c 'id; touch /app/test 2>&1 || true'
```

Pass: `YY1_LastEditor_MMD` values start with `PSN`; the dead-letter payload
starts with `gAAAAA` (Fernet ciphertext); `id` shows `uid=10001` and `touch`
fails with `Read-only file system`.

### 5.5 Look at the source data (optional)

```bash
set -a; . ./.env; set +a
docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C \
  -d PRODUKTION -Q "SELECT TOP 5 ArtikelNr, Werk, Bestand, LetzterBearbeiter, LastChanged FROM dbo.Lagerbestand ORDER BY LastChanged DESC"
```

SQL Server is also reachable from SSMS or Azure Data Studio on `127.0.0.1,14330`.

## 6. Day-to-day operations

| Task | Command |
| --- | --- |
| Follow logs | `docker compose logs -f middleware` |
| Health | `docker inspect --format '{{.State.Health.Status}}' $(docker compose ps -q middleware)` |
| Outbox and watermarks | `docker compose exec -T middleware python /app/main.py status` |
| Validate configuration | `docker compose exec -T middleware python /app/main.py check-config` |
| Run one cycle by hand | `docker compose exec -T middleware python /app/main.py run --once` |
| Restart after a config change | `docker compose up -d middleware` |
| Graceful stop (SIGTERM, finishes the current chunk) | `docker compose stop middleware` |
| Stop the sandbox, keep data | `docker compose down` |
| Wipe all sandbox data | `docker compose down -v` |

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `CONFIG ERROR: No master key found` | `secrets/master.key` missing or not mounted | Run `./scripts/bootstrap_sandbox.sh`; check `ls -l secrets/` |
| `CONFIG ERROR: Cannot decrypt ...` | `.env` was encrypted with a different master key | Re-run bootstrap with `--force` and `docker compose down -v` |
| `mssql` stays `unhealthy` | Less than 2 GB RAM for Docker, or no amd64 emulation on Apple Silicon | Raise Docker memory; enable Rosetta |
| `mssql-init` exits with code 1 | SQL Server slow to start | `docker compose up -d mssql-init` to retry |
| `Login failed for user 'mc_reader'` | Database volume from an older bootstrap | `docker compose down -v`, then start again |
| `Host '...' is not in security.allowed_host_suffixes` | SAP host missing from the allowlist | Add the host suffix in `config.yaml` |
| `outbox_records` keeps growing | SAP unreachable or rejecting credentials | `docker compose logs middleware | grep -i sap`; check the token URL and client secret |
| Middleware `unhealthy` | No cycle finished for 180 s | Check logs for a crash loop or a long-running query |
