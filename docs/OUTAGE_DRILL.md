# Simulated outage drill

The drill proves three things: an SAP or network outage never stops or crashes
the factory side, every record produced during the outage is kept encrypted on
local disk, and all of it reaches SAP once the connection is back.

Only the link between the middleware and SAP is cut. The legacy database, the
machines and the simulator keep running, so the shop floor is never affected.

## 1. Automated drill (sandbox)

With the sandbox running (`docker compose up -d`, see `RUNBOOK.md`):

```bash
./scripts/outage_drill.sh          # 90-second outage
./scripts/outage_drill.sh 300      # 5-minute outage
```

What the script does:

1. Checks the middleware is healthy and the outbox is empty.
2. Disconnects the middleware container from the `sap` Docker network. This is
   a real network cut: the SAP host no longer resolves or answers.
3. Halfway through, checks that new records are in the local outbox, then
   restarts the middleware to prove the cache survives a restart.
4. At the end of the outage, checks that the container is still running and
   the circuit breaker opened.
5. Reconnects the network and waits until the outbox is empty.
6. Checks that SAP received at least as many new records as were cached, and
   that the middleware is healthy again.

The network link is restored even if the script fails or you press `Ctrl+C`.
Expected end of the output:

```text
  PASS  outbox drained
  PASS  SAP received 31 new records (at least the 24 that were cached)
  PASS  middleware is healthy after recovery

[10:42:17] Result
  DRILL PASSED
```

The record counts depend on how long the outage lasts (the simulator adds
about 4 rows a minute).

## 2. Manual drill, step by step (sandbox)

Use this when you want to watch each stage, for example in a customer demo.
Open two terminals in the project directory.

**Terminal 1: follow the logs**

```bash
docker compose logs -f --since 1m middleware
```

**Terminal 2: run the drill**

```bash
# 0. Baseline
curl -s http://127.0.0.1:8080/_mock/stats; echo
docker compose exec -T middleware python /app/main.py status

# 1. Cut the network link to SAP
MW=$(docker compose ps -q middleware)
docker network disconnect mittelconnect-sandbox_sap "$MW"
```

Within one cycle (20 seconds in the sandbox) terminal 1 shows the retries,
then the breaker opening and records going to the outbox:

```text
"msg": "SAP request failed (attempt 1/3): ConnectError: ...; retrying in 1.0s"
"msg": "Circuit 'sap' opened after 3 consecutive failures"
"msg": "Job material_stock_sync: SAP unavailable, caching 4 records: ..."
"msg": "Job material_stock_sync: extracted=4 delivered=0 cached=4 ..."
```

```bash
# 2. While the link is down: the cache grows, the container stays healthy
docker compose exec -T middleware python /app/main.py status
docker inspect --format '{{.State.Health.Status}}' "$MW"

# 3. Optional: prove the cache survives a restart or power cut
docker compose restart middleware
MW=$(docker compose ps -q middleware)
docker network disconnect mittelconnect-sandbox_sap "$MW" 2>/dev/null || true
docker compose exec -T middleware python /app/main.py status

# 4. Restore the link
docker network connect mittelconnect-sandbox_sap "$MW"
```

Within one or two cycles terminal 1 shows the breaker closing and the replay:

```text
"msg": "Circuit 'sap' half-open: allowing trial request"
"msg": "Circuit 'sap' closed: upstream recovered"
"msg": "Replaying 3 outbox entries"
```

```bash
# 5. Verify: outbox empty, SAP counter grew by at least the cached records
docker compose exec -T middleware python /app/main.py status
curl -s http://127.0.0.1:8080/_mock/stats; echo
```

### Variant: SAP answers with errors instead of disappearing

A real SAP system under maintenance often answers with HTTP 503 rather than
dropping off the network. The mock gateway can simulate that:

```bash
curl -s -X POST "http://127.0.0.1:8080/_mock/outage?down=true"; echo
# wait two or three cycles, check status as above, then:
curl -s -X POST "http://127.0.0.1:8080/_mock/outage?down=false"; echo
```

### Variant: the legacy database goes away

```bash
MW=$(docker compose ps -q middleware)
docker network disconnect mittelconnect-sandbox_plant "$MW"
# Logs show "Job material_stock_sync: source error: ..." each cycle; the daemon keeps running
# and the watermark does not move, so nothing is skipped.
docker network connect mittelconnect-sandbox_plant "$MW"
# The next cycle reads everything that changed while the database was unreachable.
```

The middleware retries the database connection with backoff inside each cycle.
If the database stays unreachable for more than about three minutes, the
container's health turns `unhealthy`: that is the intended alert, not a crash.
It turns healthy again after the first cycle that completes.

## 3. Pass criteria

| Check | How to see it | Pass |
| --- | --- | --- |
| Factory side unaffected | `docker compose logs simulator` | Inserts continue throughout |
| Middleware stays up | `docker inspect --format '{{.State.Status}}' $MW` | `running` the whole time, never restarted by Docker |
| Data is cached | `main.py status` | `outbox_records` grows during the outage |
| Cache is encrypted | see `RUNBOOK.md` 5.4 | payloads start with `gAAAAA` |
| Cache survives restart | `main.py status` before and after `docker compose restart` | same or higher `outbox_records` |
| SAP is protected | logs | `Circuit 'sap' opened`, then no request storm |
| Full recovery | `main.py status` and mock stats | `outbox_records` back to 0, SAP `received` grew by at least the peak outbox count |
| No data loss | SAP `received` vs source rows | every non-`INVALID` row in `dbo.Lagerbestand` arrives exactly once or more |

Delivery is at-least-once: if SAP accepted a batch but the reply was lost in
the cut, that batch can arrive twice. In production, set up SAP-side duplicate
checks (for example on material, plant and timestamp) for interfaces where a
repeat would matter.

## 4. Running the drill on a customer system

Do this only in an agreed maintenance window, with the customer's IT and SAP
Basis informed. The drill touches only the middleware host's outbound traffic
to SAP; it does not touch the PLCs, the legacy database or SAP itself.

```bash
# On the middleware host. Find the middleware's container IP and the SAP host IP.
MW_IP=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' mittelconnect | awk '{print $1}')
SAP_IP=$(getent hosts s4.example-werk.de | awk '{print $1}')
echo "middleware=$MW_IP sap=$SAP_IP"

# Start the outage: reject traffic from the middleware to SAP only
sudo iptables -I DOCKER-USER -s "$MW_IP" -d "$SAP_IP" -j REJECT

# Observe as in section 2 (logs, status), for the agreed duration

# End the outage: remove exactly the rule you added
sudo iptables -D DOCKER-USER -s "$MW_IP" -d "$SAP_IP" -j REJECT
sudo iptables -L DOCKER-USER -n --line-numbers
```

Replace `s4.example-werk.de` with the SAP host from `config.yaml` and
`mittelconnect` with the container name on that host. If SAP is reached through
a proxy or load balancer, block that address instead. Record the start and end
times, peak `outbox_records` and the time until the outbox was empty in the
quarterly report.
