# Digital Wallet Backend

A double-entry wallet ledger built with FastAPI, PostgreSQL, dbmate, and
APScheduler. Money is stored as whole paise in `BIGINT`; the ledger is
immutable; balances are protected by row locks and a deferred balance trigger.

## Quick start

```bash
docker compose up
```

That single command starts:

- `postgres` — PostgreSQL 16 with a healthcheck
- `migrate` — applies `migrations/*.sql` with dbmate, then exits
- `api` — FastAPI on <http://localhost:8000>
- `worker` — APScheduler running nightly reconciliation at 02:00 Asia/Kolkata

The API waits for migrations to finish. Check it with:

```bash
curl http://localhost:8000/health
```

Optional seed profile:

```bash
docker compose --profile seed up seed
```

By default the seeder creates 1,000 users, 2,000 wallets, and 100,000 posted
transfers. It truncates mutable tables first, so do not run it against data you
want to keep.

## API contract

Every `POST` requires an `Idempotency-Key` header containing a UUID. Replaying
the same key with the same body returns the first saved response. Reusing the
key with a different body returns `422 IDEMPOTENCY_KEY_REUSED`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness plus database version/time |
| `POST` | `/users` | Create a user |
| `POST` | `/users/{user_id}/wallets` | Create a wallet |
| `GET` | `/users/{user_id}/balance` | Per-wallet and total paise |
| `GET` | `/users/{user_id}/statement` | Paginated ledger entries (`limit`, `offset`, `cursor`) |
| `POST` | `/users/{user_id}/statement/ask` | Natural-language statement Q&A |
| `POST` | `/users/{user_id}/transfers` | Create a transfer |
| `GET` | `/transfers/{transfer_id}` | Transfer detail |
| `POST` | `/transfers/{transfer_id}/release` | Release a held transfer |
| `POST` | `/admin/reconciliation/run` | Run the book check now |
| `GET` | `/admin/reconciliation/runs` | Recent runs |
| `GET` | `/admin/reconciliation/problems?run_id=` | Reported corruptions |
| `GET` | `/admin/risk-flags` | Recent fraud flags |

Errors use a uniform shape:

```json
{"code": "INSUFFICIENT_FUNDS", "message": "...", "request_id": "uuid"}
```

A quick end-to-end walkthrough is in `scripts/smoke.py`:

```bash
docker compose exec -T api python -m scripts.smoke
```

## Design Q&A

### Why paise in `BIGINT`?

Floating point cannot represent decimal money exactly. All amounts are positive
integer paise, so addition, comparison, and `SUM()` are exact. There is no
rounding path anywhere in the money movement.

### Why both a stored balance and a ledger?

The ledger is the source of truth, but recomputing a balance on every read would
be expensive. `accounts.balance_paise` is a cached projection updated in the
same transaction as the ledger legs. Nightly reconciliation compares the cache
with the ledger and reports drift; it never silently repairs it.

### How are concurrent spends made safe?

Each posting locks the transfer row and then both account rows with
`SELECT ... FOR UPDATE`, in ascending account id order. The source balance is
rechecked after the locks are acquired. Whole transfer transactions are also
serialized inside the API process before opening a DB transaction, so a
same-wallet stampede does not build a deadlock convoy. Cross-process safety is
provided by the row locks plus retries for `DeadlockDetected` and
`SerializationFailure`.

### Why can system accounts go negative?

`system_cash_in`, `system_cash_out`, and `system_fee_revenue` represent external
counterparties, not customer deposits. A partial CHECK applies `balance >= 0`
only to rows with a non-null `owner_user_id`.

### Why are ledger rows immutable?

Corrections are new reversal transfers whose `original_transfer_id` points at
the original. A `BEFORE UPDATE OR DELETE` trigger rejects edits, and a deferred
constraint trigger verifies at commit that each transfer's debits and credits
match its amount.

### Why idempotency in the database?

The `(user_id, key)` unique index plus a stored response makes retries safe
even across API restarts. Business failures are stored too, so a client that
retries an insufficient-funds response sees the same failure rather than
re-running the operation.

### Why `REPEATABLE READ` for reconciliation?

Every check in a run must see one consistent snapshot. The job takes a
`FOR UPDATE NOWAIT` lock on the `reconciliation` row so two workers cannot run
at once, records all problems in the same snapshot transaction, and reports
deduplicated corruptions.

## Tradeoffs and next steps

- The in-process transfer lock is simple and safe for one API process, but it
  caps horizontal write throughput. Next step: shard a PostgreSQL advisory lock
  by account id and remove the process-local lock.
- Fraud flags and LLM explanations are handled in the request/nightly path.
  Next step: an outbox table plus worker consumers.
- There is no authentication yet; `user_id` comes from the path. Next step: JWT
  or session auth and the idempotency scope derived from the token.
- Reconciliation reports drift but does not auto-repair. Next step: a guarded,
  audited repair workflow for a narrow set of corruption classes.
- `MockLLMClient` is the default so CI needs no API key. Set `LLM_PROVIDER=openai`
  and `OPENAI_API_KEY` for a real model.

## Validation and CI

```bash
docker compose up -d --build
docker compose exec -T api python -m pytest -q
docker compose exec -T api python -m scripts.stampede --runs 20
docker compose exec -T api python -m scripts.seed --users 10 --wallets-per-user 2 --transfers 200
docker compose exec -T api python -m scripts.seed
```

The stampede acceptance test fires 100 concurrent 10-paise spends from a
500-paise wallet. All 20 runs must produce 50 successes, 50
`INSUFFICIENT_FUNDS` responses, a zero wallet balance, a zero ledger-derived
balance, and a zero global debit/credit delta.

The pytest suite covers health, posting, idempotent replay/mismatch/parallel
retries, hold and release, reconciliation with exactly three injected
corruptions, prompt-injection-safe statement Q&A, and pagination. CI runs the
same checks with `docker compose` in `.github/workflows/ci.yml`.

## Documentation

- `docs/schema.md` — nouns, cardinality, Mermaid ER diagram, invariants
- `docs/isolation.md` — two-session lock demonstration
- `docs/performance.md` — `EXPLAIN ANALYZE` notes
- `docs/fraud.md` — DB-backed fraud rules and LLM safety
- `docs/demo.md` — five-minute demo script and video asset
