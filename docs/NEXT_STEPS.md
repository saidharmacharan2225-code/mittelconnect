# MittelConnect: next steps after the first outage drill

The sandbox drill passed 9/9 on Windows (Docker Desktop 4.93, engine 29.8.1) on
2026-10-06. These are the next steps, highest value first. Steps 1 and 2 are
and 3 are done in this repository. Steps 4 to 6 are ready-to-paste changes.

## 1. Sandbox lifecycle commands (done)

`scripts/sandbox.py` gained `down` and `reset`:

```powershell
python scripts\sandbox.py down    # stop containers, keep data (outbox, DB, received.jsonl)
python scripts\sandbox.py reset   # stop + delete volumes, secrets\ and .env
python scripts\sandbox.py all     # clean bootstrap, start, wait healthy, drill
```

`reset` removes volumes and secrets together on purpose: the SQL Server volume
stores the SA password from the old `.env`, so wiping only one of them leaves
the sandbox unable to log in.

## 2. Go monitor in the sandbox (done, verified)

`docker-compose.monitor.yml` runs `mcmon` next to the middleware exactly like
production does. Verified: `/readyz` returned 200 and `/metrics` exported
`mittelconnect_outbox_records`, `mittelconnect_dead_letters`,
`mittelconnect_heartbeat_age_seconds` and the watermark per job.

```powershell
docker compose -f docker-compose.yml -f docker-compose.monitor.yml up -d --build
curl.exe -s http://127.0.0.1:9464/metrics
```

`mittelconnect_dead_letters = 5` is expected: `sandbox/mssql/seed.sql` seeds
`INVALID-*` materials that mock SAP rejects with `M3/305`, which proves the
dead-letter path works.

## 3. Outage drill in CI on every push (done)

The `sandbox-drill` job in `.github/workflows/ci.yml` boots the full sandbox
with the monitor on every push and pull request, runs `outage_drill.sh 60`,
then checks that the monitor reports an empty outbox and that mock SAP received
no duplicate records. Container logs are printed when any step fails.

## 4. Longer and harsher drills

The 90 s drill only cached 6 records. Customers will ask about a full SAP
maintenance weekend. Run these before the first pilot:

| Drill | Command | What it proves |
|---|---|---|
| Long outage | `python scripts\sandbox.py drill 1800` | 30 min backlog drains, no duplicates |
| High volume | set `SIMULATOR_INTERVAL=1` in `.env`, then `docker compose up -d simulator`, then drill 600 | batching and `max_outbox_records` hold up |
| Database outage | `docker compose stop mssql`, wait 2 min, `docker compose start mssql` | source-side reconnect, watermark not skipped |
| Host reboot | restart Docker Desktop mid-outage | encrypted outbox survives a full stop |

For the long outage, also check that SAP received no duplicates:

```bash
docker compose exec -T mock-sap python3 -c "import json,collections;c=collections.Counter((r['Material'],r['Plant'],r['StorageLocation'],r['YY1_ChangedAt_MMD']) for r in map(json.loads,open('/data/received.jsonl')));print(sum(1 for v in c.values() if v>1),'duplicates in',sum(c.values()),'records')"
```

After the 90 s drill this printed `0 duplicates in 556 records`. Run it from
Git Bash; PowerShell mangles the inner quotes.

## 5. Alerting on the metrics you now have

`deploy/monitoring/alerts.yml` already holds the rules. The three that matter
most for a pilot customer:

```yaml
- alert: MittelConnectOutboxGrowing
  expr: mittelconnect_outbox_records > 0 and delta(mittelconnect_outbox_records[15m]) > 0
  for: 30m
- alert: MittelConnectHeartbeatStale
  expr: mittelconnect_heartbeat_age_seconds > 600
  for: 5m
- alert: MittelConnectNewDeadLetters
  expr: increase(mittelconnect_dead_letters[1h]) > 0
```

Point them at an email or Telegram receiver in Alertmanager so you hear about a
customer's outage before they call you.

## 6. Key management before the first real customer

- Back up `secrets/master.key` per site (password manager or an offline USB
  stick). Losing it makes the encrypted outbox and `.env` values unreadable.
- Rotate sandbox secrets with `python scripts\sandbox.py reset` then `all`.
- Never commit `.env` or `secrets/` (`.gitignore` already excludes both).
