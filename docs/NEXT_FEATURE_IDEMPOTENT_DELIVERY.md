# Next feature: exactly-once delivery to SAP (idempotency keys)

## Why this is the next durability gap

The outbox already makes delivery **at-least-once**: nothing is lost through
SAP outages, restarts or power cuts (WAL with `synchronous=FULL`). The drills
prove that. What they do not test is the case where SAP **commits** a `$batch`
but the reply never arrives:

1. `SAPClient.post_batch` sends the batch (`core/sap_client.py:518`).
2. SAP commits all records, then the WireGuard tunnel drops or the gateway is
   slow, and httpx raises `ReadTimeout` (a `TransportError`).
3. `post_batch` treats that like "SAP down" and immediately re-POSTs the same
   batch (`core/sap_client.py:519`, up to `max_attempts`). If those also fail,
   `Pipeline._deliver` puts the batch in the outbox (`core/pipeline.py:628`)
   and `replay_outbox` POSTs it again later.

Every one of those re-POSTs creates the records a second time. For stock
postings that means **double-booked inventory in SAP**, which is worse for the
customer than a delayed record. The same happens if the container dies between
SAP's reply and the watermark commit (`core/pipeline.py:599`): the next cycle
re-reads and re-sends those rows.

The 90 s drill cuts the network cleanly (connection refused), so SAP never
commits and the drills show 0 duplicates. A slow or lossy tunnel produces the
ambiguous case.

## Design

1. **Deterministic idempotency key per record.** SHA-256 over job name plus
   configured key fields (for `material_stock_sync`: `Material`, `Plant`,
   `StorageLocation` and the source `LastChanged`), first 32 hex characters.
   Same source row gives the same key on every attempt, after every restart.
2. **Send the key to SAP** in a custom field, e.g. `YY1_MC_IdemKey_MMD`
   (created by the customer's SAP team in the *Custom Fields* app, extension
   of the target business object). SAP then holds the truth about what was posted.
3. **Local delivery ledger.** New SQLite table `delivered(job, idem_key,
   delivered_at)`. Keys are written in the same transaction that removes the
   batch from the outbox, and checked before every send, so a crash after
   SAP's reply never causes a resend from our side.
4. **Ambiguous failures become "in doubt", not "retry".** A `ReadTimeout`,
   `RemoteProtocolError` or `WriteError` after the request body was sent
   raises a new `SAPAmbiguousError` instead of retrying in place. The batch
   goes to the outbox with `in_doubt=1`.
5. **Reconcile before replay.** For an in-doubt batch, the replay first asks
   SAP which keys exist
   (`GET <entity_set>?$filter=YY1_MC_IdemKey_MMD eq 'k1' or ...&$select=YY1_MC_IdemKey_MMD`,
   in chunks of 20 keys), records those as delivered, and POSTs only the rest.
   Connection failures *before* the request was sent (connect errors) stay
   plain retries, because SAP cannot have seen them.

Result: **exactly-once effect** in SAP, with no extra round trip in the normal
path (the lookup only happens for in-doubt batches).

## Implementation steps

### 0. Branch and baseline

```powershell
cd $HOME\source\mittelconnect
git checkout main; git pull
git checkout -b feature/idempotent-delivery
python -m unittest discover -s tests -t . -v      # all green before you start
```

### 1. Config: key fields per job (`config.yaml`, `sandbox/config.sandbox.yaml`)

```yaml
  - name: material_stock_sync
    idempotency:
      sap_field: "YY1_MC_IdemKey_MMD"
      key_fields: ["Material", "Plant", "StorageLocation", "YY1_ChangedAt_MMD"]
```

Validate in `core/settings.py`: every `key_fields` entry must be a mapped
target field, `sap_field` must be a valid OData property name.

### 2. Key computation (`core/pipeline.py`, `Transformer`)

```python
def idempotency_key(job_name: str, record: dict, key_fields: list[str]) -> str:
    material = "\x1f".join([job_name] + [str(record.get(f, "")) for f in key_fields])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
```

In `Transformer.transform`, after mapping:
`record[sap_field] = idempotency_key(self.job_name, record, key_fields)`.
Use the *transformed* values so the key is stable across source driver
changes (pyodbc vs. oracledb date types).

### 3. Local store schema (`core/pipeline.py`, `LocalStore`)

```sql
CREATE TABLE IF NOT EXISTS delivered (
    job          TEXT NOT NULL,
    idem_key     TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (job, idem_key)
) WITHOUT ROWID;
```

