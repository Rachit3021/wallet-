# Isolation and locking

Transfers run at PostgreSQL **Read Committed** with explicit row locks. The
money path is deliberately visible: each posting re-reads the transfer and both
account rows under `FOR UPDATE`, rechecks the source balance, and only then
writes the stored balances and the two ledger legs.

## Transaction shape

```text
BEGIN;
  SELECT ... FROM transfers WHERE id = %s FOR UPDATE;       -- lock intent
  SELECT ... FROM accounts WHERE id = %s FOR UPDATE;        -- lower id first
  SELECT ... FROM accounts WHERE id = %s FOR UPDATE;        -- higher id second
  -- if user wallet and balance < amount: raise INSUFFICIENT_FUNDS
  UPDATE accounts SET balance_paise = balance_paise - %s ...;
  UPDATE accounts SET balance_paise = balance_paise + %s ...;
  INSERT INTO ledger_entries ...;                          -- two legs
  UPDATE transfers SET status = 'posted' ...;
COMMIT;
```

Contention inside one API process is further serialized by
`_TRANSFER_LOCK` **before** a database transaction starts. Waiting threads do
not hold connections or row locks. Cross-process safety still comes from the
row locks and a retry wrapper for `DeadlockDetected` / `SerializationFailure`.

## Two manual psql sessions

Open two terminals against the database:

```bash
docker compose exec -T postgres psql -U wallet -d wallet
```

### Session A — hold the row lock

```sql
BEGIN;
SELECT id, balance_paise FROM accounts WHERE id = 4 FOR UPDATE;
-- leave the transaction open
```

### Session B — observe the block

```sql
SET lock_timeout = '2s';
BEGIN;
SELECT id, balance_paise FROM accounts WHERE id = 4 FOR UPDATE;
```

Expected output after two seconds:

```text
ERROR:  canceling statement due to lock timeout
```

From a third session the waiting lock is visible:

```sql
SELECT pid, state, wait_event_type, wait_event
FROM pg_stat_activity
WHERE datname = 'wallet' AND state <> 'idle';
```

```text
 pid  | state  | wait_event_type | wait_event
------+--------+-----------------+------------
  ... | active | Lock            | tuple
```

Session A then commits and B can retry:

```sql
-- Session A
UPDATE accounts SET balance_paise = balance_paise + 1 WHERE id = 4;
COMMIT;

-- Session B
BEGIN;
SELECT id, balance_paise FROM accounts WHERE id = 4 FOR UPDATE;
ROLLBACK;
```

## Automated two-session output

The same scenario is available as a deterministic script:

```bash
docker compose exec -T api python -m scripts.isolation_demo
```

```text
wallet id = 4, initial balance = 984747 paise
A: BEGIN; SELECT ... FROM accounts WHERE id = %s FOR UPDATE;
A: locked row, sees 984747 paise
B: SET LOCAL lock_timeout = '1s'; SELECT ... FOR UPDATE;
B: LockNotAvailable: canceling statement due to lock timeout
A: UPDATE accounts SET balance_paise = balance_paise + 1 WHERE id = %s; COMMIT;
A: committed
B: SELECT ... FOR UPDATE;  -- retry after A commits
B: now sees 984748 paise
cleanup: restored the original balance
```

The balance number comes from the seeded database; the lock behaviour is the
important part. The script restores the original balance at the end.