Migration for existing sites: `ALTER TABLE outbox ADD COLUMN in_doubt INTEGER
NOT NULL DEFAULT 0`, guarded by `PRAGMA table_info(outbox)` so it runs once.
New methods: `mark_delivered(job, keys)`, `undelivered(job, records)`
(filters records whose key is already in the ledger), and
`purge_delivered(older_than_days)` called next to `purge_dead_letters`
(keep 30 days; long enough for any replay window).

Wrap "delete outbox entry + mark keys delivered" in **one transaction**.

### 4. SAP client (`core/sap_client.py`)

- New `class SAPAmbiguousError(SAPError)`.
- In `_post_once`, distinguish errors by phase: `httpx.ConnectError` and
  `httpx.ConnectTimeout` keep today's behaviour; `httpx.ReadTimeout`,
  `httpx.ReadError`, `httpx.RemoteProtocolError` and `httpx.WriteError`
  raise `SAPAmbiguousError`, and `post_batch` must **not** retry those in place.
- New `async def existing_keys(service_path, entity_set, field, keys) -> set[str]`
  doing the `$filter` GET in chunks of 20 (URL length limits on SAP Gateway).

### 5. Pipeline (`core/pipeline.py`)

- `_deliver`: drop records already in the ledger before batching; on success,
  `mark_delivered` the succeeded keys; on `SAPAmbiguousError`, enqueue with
  `in_doubt=1`.
- `replay_outbox`: for `in_doubt` entries call `existing_keys`, mark those
  delivered, then POST only the remainder. If the lookup itself fails, keep
  the entry in doubt and back off; never POST blind.

### 6. Mock SAP: make the failure reproducible (`mock_sap/server.py`)

- Keep a set of seen `YY1_MC_IdemKey_MMD` values loaded from
  `received.jsonl` on start. A second create with a seen key returns
  `400` with code `MC/DUPLICATE` and is counted in `_mock/stats` as
  `duplicates` (in real SAP you would see a second document instead).
- `POST /_mock/drop-replies?count=N`: commit the next N batches, then close
  the socket without replying. This is the lost-reply case.
- `GET <entity_set>?$filter=...` returning the matching keys, for step 5.

### 7. Tests (`tests/test_offline.py`)

```text
test_key_is_stable_across_restarts        same row -> same key, twice
test_ledger_skips_delivered_records       marked keys are never re-sent
test_read_timeout_is_ambiguous_not_retry  ReadTimeout -> SAPAmbiguousError, one POST only
test_in_doubt_replay_posts_only_missing   lookup says 3 of 5 exist -> POST 2
test_lookup_failure_never_posts_blind     lookup error -> entry stays in doubt
test_outbox_delete_and_ledger_atomic      crash between the two leaves neither half
test_migration_adds_in_doubt_column       old DB file upgrades in place
```

Use `httpx.MockTransport` to raise the errors; no network needed.

```powershell
python -m unittest discover -s tests -t . -v
```

### 8. New drill check (`scripts/outage_drill.sh` and `scripts/sandbox.py`)

After the existing recovery checks:

```bash
log "Lost-reply drill: SAP commits 3 batches but the replies are dropped"
curl -fsS -X POST "http://127.0.0.1:8080/_mock/drop-replies?count=3" > /dev/null
sleep 60
DUPES="$(curl -fsS "$STATS_URL" | python3 -c 'import json,sys; print(json.load(sys.stdin)["duplicates"])')"
[ "$DUPES" -eq 0 ] && pass "no duplicates after lost replies" || fail "$DUPES duplicate records reached SAP"
```

Run it locally before pushing:

```powershell
python scripts\sandbox.py reset
python scripts\sandbox.py all
```

Expect today's 9 checks plus the new one. Run the same drill on `main`
first: it should **fail** there (duplicates > 0), which proves the test
catches the bug.

### 9. Ship

```powershell
git add -A
git commit -m "Exactly-once SAP delivery with idempotency keys and in-doubt reconciliation"
git push -u origin feature/idempotent-delivery
```

Open the PR; the `sandbox-drill` CI job runs the new check on every push.

### 10. Customer rollout

- SAP team creates the custom field on the target entity and exposes it in
  the OData service (`YY1_MC_IdemKey_MMD`, text, 32 characters). Without it,
  set `idempotency.sap_field: null`: the local ledger still prevents
  crash-related resends, only lost replies stay at-least-once.
- Add an alert on `increase(mittelconnect_outbox_in_doubt[1h]) > 0` once the
  monitor exports the new column (add it to `monitor/collector.go`).

## Effort estimate

About 3 to 4 focused days: 1 for store and client changes, 1 for mock SAP and
the drill, 1 for tests, the rest for the monitor metric and review. This is
an estimate, not measured.
